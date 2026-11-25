from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from audit.services import record_event
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
    lot = forms.ModelChoiceField(
        queryset=Lot.objects.none(), required=False, help_text="Only for lot-specific documents."
    )
    description = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))
    file = forms.FileField()

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["lot"].queryset = Lot.objects.for_user(user)
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
    groups: dict[str, list] = {}
    for d in sorted(
        documents,
        key=lambda d: (
            pinned.index(d.category) if d.category in pinned else len(pinned),
            order.get(d.category, 99),
        ),
    ):
        groups.setdefault(d.get_category_display(), []).append(d)
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
    versions = document.versions.select_related("uploaded_by") if see_history else []
    return render(
        request,
        "documents/detail.html",
        {
            "document": document,
            "versions": versions,
            "can_manage": can_manage,
            "version_form": VersionForm(),
            "folders": services.folders_for(request.user) if can_manage else [],
        },
    )


def _send(request, document, version):
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
