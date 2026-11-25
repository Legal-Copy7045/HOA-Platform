"""Neighbour sign-off pages: the panel on an ARC request, the neighbour's
portal list, and the public single-use link."""

import datetime

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from arc import signoffs
from arc.models import (
    ArcRequest,
    ScanReading,
    SignoffChannel,
    SignoffProposal,
    SignoffRequest,
)
from community.models import Lot
from core import policy
from documents import attachments
from documents.services import DocumentError

SIGNED_FORM = "Signed sign-off form"


def _arc(request, pk) -> ArcRequest:
    return get_object_or_404(ArcRequest.objects.for_user(request.user), pk=pk)


def _flash(request, fn, ok):
    try:
        result = fn()
    except (signoffs.SignoffError, DocumentError) as exc:
        messages.error(request, str(exc))
        return None
    messages.success(request, ok)
    return result


def panel_context(user, req: ArcRequest) -> dict:
    """For the sign-off panel on the ARC detail page."""
    can_arrange = signoffs.can_arrange(user, req)
    staff = policy.can(user, "arc.intake")
    from arc.services import is_recused

    staff = staff and not is_recused(user, req)
    data = {
        "signoff": signoffs.summary(req),
        "signoff_can_arrange": can_arrange and req.state in signoffs.OPEN_FOR_SIGNOFF,
        "signoff_staff": staff,
        "signoff_neighbours": signoffs.neighbours_of(req.lot),
        "signoff_has_reader": bool(_gemini_configured()),
    }
    if staff:
        data["signoff_readings"] = ScanReading.objects.filter(arc_request=req).order_by("-id")[:5]
        data["signoff_proposals"] = SignoffProposal.objects.filter(
            reading__arc_request=req, state="pending"
        ).select_related("signoff__lot")
        data["signoff_forms"] = attachments.attachments_for(req).filter(label=SIGNED_FORM)
    return data


def _gemini_configured():
    from django.conf import settings

    return settings.GEMINI_API_KEY


@login_required
@require_POST
def arc_signoff_action(request, pk, action):
    req = _arc(request, pk)
    user = request.user
    if action == "prepare":
        _flash(
            request,
            lambda: signoffs.prepare_signoffs(req, actor=user, request=request),
            "Neighbours added from the development plan.",
        )
    elif action == "add_lot":
        # Lot numbers and addresses are not private within the community;
        # the service checks the caller may arrange sign-offs for this request.
        lot = get_object_or_404(
            Lot.objects.unscoped("pick a neighbour lot"),
            number=int(request.POST.get("lot_number") or 0),
        )
        _flash(
            request,
            lambda: signoffs.add_signoff_lot(req, lot, actor=user, request=request),
            "Lot added.",
        )
    elif action == "add_address":
        _flash(
            request,
            lambda: signoffs.add_signoff_address(
                req,
                name=request.POST.get("name", ""),
                address=request.POST.get("address", ""),
                email=request.POST.get("email", ""),
                actor=user,
                request=request,
            ),
            "Address added.",
        )
    elif action == "withdraw":
        signoff = get_object_or_404(_of(req), pk=request.POST.get("signoff"))
        _flash(
            request,
            lambda: signoffs.withdraw_signoff(signoff, actor=user, request=request),
            "No longer asked.",
        )
    elif action == "send":
        sent = _flash(
            request,
            lambda: signoffs.send_signoffs(req, actor=user, request=request),
            "Requests sent.",
        )
        if sent and sent["no_contact"]:
            messages.warning(
                request,
                f"{sent['no_contact']} neighbour(s) have no email on record: "
                "print the sign-off form for them.",
            )
    elif action == "paper":
        if not policy.can(user, "arc.intake"):
            raise PermissionDenied
        signoff = get_object_or_404(_of(req), pk=request.POST.get("signoff"))
        _flash(
            request,
            lambda: signoffs.respond(
                signoff,
                position=request.POST.get("position", ""),
                signer_name=request.POST.get("signer_name", ""),
                comment=request.POST.get("comment", ""),
                channel=SignoffChannel.PAPER,
                signed_on=_date(request.POST.get("signed_on")),
                actor=user,
                request=request,
            ),
            "Paper sign-off recorded.",
        )
    elif action == "upload_form":
        upload = request.FILES.get("file")
        if upload is None:
            messages.error(request, "Choose the scanned form.")
        else:
            if not signoffs.can_arrange(user, req):
                raise PermissionDenied
            _flash(
                request,
                lambda: attachments.attach(
                    req, upload, actor=user, label=SIGNED_FORM, request=request
                ),
                "Signed form uploaded.",
            )
    elif action == "read_form":
        _flash(
            request,
            lambda: signoffs.request_reading(
                req, int(request.POST.get("attachment") or 0), actor=user, request=request
            ),
            "Reading the form. Proposals appear here in a minute; refresh the page.",
        )
    elif action in ("confirm", "reject"):
        proposal = get_object_or_404(
            SignoffProposal, pk=request.POST.get("proposal"), reading__arc_request=req
        )
        if action == "reject":
            signoffs.reject_proposal(proposal, actor=user)
            messages.success(request, "Proposal discarded.")
        else:
            if request.POST.get("signoff"):
                target = get_object_or_404(_of(req), pk=request.POST["signoff"])
                SignoffProposal.objects.filter(pk=proposal.pk).update(signoff=target)
                proposal.refresh_from_db()
            _flash(
                request,
                lambda: signoffs.confirm_proposal(
                    proposal,
                    actor=user,
                    position=request.POST.get("position") or None,
                    signer_name=request.POST.get("signer_name") or None,
                    request=request,
                ),
                "Sign-off recorded from the scanned form.",
            )
    else:
        raise Http404
    return redirect(f"{_detail_url(req)}#signoffs")


