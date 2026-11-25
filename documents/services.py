from __future__ import annotations

import hashlib

from django.conf import settings
from django.db import transaction
from django.db.models import Max

from audit.services import record_event
from core.policy import require
from core.scoping import system_access
from documents.models import Document, DocumentVersion, ScanResult, VersionScan, Visibility
from documents.scanning import scan
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
    result, scanner = scan(uploaded)
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
        VersionScan.objects.create(version=version, result=result, scanner=scanner)
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
    description: str = "",
    actor,
    request=None,
) -> Document:
    require(actor, "document.manage")
    if visibility == Visibility.LOT and lot is None:
        raise DocumentError("Choose the lot this document belongs to.")
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
            description=description,
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
    latest = version.scans.first()
    return scan_allows_download(latest.result if latest else None)
