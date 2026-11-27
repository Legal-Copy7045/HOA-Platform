from __future__ import annotations

import hashlib

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Max

from audit.services import record_event
from core.policy import require
from core.scoping import system_access
from documents.models import (
    Category,
    Document,
    DocumentVersion,
    ScanResult,
    VersionScan,
    Visibility,
)
from documents.scanning import detail_of, scan
from documents.validation import UploadRejected, check_upload, safe_filename


class DocumentError(ValueError):
    pass


def _safe_name(name: str, ext: str) -> str:
    return safe_filename(name, ext)


def _store_version(
    document: Document,
    uploaded,
    *,
    actor,
    note: str = "",
    request=None,
    allowed: set[str] | None = None,
) -> DocumentVersion:
    try:
        kind = check_upload(uploaded, allowed=allowed)
    except UploadRejected as exc:
        raise DocumentError(str(exc)) from exc
    digest = hashlib.sha256()
    for chunk in uploaded.chunks():
        digest.update(chunk)
    uploaded.seek(0)
    outcome = scan(uploaded)
    result, scanner = outcome
    if result == ScanResult.INFECTED:
        record_event(
            "document.upload_blocked",
            actor=actor,
            request=request,
            target=document,
            metadata={"reason": "malware"},
        )
        raise DocumentError("The file was flagged by the malware scanner and was not stored.")
    with transaction.atomic(), system_access("store document version"):
        number = (
            DocumentVersion.objects.filter(document=document).aggregate(n=Max("number"))["n"] or 0
        ) + 1
        version = DocumentVersion(
            document=document,
            number=number,
            original_filename=_safe_name(uploaded.name, kind.ext),
            content_type=kind.content_type,
            size=uploaded.size,
            sha256=digest.hexdigest(),
            uploaded_by=actor,
            note=note[:200],
        )
        version.file.save("upload", uploaded, save=False)
        version.save()
        VersionScan.objects.create(
            version=version, result=result, scanner=scanner, detail=detail_of(outcome)
        )
        if result == ScanResult.ERROR:
            # The scanner did not answer (e.g. still loading after a restart):
            # try again shortly instead of waiting for someone to notice (D57).
            from documents import scan_queue

            scan_queue.queue_soon(scan_queue.VERSION, version.pk)
        Document.objects.unscoped("set current version").filter(pk=document.pk).update(
            current_version=version
        )
        record_event(
            "document.version_uploaded",
            actor=actor,
            request=request,
            target=document,
            metadata={"version": number, "scan": result},
        )
    return version


def create_document(
    *,
    title: str,
    category: str,
    visibility: str,
    file,
    lot=None,
    folder=None,
    description: str = "",
    dated_on=None,
    actor,
    request=None,
) -> Document:
    committee = None
    if folder is not None:
        # A filed document takes its folder's visibility (and committee).
        _require_folder_writer(actor, folder)
        visibility, committee, lot = folder.visibility, folder.committee, None
    else:
        require(actor, "document.manage")
        if visibility == Visibility.COMMITTEE:
            raise DocumentError("Committee documents go in the committee's folder.")
    if visibility == Visibility.LOT and lot is None:
        raise DocumentError("Choose the home this document belongs to.")
    if visibility != Visibility.LOT:
        lot = None
    if not title.strip():
        raise DocumentError("A document needs a title.")
    with transaction.atomic():
        document = Document.objects.create(
            title=title.strip()[:200],
            category=category,
            visibility=visibility,
            lot=lot,
            folder=folder,
            committee=committee,
            description=description,
            dated_on=dated_on,
            created_by=actor,
        )
        _store_version(document, file, actor=actor, request=request)
        record_event(
            "document.created",
            actor=actor,
            request=request,
            target=document,
            metadata={"visibility": visibility, "category": category},
        )
    document.refresh_from_db()
    return document


def add_version(document: Document, *, file, note: str = "", actor, request=None):
    if document.folder_id and document.committee_id:
        _require_folder_writer(actor, document.folder)
    else:
        require(actor, "document.manage")
    return _store_version(document, file, actor=actor, note=note, request=request)


