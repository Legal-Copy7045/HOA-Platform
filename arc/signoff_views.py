"""Neighbor acknowledgement pages: the panel on an ARC request, the neighbor's
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
from core.scoping import own_records
from documents import attachments
from documents.services import DocumentError

SIGNED_FORM = "Signed acknowledgement form"
# Forms uploaded before the rename keep their old label: never show either to neighbors.
SIGNED_FORM_LABELS = (SIGNED_FORM, "Signed sign-off form")


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
        "signoff_neighbors": signoffs.neighbors_of(req.lot),
        "signoff_has_reader": bool(_gemini_configured()),
        "signoff_board": signoffs.is_board_side(user, req),
        "signoff_lot_choices": _lot_choices(req) if can_arrange else [],
    }
    if staff:
        data["signoff_readings"] = ScanReading.objects.filter(arc_request=req).order_by("-id")[:5]
        data["signoff_proposals"] = SignoffProposal.objects.filter(
            reading__arc_request=req, state="pending"
        ).select_related("signoff__lot")
        data["signoff_forms"] = attachments.attachments_for(req).filter(
            label__in=SIGNED_FORM_LABELS
        )
    return data


def _lot_choices(req) -> list[tuple[int, str]]:
    """Every other lot as "136 Oak Creek Drive (Pat & Sam Example)": the
    address's first line and the owners on the deed (never tenants or emails)."""
    from core.scoping import system_access

    with system_access("neighbor lot choices"):
        lots = list(Lot.objects.exclude(pk=req.lot_id).order_by("street_address", "number"))
    names = signoffs.owner_names([lot.pk for lot in lots])
    out = []
    for lot in lots:
        first = (lot.street_address or "").splitlines()[0] if lot.street_address else ""
        label = first or f"Lot {lot.number}"
        if names.get(lot.pk):
            label += f" ({names[lot.pk]})"
        out.append((lot.pk, label))
    return out


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
            "Neighbors added from the development plan.",
        )
    elif action == "add_lot":
        # Lot numbers and addresses are not private within the community;
        # the service checks the caller may arrange sign-offs for this request.
        lot = get_object_or_404(
            Lot.objects.unscoped("pick a neighbor lot"), pk=int(request.POST.get("lot") or 0)
        )
        _flash(
            request,
            lambda: signoffs.add_signoff_lot(req, lot, actor=user, request=request),
            "Neighbor added.",
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
            "Reminders sent.",
        )
        if sent and sent["no_contact"]:
            messages.warning(
                request,
                f"{sent['no_contact']} neighbor(s) have no email on record: "
                "print the acknowledgement form for them.",
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
            "Paper acknowledgement recorded.",
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
            target = None
            if request.POST.get("signoff"):
                target = get_object_or_404(_of(req), pk=request.POST["signoff"])
            _flash(
                request,
                lambda: signoffs.confirm_proposal(
                    proposal,
                    actor=user,
                    position=request.POST.get("position") or None,
                    signer_name=request.POST.get("signer_name") or None,
                    retarget=target,
                    request=request,
                ),
                "Acknowledgement recorded from the scanned form.",
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


# --- The neighbor's side (portal) ---------------------------------------------------


def asked_of_me(user):
    """Acknowledgements neighbors have asked of the lots this person owns
    (the owner side only, also for a board member: SEC-001 owner era)."""
    roles = policy.roles_for(user)
    return (
        own_records(SignoffRequest.objects.for_user(user), user)
        .filter(lot_id__in=roles.owned_lot_ids)
        .exclude(state="withdrawn")
    )


def asked_of_me_waiting(user) -> int:
    """How many are still waiting for this person's answer (the tab count)."""
    from arc.models import NEIGHBOR_VISIBLE_STATES

    return (
        asked_of_me(user)
        .filter(
            state="pending",
            sent_at__isnull=False,
            arc_request__state__in=NEIGHBOR_VISIBLE_STATES,
        )
        .count()
    )


@login_required
def signoffs_mine(request):
    """The "Asked of me" tab of the owner's ARC page: sign-offs neighbors
    have asked of the lots I own."""
    policy.require(request.user, "arc.submit")
    rows = list(
        asked_of_me(request.user)
        .select_related("arc_request__lot", "lot")
        .order_by("state", "-created_at")
    )
    for row in rows:
        row.asked_because = signoffs.asked_because(row)
        row.respond_by = signoffs.respond_by(row) if row.state == "pending" else None
    return render(
        request,
        "arc/signoffs_mine.html",
        {"rows": rows, "arc_tab": "asked", "asked_count": asked_of_me_waiting(request.user)},
    )


def _neighbor_signoff(request, pk) -> SignoffRequest:
    # The owner side only, also for a board member: acknowledgements sent
    # since this person owns the lot, on requests still open (SEC-001).
    signoff = get_object_or_404(
        own_records(SignoffRequest.objects.for_user(request.user), request.user).select_related(
            "arc_request__lot"
        ),
        pk=pk,
    )
    if signoff.lot_id not in policy.roles_for(request.user).owned_lot_ids:
        raise Http404  # the applicant sees statuses on their request, not this page
    return signoff


@login_required
def signoff_detail(request, pk):
    signoff = _neighbor_signoff(request, pk)
    can_answer = signoffs.may_answer_in_portal(request.user, signoff)
    posted = {}
    error = ""
    if request.method == "POST":
        posted = {
            "position": request.POST.get("position", ""),
            "comment": request.POST.get("comment", ""),
            "signer_name": request.POST.get("signer_name", ""),
        }
        try:
            signoffs.respond(
                signoff,
                **posted,
                channel=SignoffChannel.PORTAL,
                actor=request.user,
                request=request,
            )
        except (signoffs.SignoffError, DocumentError) as exc:
            # Shown again with what the neighbor chose and typed.
            error = str(exc)
        else:
            messages.success(
                request, "Thank you. Your acknowledgement has been sent to the committee."
            )
            return redirect("signoff_detail", pk=signoff.pk)
    context = _answer_context(signoff, can_answer=can_answer)
    context.update({"posted": posted, "error": error})
    return render(request, "arc/signoff_answer.html", context)


@login_required
def signoff_file(request, pk, file_id):
    signoff = _neighbor_signoff(request, pk)
    return attachments.serve(
        request, signoff.arc_request, file_id, exclude_labels=SIGNED_FORM_LABELS
    )


def _answer_context(signoff, *, can_answer, token=None):
    req = signoff.arc_request
    files = attachments.attachments_for(req).exclude(label__in=SIGNED_FORM_LABELS)
    from documents.services import scan_allows_download

    files = list(files)
    for f in files:
        f.thumbnail = f.content_type.startswith("image/") and scan_allows_download(
            attachments.current_scan(f)
        )
    return {
        "signoff": signoff,
        "req": req,
        "files": files,
        "can_answer": can_answer,
        "token": token,
        "last": signoff.responses.order_by("created_at", "id").last(),
        "asked_because": signoffs.asked_because(signoff),
        "respond_by": signoffs.respond_by(signoff),
        "contact": signoffs.contact_line(),
    }


# --- The single-use emailed link (no login) ------------------------------------------


@login_required
def signoff_link(request, token):
    """Links emailed before sign-in became mandatory (D24): after signing in,
    an owner of the neighboring lot goes to their review page. Nobody sees
    anything without signing in."""
    signoff = signoffs.signoff_for_token(token)
    if signoff is None or signoff.lot_id not in policy.roles_for(request.user).owned_lot_ids:
        return render(
            request,
            "arc/signoff_link_expired.html",
            {"contact": signoffs.contact_line()},
            status=404,
        )
    return redirect("signoff_detail", pk=signoff.pk)


@login_required
def signoff_link_file(request, token, file_id):
    signoff = signoffs.signoff_for_token(token)
    if signoff is None or signoff.lot_id not in policy.roles_for(request.user).owned_lot_ids:
        raise Http404
    return redirect("signoff_file", pk=signoff.pk, file_id=file_id)


# --- Board: the adjacency list ---------------------------------------------------------


@login_required
def neighbors_page(request):
    """Board: each lot's left and right neighbors and other affected lots."""
    policy.require(request.user, "arc.configure")
    lots = list(Lot.objects.for_user(request.user).order_by("number"))
    by_number = {lot.number: lot for lot in lots}

    def pick(value):
        value = (value or "").strip()
        return by_number.get(int(value)) if value.isdigit() else None

    def wanted(suffix):
        others = [
            by_number[int(p)]
            for p in (request.POST.get(f"others{suffix}") or "").replace(",", " ").split()
            if p.isdigit() and int(p) in by_number
        ]
        return (
            pick(request.POST.get(f"left{suffix}")),
            pick(request.POST.get(f"right{suffix}")),
            others,
        )

    def save(lot, left, right, others):
        signoffs.set_neighbors(
            lot, left=left, right=right, others=others, actor=request.user, request=request
        )

    all_rows = any(k.startswith("left_") for k in request.POST)
    if request.method == "POST" and (request.POST.get("save_all") or all_rows):
        # Every row is in one form: "Save all", or a row's own Save, saves
        # each row that changed (and audits it), so no edit is lost.
        changed = []
        for lot in lots:
            if f"left_{lot.number}" not in request.POST:
                continue
            left, right, others = wanted(f"_{lot.number}")
            sides = signoffs.neighbor_sides(lot)
            if (left, right, sorted(o.pk for o in others)) != (
                sides["left"],
                sides["right"],
                sorted(o.pk for o in sides["other"]),
            ):
                save(lot, left, right, others)
                changed.append(lot.number)
        messages.success(
            request,
            f"Neighbors saved for {len(changed)} lot{'s' if len(changed) != 1 else ''}."
            if changed
            else "No changes to save.",
        )
        row = pick(request.POST.get("lot"))
        return redirect(_neighbors_url() + (f"#lot-{row.number}" if row else ""))
    if request.method == "POST":
        lot = pick(request.POST.get("lot"))
        if lot is None:
            raise Http404
        save(lot, *wanted(""))
        messages.success(request, f"Neighbors of lot {lot.number} saved.")
        return redirect(f"{_neighbors_url()}#lot-{lot.number}")
    names = signoffs.owner_names([lot.pk for lot in lots])
    rows = []
    for lot in lots:
        sides = signoffs.neighbor_sides(lot)
        rows.append(
            {
                "lot": lot,
                "owners": names.get(lot.pk, ""),
                "left": sides["left"],
                "right": sides["right"],
                "others": sides["other"],
                "left_name": names.get(sides["left"].pk, "") if sides["left"] else "",
                "right_name": names.get(sides["right"].pk, "") if sides["right"] else "",
            }
        )
    return render(request, "arc/neighbors.html", {"rows": rows})


def _neighbors_url():
    from django.urls import reverse

    return reverse("arc_neighbors")
