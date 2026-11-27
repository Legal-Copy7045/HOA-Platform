"""Records retention page (D72): the schedule, what is due, disposals waiting
for a second board member, legal holds and past disposals. Board only."""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from core import policy
from documents import retention
from documents.models import (
    Category,
    DisposalRequest,
    Document,
    DocumentDisposal,
    LegalHold,
    RetentionScan,
)


def _page(request, *, errors=None, reason=""):
    return render(
        request,
        "documents/records_retention.html",
        {
            "schedule": retention.schedule(),
            "preview": retention.preview(),
            "requests": retention.pending_requests(),
            "holds": retention.active_holds().select_related("document", "placed_by"),
            "disposals": DocumentDisposal.objects.select_related(
                "version", "document", "proposed_by", "confirmed_by"
            )[:50],
            "last_scan": RetentionScan.objects.first(),
            "categories": Category.choices,
            "errors": errors or [],
            "reason": reason,
        },
    )


@login_required
def records_retention(request):
    policy.require(request.user, retention.PERMISSION)
    return _page(request)


@login_required
@require_POST
def disposal_propose(request):
    policy.require(request.user, retention.PERMISSION)
    reason = request.POST.get("reason", "")
    try:
        retention.propose(
            request.POST.getlist("version"), reason=reason, actor=request.user, request=request
        )
    except (retention.RetentionError, ValueError) as exc:
        message = str(exc) if isinstance(exc, retention.RetentionError) else "Choose files."
        return _page(request, errors=[message], reason=reason)
    messages.success(request, "Disposal proposed. A different board member must confirm it.")
    return redirect("records_retention")


@login_required
@require_POST
def disposal_decide(request, pk):
    policy.require(request.user, retention.PERMISSION)
    req = get_object_or_404(DisposalRequest, pk=pk)
    approve = request.POST.get("decision") == "confirm"
    try:
        n = retention.decide(
            req,
            approve=approve,
            note=request.POST.get("note", ""),
            actor=request.user,
            request=request,
        )
    except retention.RetentionError as exc:
        return _page(request, errors=[str(exc)])
    if approve:
        messages.success(request, f"Disposed of {n} file{'s' if n != 1 else ''}. Records kept.")
    else:
        messages.success(request, "Disposal declined. Nothing was removed.")
    return redirect("records_retention")


@login_required
@require_POST
def hold_place(request):
    policy.require(request.user, retention.PERMISSION)
    document = None
    raw = request.POST.get("document", "")
    if raw:
        if not raw.isdigit():
            raise Http404
        document = get_object_or_404(Document.objects.for_user(request.user), pk=int(raw))
    try:
        retention.place_hold(
            document=document,
            category="" if document else request.POST.get("category", ""),
            reason=request.POST.get("reason", ""),
            actor=request.user,
            request=request,
        )
    except retention.RetentionError as exc:
        if document is not None:
            messages.error(request, str(exc))
            return redirect("document_detail", pk=document.pk)
        return _page(request, errors=[str(exc)])
    messages.success(request, "Legal hold placed. Nothing it covers can be disposed of.")
    if document is not None:
        return redirect("document_detail", pk=document.pk)
    return redirect("records_retention")


@login_required
@require_POST
def hold_release(request, pk):
    policy.require(request.user, retention.PERMISSION)
    hold = get_object_or_404(LegalHold, pk=pk)
    try:
        retention.release_hold(
            hold, reason=request.POST.get("reason", ""), actor=request.user, request=request
        )
    except retention.RetentionError as exc:
        return _page(request, errors=[str(exc)])
    messages.success(request, "Legal hold released.")
    return redirect("records_retention")