def set_archived(document: Document, *, archived: bool, actor, request=None) -> Document:
    require(actor, "document.manage")
    with system_access("archive document"):
        Document.objects.filter(pk=document.pk).update(archived=archived)
    record_event(
        "document.archived" if archived else "document.restored",
        actor=actor,
        request=request,
        target=document,
    )
    document.archived = archived
    return document


def scan_allows_download(result: str | None) -> bool:
    """One rule for every stored file (document versions and attachments)."""
    if result is None or result in (ScanResult.INFECTED, ScanResult.ERROR):
        return False
    if settings.DOCUMENT_REQUIRE_CLEAN_SCAN:
        return result == ScanResult.CLEAN
    return True


def downloadable(version: DocumentVersion) -> bool:
    """The file may be sent: it passes the scan rule and has not been
    disposed of under the retention schedule (D72)."""
    if disposed(version):
        return False
    latest = version.scans.first()
    return scan_allows_download(latest.result if latest else None)


def disposed(version: DocumentVersion) -> bool:
    from documents.models import DocumentDisposal

    return DocumentDisposal.objects.filter(version_id=version.pk).exists()


def set_dated_on(document: Document, *, dated_on, actor, request=None) -> Document:
    """The document's own date, which "N years after the document date"
    retention counts from (D72)."""
    require(actor, "document.manage")
    Document.objects.unscoped("set document date").filter(pk=document.pk).update(dated_on=dated_on)
    document.dated_on = dated_on
    record_event(
        "document.dated",
        actor=actor,
        request=request,
        target=document,
        metadata={"dated_on": dated_on.isoformat() if dated_on else None},
    )
    return document


# --- Folders and search (Milestone 3) --------------------------------------------------

from documents.models import Folder  # noqa: E402


def _require_folder_writer(actor, folder: Folder) -> None:
    """Board and manager file anywhere; a committee's members file in its folder."""
    from core.policy import SYSTEM, can

    if actor is SYSTEM or can(actor, "document.manage"):
        return
    if folder.committee_id:
        from governance.services import require_committee_member

        require_committee_member(actor, folder.committee)
        return
    require(actor, "document.manage")


def create_folder(
    *, name: str, visibility: str, parent=None, committee=None, actor, request=None
) -> Folder:
    require(actor, "document.manage")
    name = " ".join((name or "").split())[:120]
    if not name:
        raise DocumentError("A folder needs a name.")
    if visibility not in (Visibility.OWNERS, Visibility.BOARD, Visibility.COMMITTEE):
        raise DocumentError("Choose who can see this folder (there are no public folders).")
    if (visibility == Visibility.COMMITTEE) != (committee is not None):
        raise DocumentError("Committee folders need a committee, and only they have one.")
    if parent is not None and parent.visibility != visibility:
        raise DocumentError("A subfolder must have the same visibility as its parent.")
    try:
        with transaction.atomic():
            folder = Folder.objects.create(
                name=name,
                parent=parent,
                visibility=visibility,
                committee=committee,
                created_by=None if _is_system(actor) else actor,
            )
    except IntegrityError as exc:
        raise DocumentError("A folder with that name already exists here.") from exc
    record_event(
        "document.folder_created",
        actor=None if _is_system(actor) else actor,
        request=request,
        target=folder,
        metadata={"visibility": visibility},
    )
    return folder


def _is_system(actor) -> bool:
    from core.policy import SYSTEM

    return actor is SYSTEM


def committee_folder(committee, *, actor) -> Folder:
    """The committee's folder, created with the committee's name on first use."""
    from core.policy import SYSTEM

    existing = Folder.objects.filter(committee=committee, parent__isnull=True).first()
    if existing:
        return existing
    return create_folder(
        name=f"{committee.name} committee",
        visibility=Visibility.COMMITTEE,
        committee=committee,
        actor=SYSTEM,  # structural: every committee has one
    )


def committee_documents(user, committee):
    return (
        Document.objects.for_user(user)
        .filter(committee=committee, archived=False)
        .select_related("current_version")
    )


