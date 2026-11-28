import datetime

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from audit.services import record_event
from community.home_picker import HomeField
from community.models import Lot
from core import features, policy
from documents import services
from documents.models import Category, Document, DocumentVersion, Folder, Visibility
from documents.validation import download_filename


class NewDocumentForm(forms.Form):
    title = forms.CharField(max_length=200)
    category = forms.ChoiceField(choices=Category.choices)
    folder = forms.ModelChoiceField(
        queryset=Folder.objects.none(),
        required=False,
        help_text="A filed document takes the folder's visibility.",
    )
    visibility = forms.ChoiceField(
        choices=[c for c in Visibility.choices if c[0] != "committee"],
        help_text="Used only when no folder is chosen.",
    )
    # Picked by street address (D96).
    lot = HomeField(
        required=False,
        label="Home",
        help_text="Only for documents about one home: type part of its address.",
    )
    description = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))
    dated_on = forms.DateField(
        required=False,
        label="Document date",
        widget=forms.DateInput(attrs={"type": "date"}),
        help_text="Its own date (a statement's period end, a signing date). Blank: today. "
        "Retention periods count from it.",
    )
    file = forms.FileField()

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["lot"].queryset = Lot.objects.for_user(user).order_by(
            "street_address", "number"
        )
        self.fields["folder"].queryset = services.folders_for(user)


class VersionForm(forms.Form):
    file = forms.FileField()
    note = forms.CharField(max_length=200, required=False)


@login_required
def library(request):
    policy.require(request.user, "document.view_owner")
    can_manage = policy.can(request.user, "document.manage")
    form = NewDocumentForm(request.POST or None, request.FILES or None, user=request.user)
    if request.method == "POST":
        policy.require(request.user, "document.manage")
        if form.is_valid():
            try:
                document = services.create_document(
                    actor=request.user, request=request, **form.cleaned_data
                )
            except services.DocumentError as exc:
                form.add_error(None, str(exc))
            else:
                messages.success(request, "Document uploaded.")
                return redirect("document_detail", pk=document.pk)
        # Invalid: shown again with what was typed (the file must be chosen again).
    documents = Document.objects.for_user(request.user).select_related(
        "current_version", "lot", "folder"
    )
    if not can_manage:
        documents = documents.filter(archived=False)
    folders = list(services.folders_for(request.user).filter(parent__isnull=True))
    current = None
    if request.GET.get("folder"):
        current = services.folders_for(request.user).filter(pk=request.GET["folder"]).first()
        if current is None:
            raise Http404
        documents = documents.filter(folder=current)
    q = (request.GET.get("q") or "").strip()
    documents = services.search_documents(documents, q)
    # Governing documents (Declaration, bylaws) first, then rules and
    # policies, then the rest in the usual category order.
    order = {value: i for i, value in enumerate(Category.values)}
    pinned = (Category.GOVERNING, Category.POLICIES)
    from governance.packets import FOLDER_NAMES

    packet_folders = {name for name, _ in FOLDER_NAMES.values()}
    groups: dict[str, list] = {}
    for d in sorted(
        documents,
        key=lambda d: (
            pinned.index(d.category) if d.category in pinned else len(pinned),
            order.get(d.category, 99),
        ),
    ):
        # Packets sit with their folder's name, not under "Other" (WT-017).
        in_packets = d.folder_id and d.folder.parent_id is None and d.folder.name in packet_folders
        groups.setdefault(d.folder.name if in_packets else d.get_category_display(), []).append(d)
    return render(
        request,
        "documents/library.html",
        {
            "groups": groups,
            "form": form,
            "can_manage": can_manage,
            "folders": folders,
            "current": current,
            "subfolders": current.children.all() if current else [],
            "q": q,
            "folder_visibilities": [c for c in Visibility.choices if c[0] in ("owners", "board")],
            "pinned_group": Category.GOVERNING.label,
            "can_ask_records": policy.can(request.user, "records_request.submit")
            and features.url_name_enabled("records_requests", request),
        },
    )


@login_required
@require_POST
def folder_create(request):
    policy.require(request.user, "document.manage")
    parent = None
    if request.POST.get("parent"):
        parent = get_object_or_404(services.folders_for(request.user), pk=request.POST["parent"])
    try:
        folder = services.create_folder(
            name=request.POST.get("name", ""),
            visibility=parent.visibility if parent else request.POST.get("visibility", ""),
            parent=parent,
            committee=parent.committee if parent else None,
            actor=request.user,
            request=request,
        )
    except services.DocumentError as exc:
        messages.error(request, str(exc))
        return redirect("documents")
    messages.success(request, "Folder created.")
    return redirect(f"{reverse('documents')}?folder={folder.pk}")