def _of(req):
    """Sign-offs of an ARC request the caller already fetched through scoping."""
    return SignoffRequest.objects.unscoped("arc request already scoped").filter(arc_request=req)


def _detail_url(req):
    from django.urls import reverse

    return reverse("arc_detail", args=[req.pk])


def _date(value):
    try:
        return datetime.date.fromisoformat(value) if value else None
    except ValueError:
        return None


@login_required
def arc_signoff_form(request, pk):
    req = _arc(request, pk)
    if not (signoffs.can_arrange(request.user, req) or policy.can(request.user, "arc.view_all")):
        raise PermissionDenied
    return attachments.pdf_response(signoffs.signoff_form_pdf(req), f"ARC-{req.pk}-signoffs.pdf")


# --- The neighbour's side (portal) ---------------------------------------------------


@login_required
def signoffs_mine(request):
    """Sign-offs neighbours have asked of the lots I own."""
    policy.require(request.user, "arc.submit")
    roles = policy.roles_for(request.user)
    rows = (
        SignoffRequest.objects.for_user(request.user)
        .filter(lot_id__in=roles.owned_lot_ids)
        .exclude(state="withdrawn")
        .select_related("arc_request__lot", "lot")
        .order_by("state", "-created_at")
    )
    return render(request, "arc/signoffs_mine.html", {"rows": rows})


def _neighbour_signoff(request, pk) -> SignoffRequest:
    signoff = get_object_or_404(
        SignoffRequest.objects.for_user(request.user).select_related("arc_request__lot"), pk=pk
    )
    if signoff.lot_id not in policy.roles_for(request.user).owned_lot_ids:
        raise Http404  # the applicant sees statuses on their request, not this page
    return signoff


@login_required
def signoff_detail(request, pk):
    signoff = _neighbour_signoff(request, pk)
    if request.method == "POST":
        _flash(
            request,
            lambda: signoffs.respond(
                signoff,
                position=request.POST.get("position", ""),
                signer_name=request.POST.get("signer_name", ""),
                comment=request.POST.get("comment", ""),
                channel=SignoffChannel.PORTAL,
                actor=request.user,
                request=request,
            ),
            "Thank you: your answer has been sent to the committee.",
        )
        return redirect("signoff_detail", pk=signoff.pk)
    return render(
        request,
        "arc/signoff_answer.html",
        _answer_context(signoff, can_answer=signoffs.may_answer_in_portal(request.user, signoff)),
    )


@login_required
def signoff_file(request, pk, file_id):
    signoff = _neighbour_signoff(request, pk)
    return attachments.serve(request, signoff.arc_request, file_id, exclude_labels=(SIGNED_FORM,))


def _answer_context(signoff, *, can_answer, token=None):
    req = signoff.arc_request
    files = attachments.attachments_for(req).exclude(label=SIGNED_FORM)
    return {
        "signoff": signoff,
        "req": req,
        "files": files,
        "can_answer": can_answer,
        "token": token,
        "last": signoff.responses.order_by("created_at", "id").last(),
    }


# --- The single-use emailed link (no login) ------------------------------------------


def signoff_link(request, token):
    signoff = signoffs.signoff_for_token(token)
    if signoff is None:
        return render(request, "arc/signoff_link_expired.html", status=404)
    if request.method == "POST":
        try:
            signoffs.respond(
                signoff,
                position=request.POST.get("position", ""),
                signer_name=request.POST.get("signer_name", ""),
                comment=request.POST.get("comment", ""),
                channel=SignoffChannel.LINK,
                request=request,
            )
        except signoffs.SignoffError as exc:
            messages.error(request, str(exc))
            return render(
                request,
                "arc/signoff_answer.html",
                _answer_context(signoff, can_answer=True, token=token),
            )
        return render(request, "arc/signoff_thanks.html")
    return render(
        request, "arc/signoff_answer.html", _answer_context(signoff, can_answer=True, token=token)
    )


def signoff_link_file(request, token, file_id):
    signoff = signoffs.signoff_for_token(token)
    if signoff is None:
        raise Http404
    return attachments.serve(request, signoff.arc_request, file_id, exclude_labels=(SIGNED_FORM,))


# --- Board: the adjacency list ---------------------------------------------------------


@login_required
def neighbours_page(request):
    policy.require(request.user, "arc.configure")
    lots = list(Lot.objects.for_user(request.user).order_by("number"))
    by_number = {lot.number: lot for lot in lots}
    if request.method == "POST":
        lot = by_number.get(int(request.POST.get("lot") or 0))
        if lot is None:
            raise Http404
        wanted = []
        for part in (request.POST.get("neighbours") or "").replace(",", " ").split():
            if part.isdigit() and int(part) in by_number:
                wanted.append(by_number[int(part)])
        signoffs.set_neighbours(lot, wanted, actor=request.user, request=request)
        messages.success(request, f"Neighbours of lot {lot.number} saved (both ways).")
        return redirect("arc_neighbours")
    rows = [(lot, [n.number for n in signoffs.neighbours_of(lot)]) for lot in lots]
    return render(request, "arc/neighbours.html", {"rows": rows})
