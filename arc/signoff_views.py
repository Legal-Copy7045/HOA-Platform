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


def _gemini_configured():
    from core import ai

    return bool(ai.api_key())


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
        # Homes and their owners are public within the community (D96);
        # picked by street address, and the service checks the caller may
        # arrange sign-offs for this request.
        from community import home_picker

        try:
            lot = home_picker.resolve(request.POST.get("lot", ""), home_picker.all_homes())
        except home_picker.HomeNotFound as exc:
            messages.error(request, exc.message)
            return redirect("arc_detail", pk=req.pk)
        if lot is None:
            messages.error(request, "Choose a home: type part of its address.")
            return redirect("arc_detail", pk=req.pk)
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
    """Board: each home's left and right neighbors and other affected homes,
    picked by street address (D96; a lot number is still accepted)."""
    from community import home_picker

    policy.require(request.user, "arc.configure")
    lots = list(Lot.objects.for_user(request.user).order_by("number"))
    by_number = {lot.number: lot for lot in lots}

    def row_lot(value):
        # The row being saved is named internally by its lot number.
        value = (value or "").strip()
        return by_number.get(int(value)) if value.isdigit() else None

    def wanted(suffix):
        """(left, right, others, typed values, errors) from the posted text."""
        values = {k: request.POST.get(f"{k}{suffix}", "") for k in ("left", "right", "others")}
        errors = []
        sides = {}
        for key, name in (("left", "Left neighbor"), ("right", "Right neighbor")):
            try:
                sides[key] = home_picker.resolve(values[key], lots)
            except home_picker.HomeNotFound as exc:
                sides[key] = None
                errors.append(f"{name}: {exc.message}")
        others, other_errors = home_picker.resolve_many(values["others"], lots)
        errors += [f"Also affected: {e}" for e in other_errors]
        return sides["left"], sides["right"], others, values, errors

    def save(lot, left, right, others):
        signoffs.set_neighbors(
            lot, left=left, right=right, others=others, actor=request.user, request=request
        )

    posted = {}  # lot pk -> (typed values, errors) for a form that needs fixing
    all_rows = any(k.startswith("left_") for k in request.POST)
    if request.method == "POST" and (request.POST.get("save_all") or all_rows):
        # Every row is in one form: "Save all" saves each row that changed
        # (and audits it). A row whose text names no home stops the save and
        # the page comes back with every typed value and the errors.
        plans = []
        for lot in lots:
            if f"left_{lot.number}" not in request.POST:
                continue
            left, right, others, values, errors = wanted(f"_{lot.number}")
            if errors:
                posted[lot.pk] = (values, errors)
                continue
            posted.setdefault(lot.pk, (values, []))
            sides = signoffs.neighbor_sides(lot)
            if (left, right, sorted(o.pk for o in others)) != (
                sides["left"],
                sides["right"],
                sorted(o.pk for o in sides["other"]),
            ):
                plans.append((lot, left, right, others))
        if not any(errs for _, errs in posted.values()):
            for plan in plans:
                save(*plan)
            messages.success(
                request,
                f"Neighbors saved for {len(plans)} home{'s' if len(plans) != 1 else ''}."
                if plans
                else "No changes to save.",
            )
            row = row_lot(request.POST.get("lot"))
            return redirect(_neighbors_url() + (f"#lot-{row.number}" if row else ""))
        messages.error(request, "Nothing was saved: check the homes marked below.")
    elif request.method == "POST":
        lot = row_lot(request.POST.get("lot"))
        if lot is None:
            raise Http404
        left, right, others, values, errors = wanted("")
        if not errors:
            save(lot, left, right, others)
            messages.success(request, f"Neighbors of {lot.label} saved.")
            # Back to the list it was opened from (WT-035: one home at a time).
            back = "?show=missing" if request.POST.get("show") == "missing" else ""
            return redirect(f"{_neighbors_url()}{back}#home-{lot.number}")
        posted[lot.pk] = (values, errors)
    names = signoffs.owner_names([lot.pk for lot in lots])
    dup = home_picker._duplicates(lots)
    rows = []
    for lot in lots:
        sides = signoffs.neighbor_sides(lot)
        values, errors = posted.get(lot.pk, (None, []))
        if values is None:
            values = {
                "left": home_picker.picker_value(sides["left"], dup),
                "right": home_picker.picker_value(sides["right"], dup),
                "others": "; ".join(home_picker.picker_value(o, dup) for o in sides["other"]),
            }
        rows.append(
            {
                "lot": lot,
                "owners": names.get(lot.pk, ""),
                "left": sides["left"],
                "right": sides["right"],
                "others": sides["other"],
                "left_name": names.get(sides["left"].pk, "") if sides["left"] else "",
                "right_name": names.get(sides["right"].pk, "") if sides["right"] else "",
                "missing": not (sides["left"] and sides["right"]),
                "values": values,
                "errors": errors,
                "left_field": f"left_{lot.number}",
                "right_field": f"right_{lot.number}",
                "others_field": f"others_{lot.number}",
                "left_id": f"nl-{lot.number}",
                "right_id": f"nr-{lot.number}",
                "others_id": f"no-{lot.number}",
            }
        )
    # WT-035: a find box, a Missing neighbors filter and one home at a time
    # (?home=N: three fields and Save, the single-row POST above, the same
    # service as Save all). Phones get the compact list; the bulk form stays
    # for computers. Never logged or audited, like the other find boxes.
    missing = sum(r["missing"] for r in rows)
    total = len(rows)
    show = request.GET.get("show", "") or request.POST.get("show", "")
    if show != "missing":
        show = ""
    q = request.GET.get("q", "").strip()[:100]
    shown = [r for r in rows if r["missing"] or r["errors"]] if show else rows
    if q:
        needle = q.lower()
        shown = [
            r
            for r in shown
            if (q.isdigit() and r["lot"].number == int(q))
            or needle in r["lot"].label.lower()
            or needle in r["owners"].lower()
        ]
    edit = row_lot(request.GET.get("home") or request.POST.get("lot"))
    if request.method == "POST" and all_rows:
        edit = None
    edit_row = next((r for r in rows if edit and r["lot"].pk == edit.pk), None)
    status = 400 if request.method == "POST" else 200
    return render(
        request,
        "arc/neighbors.html",
        {
            "rows": shown,
            "total": total,
            "missing": missing,
            "show": show,
            "q": q,
            "edit": edit_row,
        },
        status=status,
    )


def _neighbors_url():
    from django.urls import reverse

    return reverse("arc_neighbors")