@login_required
@require_POST
def document_move(request, pk):
    policy.require(request.user, "document.manage")
    document = get_object_or_404(Document.objects.for_user(request.user), pk=pk)
    folder = None
    if request.POST.get("folder"):
        folder = get_object_or_404(services.folders_for(request.user), pk=request.POST["folder"])
    try:
        services.move_document(document, folder, actor=request.user, request=request)
    except services.DocumentError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Document filed.")
    return redirect("document_detail", pk=document.pk)


@login_required
@require_POST
def committee_upload(request, pk):
    """A committee member adds a document to their committee's folder."""
    from governance.models import Committee
    from governance.services import can_view_committee

    committee = get_object_or_404(Committee, pk=pk)
    if not can_view_committee(request.user, committee):
        raise Http404
    upload = request.FILES.get("file")
    if upload is None:
        messages.error(request, "Choose a file.")
        return redirect("committee_detail", pk=committee.pk)
    try:
        services.create_document(
            title=request.POST.get("title", "") or upload.name,
            category=Category.OTHER,
            visibility=Visibility.COMMITTEE,
            file=upload,
            folder=services.committee_folder(committee, actor=request.user),
            actor=request.user,
            request=request,
        )
    except services.DocumentError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Document added to the committee folder.")
    return redirect("committee_detail", pk=committee.pk)


@login_required
def detail(request, pk):
    document = get_object_or_404(Document.objects.for_user(request.user), pk=pk)
    can_manage = policy.can(request.user, "document.manage")
    see_history = policy.can(request.user, "document.view_board")
    record_event("document.viewed", request=request, actor=request.user, target=document)
    versions = list(document.versions.select_related("uploaded_by")) if see_history else []
    from documents import retention
    from documents.models import DocumentDisposal

    disposals = {
        d.version_id: d
        for d in DocumentDisposal.objects.filter(document=document).select_related(
            "confirmed_by", "proposed_by"
        )
    }
    for v in versions:
        v.disposal_record = disposals.get(v.pk)
    can_hold = policy.can(request.user, retention.PERMISSION) and features.enabled(
        "records_retention", request
    )
    return render(
        request,
        "documents/detail.html",
        {
            "document": document,
            "versions": versions,
            "can_manage": can_manage,
            "version_form": VersionForm(),
            "folders": services.folders_for(request.user) if can_manage else [],
            "current_disposed": document.current_version_id in disposals,
            "can_access_history": policy.can(request.user, "document.access_history"),
            "comparable": see_history and sum(1 for v in versions if comparable(v)) >= 2,
            "can_hold": can_hold,
            "holds": retention.holds_for(document) if can_hold else [],
        },
    )


@login_required
@require_POST
def set_date(request, pk):
    policy.require(request.user, "document.manage")
    document = get_object_or_404(Document.objects.for_user(request.user), pk=pk)
    raw = request.POST.get("dated_on", "")
    try:
        dated_on = datetime.date.fromisoformat(raw) if raw else None
    except ValueError:
        messages.error(request, "Enter a date.")
    else:
        services.set_dated_on(document, dated_on=dated_on, actor=request.user, request=request)
        messages.success(request, "Document date saved.")
    return redirect("document_detail", pk=document.pk)


ACCESS_ACTIONS = ("document.viewed", "document.downloaded")
ACCESS_LIMIT = 500


@login_required
def access_history(request, pk):
    """Who opened or downloaded this document, from the audit trail (D72).
    Board and auditor; never the network address."""
    policy.require(request.user, "document.access_history")
    document = get_object_or_404(Document.objects.for_user(request.user), pk=pk)
    from audit.models import AuditEvent

    events = list(
        AuditEvent.objects.filter(
            target_type=Document._meta.label_lower,
            target_id=str(document.pk),
            action__in=ACCESS_ACTIONS,
        ).select_related("actor")[:ACCESS_LIMIT]
    )
    record_event(
        "document.access_history_viewed",
        request=request,
        actor=request.user,
        target=document,
        metadata={"events": len(events)},
    )
    return render(
        request,
        "documents/access_history.html",
        {"document": document, "events": events, "limit": ACCESS_LIMIT},
    )


# Version comparison (D72): plain text files only. Word and PDF files would
# need a parser the portal does not ship (and parsing hostile files is a
# risk of its own), so they are not compared.
COMPARE_TYPES = frozenset({"text/plain", "text/csv"})
COMPARE_MAX_BYTES = 512 * 1024


