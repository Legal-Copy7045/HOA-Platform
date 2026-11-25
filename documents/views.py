from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from audit.services import record_event
from community.models import Lot
from core import policy
from documents import services
from documents.models import Category, Document, DocumentVersion, Visibility


class NewDocumentForm(forms.Form):
    title = forms.CharField(max_length=200)
    category = forms.ChoiceField(choices=Category.choices)
    visibility = forms.ChoiceField(choices=Visibility.choices)
    lot = forms.ModelChoiceField(
        queryset=Lot.objects.none(), required=False, help_text="Only for lot-specific documents."
    )
    description = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))
    file = forms.FileField()

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["lot"].queryset = Lot.objects.for_user(user)


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
                messages.error(request, str(exc))
            else:
                messages.success(request, "Document uploaded.")
                return redirect("document_detail", pk=document.pk)
        else:
            messages.error(request, "Check the form: a title, category and file are needed.")
        return redirect("documents")
    documents = Document.objects.for_user(request.user).select_related("current_version", "lot")
    if not can_manage:
        documents = documents.filter(archived=False)
    groups: dict[str, list] = {}
    for d in documents:
        groups.setdefault(d.get_category_display(), []).append(d)
    return render(
        request,
        "documents/library.html",
        {"groups": groups, "form": form, "can_manage": can_manage},
    )


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
        filename=version.original_filename,
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