def move_document(document: Document, folder: Folder | None, *, actor, request=None) -> Document:
    """File a document in a folder (it takes the folder's visibility) or take it
    out of folders. Lot documents stay unfiled."""
    require(actor, "document.manage")
    if document.visibility == Visibility.LOT:
        raise DocumentError("Documents for one home are not filed in folders.")
    fields = {"folder": folder}
    if folder is not None:
        fields.update(visibility=folder.visibility, committee=folder.committee)
    elif document.visibility == Visibility.COMMITTEE:
        raise DocumentError("Committee documents stay in a committee folder.")
    Document.objects.unscoped("move document").filter(pk=document.pk).update(**fields)
    for key, value in fields.items():
        setattr(document, key, value)
    record_event(
        "document.moved",
        actor=actor,
        request=request,
        target=document,
        metadata={"folder_id": folder.pk if folder else None, "visibility": document.visibility},
    )
    return document


def folders_for(user):
    """Folders whose documents this person may see."""
    from core import policy

    if policy.can(user, "lot.view_all"):
        return Folder.objects.all()
    roles = policy.roles_for(user)
    from django.db.models import Q

    return Folder.objects.filter(
        Q(visibility=Visibility.OWNERS)
        | Q(visibility=Visibility.COMMITTEE, committee_id__in=roles.committee_ids)
    )


def search_documents(queryset, q: str):
    """Title, description, category label or folder name contains ``q``."""
    from django.db.models import Q

    q = " ".join((q or "").split())[:100]
    if not q:
        return queryset
    categories = [v for v, label in Category.choices if q.lower() in label.lower()]
    return queryset.filter(
        Q(title__icontains=q)
        | Q(description__icontains=q)
        | Q(folder__name__icontains=q)
        | Q(category__in=categories)
    )


def scan_summary(days: int = 7) -> dict:
    """Malware scanning at a glance for System status (counts only, no file
    names or owners): scans in the last ``days`` by result, and files whose
    latest result does not allow a download yet (waiting for a rescan)."""
    import datetime

    from django.db.models import Count, OuterRef, Subquery
    from django.utils import timezone

    from documents.models import Attachment, AttachmentScan, DocumentVersion

    since = timezone.now() - datetime.timedelta(days=days)
    counts = dict.fromkeys(ScanResult.values, 0)
    with system_access("scan summary counts"):
        for model, field in ((VersionScan, "scanned_at"), (AttachmentScan, "scanned_at")):
            rows = (
                model.objects.filter(**{f"{field}__gte": since})
                .values("result")
                .annotate(n=Count("id"))
            )
            for row in rows:
                counts[row["result"]] = counts.get(row["result"], 0) + row["n"]
        # A new attachment's first scan is on the attachment itself.
        for row in (
            Attachment.objects.filter(uploaded_at__gte=since)
            .values("scan_result")
            .annotate(n=Count("id"))
        ):
            counts[row["scan_result"]] = counts.get(row["scan_result"], 0) + row["n"]

        blocked = [ScanResult.ERROR, ScanResult.UNSCANNED]
        latest_version = VersionScan.objects.filter(version=OuterRef("pk")).values("result")[:1]
        waiting_versions = (
            DocumentVersion.objects.annotate(latest=Subquery(latest_version))
            .filter(latest__in=blocked)
            .count()
        )
        latest_attachment = AttachmentScan.objects.filter(attachment=OuterRef("pk")).values(
            "result"
        )[:1]
        waiting_attachments = (
            Attachment.objects.annotate(latest=Subquery(latest_attachment))
            .filter(_waiting_q(blocked))
            .count()
        )
    return {
        "days": days,
        "scanned": sum(counts.values()),
        "clean": counts.get(ScanResult.CLEAN, 0),
        "infected": counts.get(ScanResult.INFECTED, 0),
        "errors": counts.get(ScanResult.ERROR, 0),
        "waiting": waiting_versions + waiting_attachments,
    }


def _waiting_q(blocked):
    """An attachment is waiting when its newest rescan, or (with none) its
    first scan, does not allow a download."""
    from django.db.models import Q

    return Q(latest__in=blocked) | Q(latest__isnull=True, scan_result__in=blocked)