def comparable(version) -> bool:
    return version.content_type in COMPARE_TYPES and version.size <= COMPARE_MAX_BYTES


def _text(version) -> list[str] | None:
    if not (comparable(version) and services.downloadable(version)):
        return None
    try:
        with version.file.open("rb") as fh:
            raw = fh.read(COMPARE_MAX_BYTES + 1)
    except (OSError, ValueError):
        return None
    if len(raw) > COMPARE_MAX_BYTES:
        return None
    return raw.decode("utf-8", errors="replace").splitlines()


def side_by_side(a: list[str], b: list[str]) -> list[dict]:
    """Rows of the old and new line with their numbers and the kind of
    change, from difflib (the template escapes everything)."""
    import difflib

    rows = []
    matcher = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        old, new = a[i1:i2], b[j1:j2]
        for k in range(max(len(old), len(new))):
            rows.append(
                {
                    "kind": "same" if tag == "equal" else tag,
                    "old": old[k] if k < len(old) else None,
                    "new": new[k] if k < len(new) else None,
                    "old_no": i1 + k + 1 if k < len(old) else None,
                    "new_no": j1 + k + 1 if k < len(new) else None,
                }
            )
    return rows


@login_required
def compare(request, pk):
    """Two versions of a plain-text document side by side (board, manager
    and auditor, who see every version)."""
    policy.require(request.user, "document.view_board")
    document = get_object_or_404(Document.objects.for_user(request.user), pk=pk)
    versions = [v for v in document.versions.all() if comparable(v)]
    if len(versions) < 2:
        raise Http404

    def pick(name, default):
        try:
            number = int(request.GET.get(name, default))
        except ValueError:
            raise Http404 from None
        found = next((v for v in versions if v.number == number), None)
        if found is None:
            raise Http404
        return found

    new = pick("b", versions[0].number)
    older = [v.number for v in versions if v.number < new.number]
    old = pick("a", older[0] if older else versions[-1].number)
    old_text, new_text = _text(old), _text(new)
    rows = None
    if old_text is not None and new_text is not None:
        rows = side_by_side(old_text, new_text)
        record_event(
            "document.compared",
            request=request,
            actor=request.user,
            target=document,
            metadata={"from_version": old.number, "to_version": new.number},
        )
    return render(
        request,
        "documents/compare.html",
        {
            "document": document,
            "versions": versions,
            "old": old,
            "new": new,
            "rows": rows,
            "changed": sum(1 for r in rows or [] if r["kind"] != "same"),
        },
    )


def _send(request, document, version):
    if services.disposed(version):
        messages.error(
            request,
            "This file was disposed of under the records retention schedule. Its record is kept.",
        )
        return redirect("document_detail", pk=document.pk)
    if not services.downloadable(version):
        messages.error(request, "This file can't be downloaded until it passes a malware scan.")
        return redirect("document_detail", pk=document.pk)
    record_event(
        "document.downloaded",
        request=request,
        actor=request.user,
        target=document,
        metadata={"version": version.number},
    )
    response = FileResponse(
        version.file.open("rb"),
        as_attachment=True,
        filename=download_filename(version.original_filename, version.content_type),
        content_type=version.content_type,
    )
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    response["Content-Security-Policy"] = "default-src 'none'; sandbox"
    return response


@login_required
def download(request, pk):
    document = get_object_or_404(Document.objects.for_user(request.user), pk=pk)
    if document.current_version is None:
        raise Http404
    return _send(request, document, document.current_version)


@login_required
def download_version(request, pk, version):
    """Earlier versions: board, manager and auditor only."""
    policy.require(request.user, "document.view_board")
    document = get_object_or_404(Document.objects.for_user(request.user), pk=pk)
    v = get_object_or_404(DocumentVersion, document=document, number=version)
    return _send(request, document, v)


@login_required
@require_POST
def add_version(request, pk):
    policy.require(request.user, "document.manage")
    document = get_object_or_404(Document.objects.for_user(request.user), pk=pk)
    form = VersionForm(request.POST, request.FILES)
    if form.is_valid():
        try:
            services.add_version(document, actor=request.user, request=request, **form.cleaned_data)
        except services.DocumentError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, "New version published. Earlier versions are kept.")
    return redirect("document_detail", pk=document.pk)


@login_required
@require_POST
def archive(request, pk, action):
    policy.require(request.user, "document.manage")
    document = get_object_or_404(Document.objects.for_user(request.user), pk=pk)
    services.set_archived(
        document, archived=action == "archive", actor=request.user, request=request
    )
    return redirect("document_detail", pk=document.pk)
