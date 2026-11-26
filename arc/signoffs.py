"""ARC neighbor acknowledgements (Milestone 3 phase 7; reworked in Milestone 4).

(The code says "signoff"; everything people see says "acknowledgement".)

* Neighbors come from an adjacency list the board records once from the
  development plan. They are on every request from the start and locked:
  the homeowner cannot remove them, the board can. The homeowner (or the
  board) can add other lots or addresses.
* "Request neighbor acknowledgements" moves the draft to awaiting
  acknowledgements and emails each neighbor a review link. When the last
  one is in (or the board removes the last one waiting), the request goes
  to the board automatically. Neighbors may comment and may object.
* A neighbor who owns a lot here and has a login signs in the portal. Anyone
  else gets a single-use, expiring link by email (only its hash is stored)
  that shows that one request's summary and plans, and nothing else.
* A printable form prefilled from the request can be signed on paper,
  scanned and uploaded; Gemini proposes what each signature says, and a
  person confirms it unless the board switched on automatic confirming.
* Acknowledgements inform the reviewers. They never decide the request:
  that stays with the committee, with recusal.
"""

from __future__ import annotations

import contextlib
import datetime
import hashlib

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone

from arc.models import (
    NEIGHBOR_VISIBLE_STATES,
    ArcRequest,
    ArcState,
    LotNeighbor,
    ScanReading,
    SignoffChannel,
    SignoffProposal,
    SignoffRequest,
    SignoffResponse,
    SignoffState,
)
from audit.services import record_event
from comms.notify import send_update
from community.models import Lot, Ownership
from community.rules import rule
from core import policy
from core.policy import SYSTEM, can, require
from core.scoping import system_access
from core.text import single_line

OPEN_FOR_SIGNOFF = {
    ArcState.DRAFT,
    ArcState.NEIGHBORS,
    ArcState.SUBMITTED,
    ArcState.IN_REVIEW,
    ArcState.APPEAL,
}
MAX_EXTRA = 20


class SignoffError(ValueError):
    pass


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# --- The adjacency list ---------------------------------------------------------------


def neighbors_of(lot: Lot) -> list[Lot]:
    """Left, right, then other affected lots, as the board recorded them."""
    with system_access("neighbors of lot"):
        rows = list(LotNeighbor.objects.filter(lot=lot).select_related("neighbor").order_by("side"))
    order = {"left": 0, "right": 1, "other": 2}
    rows.sort(key=lambda r: (order.get(r.side, 3), r.neighbor.number))
    return [r.neighbor for r in rows]


def neighbor_sides(lot: Lot) -> dict:
    """{"left": Lot|None, "right": Lot|None, "other": [Lot, ...]} for one lot."""
    with system_access("neighbor sides"):
        rows = list(LotNeighbor.objects.filter(lot=lot).select_related("neighbor"))
    out = {"left": None, "right": None, "other": []}
    for r in rows:
        if r.side in ("left", "right"):
            out[r.side] = r.neighbor
        else:
            out["other"].append(r.neighbor)
    out["other"].sort(key=lambda n: n.number)
    return out


def set_neighbors(
    lot: Lot,
    *,
    left: Lot | None = None,
    right: Lot | None = None,
    others=(),
    actor,
    request=None,
) -> dict:
    """Replace one lot's left and right neighbors and other affected lots."""
    require(actor, "arc.configure")
    wanted = []
    seen = set()
    for side, neighbor in [("left", left), ("right", right)] + [("other", o) for o in others]:
        if neighbor is None or neighbor.pk == lot.pk or neighbor.pk in seen:
            continue
        seen.add(neighbor.pk)
        wanted.append((side, neighbor))
    with transaction.atomic(), system_access("set neighbors"):
        LotNeighbor.objects.filter(lot=lot).delete()
        for side, neighbor in wanted:
            LotNeighbor.objects.create(lot=lot, neighbor=neighbor, side=side)
    record_event(
        "arc.neighbors_set",
        actor=actor,
        request=request,
        target=lot,
        metadata={"lot_id": lot.pk, "count": len(wanted)},
    )
    return neighbor_sides(lot)


def owner_names(lot_ids) -> dict[int, str]:
    """Current owners' names per lot, for sign-off lists and the printed form
    (a neighbor's name and address, as on the association's paper form)."""
    with system_access("neighbor names"):
        rows = (
            Ownership.objects.active()
            .filter(lot_id__in=list(lot_ids))
            .select_related("person")
            .order_by("start_date", "pk")
        )
        out: dict[int, list[str]] = {}
        for o in rows:
            out.setdefault(o.lot_id, []).append(o.person.display_name)
    return {k: " & ".join(v) for k, v in out.items()}


# --- Who may arrange sign-offs -------------------------------------------------------


def _is_applicant(actor, req: ArcRequest) -> bool:
    if actor is SYSTEM:
        return False
    roles = policy.roles_for(actor)
    return req.applicant_id == roles.person_id and req.lot_id in roles.owned_lot_ids


def can_arrange(actor, req: ArcRequest) -> bool:
    """The applicant, or ARC intake staff who are not recused."""
    if actor is SYSTEM:
        return True
    if _is_applicant(actor, req) and can(actor, "arc.submit"):
        return True
    from arc.services import is_recused

    return can(actor, "arc.intake") and not is_recused(actor, req)


def is_board_side(actor, req: ArcRequest) -> bool:
    """The board (not recused) may lock and remove neighbors."""
    if actor is SYSTEM:
        return True
    from arc.services import is_recused

    return can(actor, "arc.configure") and not is_recused(actor, req)


def _require_arrange(actor, req: ArcRequest) -> None:
    if not can_arrange(actor, req):
        raise PermissionDenied("signoff")
    if req.state not in OPEN_FOR_SIGNOFF:
        raise SignoffError("Acknowledgements can be arranged only while the request is open.")


def prepare_signoffs(req: ArcRequest, *, actor, request=None) -> int:
    """Add the neighboring lots (from the adjacency list) not already asked."""
    _require_arrange(actor, req)
    created = 0
    with system_access("prepare signoffs"):
        for lot in neighbors_of(req.lot):
            try:
                with transaction.atomic():
                    SignoffRequest.objects.create(
                        arc_request=req, lot=lot, locked=True, created_by=_user(actor)
                    )
                created += 1
            except IntegrityError:
                continue
    if created:
        record_event(
            "arc.signoffs_prepared",
            actor=_user(actor),
            request=request,
            target=req,
            metadata={"count": created},
        )
    return created


def add_signoff_lot(req: ArcRequest, lot: Lot, *, actor, request=None) -> SignoffRequest:
    _require_arrange(actor, req)
    if lot.pk == req.lot_id:
        raise SignoffError("That is the applicant's own home.")
    try:
        with transaction.atomic(), system_access("add signoff lot"):
            existing = SignoffRequest.objects.filter(arc_request=req, lot=lot).first()
            if existing and existing.state == SignoffState.WITHDRAWN:
                # Asked again after being removed: back on the list.
                SignoffRequest.objects.filter(pk=existing.pk).update(
                    state=SignoffState.PENDING, sent_at=None, locked=is_board_side(actor, req)
                )
                existing.refresh_from_db()
                signoff = existing
            else:
                signoff = SignoffRequest.objects.create(
                    arc_request=req,
                    lot=lot,
                    locked=is_board_side(actor, req),
                    created_by=_user(actor),
                )
    except IntegrityError as exc:
        raise SignoffError("That home is already on the list.") from exc
    record_event(
        "arc.signoff_added",
        actor=_user(actor),
        request=request,
        target=req,
        metadata={"kind": "lot"},
    )
    return signoff


def add_signoff_address(
    req: ArcRequest, *, name: str, address: str, email: str = "", actor, request=None
) -> SignoffRequest:
    """An extra address outside the adjacency list (e.g. across the road)."""
    _require_arrange(actor, req)
    name, address = single_line(name)[:200], single_line(address)[:300]
    email = (email or "").strip()
    if not address:
        raise SignoffError("Enter the address.")
    if email:
        from django.core.validators import validate_email
        from django.forms import ValidationError

        try:
            validate_email(email)
        except ValidationError as exc:
            raise SignoffError("Enter a valid email address, or leave it blank.") from exc
    with system_access("add signoff address"):
        if SignoffRequest.objects.filter(arc_request=req, lot__isnull=True).count() >= MAX_EXTRA:
            raise SignoffError("That is the most extra addresses one request can have.")
        signoff = SignoffRequest.objects.create(
            arc_request=req,
            name=name,
            address=address,
            email=email,
            locked=is_board_side(actor, req),
            created_by=_user(actor),
        )
    record_event(
        "arc.signoff_added",
        actor=_user(actor),
        request=request,
        target=req,
        metadata={"kind": "address"},
    )
    return signoff


def withdraw_signoff(signoff: SignoffRequest, *, actor, request=None) -> None:
    req = signoff.arc_request
    _require_arrange(actor, req)
    if signoff.locked and not is_board_side(actor, req):
        raise SignoffError("Only the board can remove this neighbor.")
    with system_access("withdraw signoff"):
        SignoffRequest.objects.filter(pk=signoff.pk, state=SignoffState.PENDING).update(
            state=SignoffState.WITHDRAWN,
            token_hash="",  # nosec B106 - clearing a used link's hash, not a password
        )
    record_event("arc.signoff_withdrawn", actor=_user(actor), request=request, target=req)
    submit_if_acknowledged(req)


def submit_if_acknowledged(req: ArcRequest) -> bool:
    """Once nothing is waiting, a request awaiting acknowledgements goes to
    the board, and the applicant is told."""
    from arc.services import ARC, _waiting_acknowledgements
    from core.workflow import WorkflowError

    req.refresh_from_db()
    if req.state != ArcState.NEIGHBORS or _waiting_acknowledgements(req):
        return False
    try:
        ARC.apply(req, "acknowledged", actor=SYSTEM)
    except WorkflowError:
        return False  # another answer arriving at the same moment already sent it on
    with system_access("applicant email"):
        user = getattr(req.applicant, "user", None)
    if user is not None and user.is_active and user.email:
        link = settings.PORTAL_BASE_URL + reverse("arc_detail", args=[req.pk])
        send_update(
            {user.email},
            subject=f"[{settings.SITE_NAME}] Your neighbors' acknowledgements are in",
            body=(
                "Every neighbor has now acknowledged your architectural request, and it "
                f"has gone to the committee for review.\n\n{link}\n\n-- \n{settings.SITE_NAME}"
            ),
            link=link,
        )
    return True


# --- Sending --------------------------------------------------------------------------


def _owners_of(lot_id: int):
    with system_access("signoff owners"):
        return list(Ownership.objects.active().filter(lot_id=lot_id).select_related("person__user"))


def send_signoffs(req: ArcRequest, *, actor, request=None) -> dict:
    """Email every waiting neighbor who has a portal login a link to their
    review page. The page needs sign-in: there is no anonymous access to any
    part of the portal (owner's rule, D24). Neighbors without a login, and
    addresses outside the community, use the printed form. Emails carry only
    the link and the project's address, never the plans or anyone's details."""
    _require_arrange(actor, req)
    base = settings.PORTAL_BASE_URL
    sent = {"portal": 0, "no_contact": 0}
    messages: list[tuple[str, str]] = []
    # A neighbor is emailed at most once a day, however often the button is
    # pressed (SEC-006).
    recent = timezone.now() - datetime.timedelta(hours=23)
    with transaction.atomic(), system_access("send signoffs"):
        pending = SignoffRequest.objects.select_for_update(of=("self",)).filter(
            arc_request=req, state=SignoffState.PENDING
        )
        for signoff in pending.select_related("lot"):
            if signoff.sent_at and signoff.sent_at > recent:
                continue
            emails = set()
            if signoff.lot_id:
                for o in _owners_of(signoff.lot_id):
                    user = o.person.user
                    if user is not None and user.is_active:
                        emails.add(user.email)
            if not emails:
                sent["no_contact"] += 1
                continue
            link = base + reverse("signoff_detail", args=[signoff.pk])
            for email in emails:
                messages.append((email, link))
                sent["portal"] += 1
            SignoffRequest.objects.filter(pk=signoff.pk).update(sent_at=timezone.now())
    where = single_line(req.lot.street_address)
    subject = f"[{settings.SITE_NAME}] Please review your neighbor's project"
    for email, link in messages:
        body = (
            f"Your neighbor at {where} is applying to the architectural committee for "
            "an exterior project. Before the committee reviews it, each neighbor is asked "
            "to acknowledge it, and may comment or object.\n\n"
            f"Sign in to the portal to review the details and plans, and acknowledge:\n{link}\n\n"
            "Your acknowledgement and any comments go to the committee, which makes the "
            f"decision. If you did not expect this, you can ignore it.\n\n-- \n{settings.SITE_NAME}"
        )
        send_update({email}, subject=subject, body=body, link=link)
    record_event(
        "arc.signoffs_sent",
        actor=_user(actor),
        request=request,
        target=req,
        metadata=sent,
    )
    return sent


# --- Answering ------------------------------------------------------------------------


def signoff_for_token(token: str) -> SignoffRequest | None:
    """The waiting sign-off this single-use link is for, if still valid."""
    if not token or len(token) > 100:
        return None
    with system_access("signoff by token"):
        signoff = (
            SignoffRequest.objects.select_related("arc_request__lot", "lot")
            .filter(token_hash=_hash(token), state=SignoffState.PENDING)
            .first()
        )
    if signoff is None or not signoff.token_expires_at or signoff.token_expires_at < timezone.now():
        return None
    if signoff.arc_request.state not in NEIGHBOR_VISIBLE_STATES:
        return None  # withdrawn or decided: the plans are no longer shown (SEC-005)
    return signoff


def may_answer_in_portal(user, signoff: SignoffRequest) -> bool:
    if signoff.state != SignoffState.PENDING or not signoff.lot_id or not signoff.sent_at:
        return False
    if signoff.arc_request.state not in NEIGHBOR_VISIBLE_STATES:
        return False
    if not can(user, "arc.submit"):  # owners on the deed answer for their lot
        return False
    return signoff.lot_id in policy.roles_for(user).owned_lot_ids


def respond(
    signoff: SignoffRequest,
    *,
    position: str,
    signer_name: str,
    comment: str = "",
    channel: str,
    actor=None,
    signed_on=None,
    evidence_attachment_id=None,
    request=None,
) -> SignoffResponse:
    """Record an answer. Portal: an owner of that lot. Link: whoever holds the
    single-use link (the caller checked it). Paper/AI: staff entering it."""
    if position not in (SignoffState.APPROVED, SignoffState.OBJECTED):
        raise SignoffError("Choose no objection, or object.")
    signer_name = single_line(signer_name)[:200]
    if not signer_name:
        raise SignoffError("Type your name to acknowledge.")
    if (
        position == SignoffState.OBJECTED
        and channel in (SignoffChannel.PORTAL, SignoffChannel.LINK)
        and not (comment or "").strip()
    ):
        raise SignoffError("Please say what you object to.")
    if channel == SignoffChannel.PORTAL:
        if actor is None or not may_answer_in_portal(actor, signoff):
            raise PermissionDenied("signoff")
    elif channel in (SignoffChannel.PAPER, SignoffChannel.AI):
        if actor is not SYSTEM and not can(actor, "arc.intake"):
            raise PermissionDenied("signoff")
        if actor is not SYSTEM:
            from arc.services import is_recused

            if is_recused(actor, signoff.arc_request):
                raise PermissionDenied("recused")
    elif channel != SignoffChannel.LINK:
        raise SignoffError("Unknown channel.")
    with transaction.atomic(), system_access("record signoff"):
        locked = SignoffRequest.objects.select_for_update().get(pk=signoff.pk)
        if locked.state == SignoffState.WITHDRAWN:
            raise SignoffError("This acknowledgement is no longer being asked for.")
        if channel in (SignoffChannel.PORTAL, SignoffChannel.LINK) and locked.state != "pending":
            raise SignoffError("This has already been acknowledged.")
        if locked.arc_request.state not in OPEN_FOR_SIGNOFF:
            raise SignoffError("The request is no longer open for acknowledgements.")
        response = SignoffResponse.objects.create(
            signoff=locked,
            position=position,
            signer_name=signer_name,
            comment=(comment or "").strip()[:2000],
            signed_on=signed_on or timezone.localdate(),
            channel=channel,
            recorded_by=None if actor in (None, SYSTEM) else actor,
            evidence_attachment_id=evidence_attachment_id,
        )
        # The link is single-use: it stops working once answered.
        SignoffRequest.objects.filter(pk=locked.pk).update(
            state=position,
            token_hash="",  # nosec B106 - clearing a used link's hash, not a password
        )
    record_event(
        "arc.signoff_answered",
        actor=None if actor in (None, SYSTEM) else actor,
        request=request,
        target=signoff.arc_request,
        metadata={"position": position, "channel": channel},
    )
    signoff.refresh_from_db()
    submit_if_acknowledged(signoff.arc_request)
    return response


ASKED_BECAUSE = {
    "left": "You were asked because you live next door (left side).",
    "right": "You were asked because you live next door (right side).",
    "other": "You were asked because the project may affect your home.",
}


def asked_because(signoff: SignoffRequest) -> str:
    """Why this neighbor was asked, in a sentence they understand."""
    if signoff.lot_id:
        with system_access("neighbor side"):
            side = (
                LotNeighbor.objects.filter(
                    lot_id=signoff.arc_request.lot_id, neighbor_id=signoff.lot_id
                )
                .values_list("side", flat=True)
                .first()
            )
        if side in ASKED_BECAUSE:
            return ASKED_BECAUSE[side]
    return "You were asked because your neighbor would like your view on the project."


def respond_by(signoff: SignoffRequest):
    """The date neighbors are asked to answer by, when the board set one."""
    days = rule("arc.signoff_respond_days")
    if not days or not signoff.sent_at:
        return None
    return timezone.localtime(signoff.sent_at).date() + datetime.timedelta(days=days)


def contact_line() -> str:
    """Who to contact with questions, as the board recorded it (never hard-coded)."""
    from community.models import CommunityProfile

    profile = CommunityProfile.get()
    return single_line(profile.letter_contact or profile.arc_form_return_to or "")


def summary(req: ArcRequest) -> dict:
    with system_access("signoff summary"):
        rows = list(
            SignoffRequest.objects.filter(arc_request=req)
            .exclude(state=SignoffState.WITHDRAWN)
            .select_related("lot")
            .prefetch_related("responses")
        )
    counts = dict.fromkeys((SignoffState.PENDING, SignoffState.APPROVED, SignoffState.OBJECTED), 0)
    names = owner_names([r.lot_id for r in rows if r.lot_id])
    for r in rows:
        r.owner_names = names.get(r.lot_id, "")
        answers = list(r.responses.all())
        r.last_response = answers[-1] if answers else None
        counts[r.state] = counts.get(r.state, 0) + 1
    return {"rows": rows, "counts": counts}


# --- The printable form ---------------------------------------------------------------


DEFAULT_CONDITIONS = (
    "In order for this review and approval to be considered, you agree that any costs "
    "involved in implementing the requested alteration are yours to bear and in no way give "
    "you any exclusive use of the Common Area; and that should the alteration require any "
    "additional maintenance or upkeep, those future costs will be at your expense."
)
DEFAULT_TYPES = ["Landscape", "Building exterior", "Satellite dish", "Other"]
LOCATIONS = [("front", "Front"), ("side", "Side"), ("rear", "Rear"), ("other", "Other")]


def _box(checked: bool) -> str:
    return "[X]" if checked else "[  ]"


def _blank(value, width=40) -> str:
    text = str(value) if value else ""
    return text if text else "_" * width


def signoff_form_pdf(req: ArcRequest) -> bytes:
    """The association's alteration request form, prefilled from the request:
    owner and address, dates, contact, type, location, scope, materials, the
    left and right neighbors (and any others) with signature lines, the
    attachments reminder, the conditions, and the owner's signature."""
    from arc.models import ArcProjectType
    from community.models import CommunityProfile, ContactProfile
    from core import pdf

    profile = CommunityProfile.get()
    with system_access("alteration form"):
        applicant = req.applicant
        contact = ContactProfile.objects.filter(person=applicant).first()
        email = applicant.user.email if applicant.user_id else applicant.email
        rows = list(
            SignoffRequest.objects.filter(arc_request=req)
            .exclude(state=SignoffState.WITHDRAWN)
            .select_related("lot")
        )
        sides = {}
        for n in LotNeighbor.objects.filter(lot=req.lot):
            sides[n.neighbor_id] = n.side
    names = owner_names([r.lot_id for r in rows if r.lot_id])
    start = req.planned_start.strftime("%B %-d, %Y") if req.planned_start else ""
    finish = req.planned_completion.strftime("%B %-d, %Y") if req.planned_completion else ""
    order = {"left": 0, "right": 1}
    rows.sort(key=lambda r: (order.get(sides.get(r.lot_id), 2), r.pk))
    types = [t.name for t in ArcProjectType.objects.filter(active=True)] or DEFAULT_TYPES
    chosen = (req.project_type or "").strip().lower()

    paras = [
        ("heading", profile.legal_name.upper()),
        (
            "small",
            profile.arc_form_return_to
            or "Please return the completed form to the association's management office.",
        ),
        ("heading", "ALTERATION REQUEST FORM"),
        ("small", f"Reference ARC-{req.pk}. Prefilled from the request in the owners' portal."),
        ("normal", ""),
        ("normal", f"1.  Name of owner requesting approval: {_blank(applicant.display_name)}"),
        (
            "normal",
            f"     Approximate date to begin alteration: {_blank(start, 20)}",
        ),
        (
            "normal",
            f"2.  Address of unit where alteration will occur: {req.lot.legal_label}",
        ),
        (
            "normal",
            f"     Approximate date of completion: {_blank(finish, 20)}",
        ),
        (
            "normal",
            f"3.  Phone of applicant: {_blank(contact.phone if contact else '', 24)}"
            f"     Email address: {_blank(email, 30)}",
        ),
        (
            "normal",
            "4.  Type of alteration:  "
            + "   ".join(f"{_box(t.lower() == chosen)} {t}" for t in types),
        ),
    ]
    if chosen and chosen not in [t.lower() for t in types]:
        paras.append(("normal", f"     If other, please explain: {req.project_type}"))
    paras += [
        (
            "normal",
            "5.  Location (from the nearest unit):  "
            + "   ".join(f"{_box(req.location == v)} {label}" for v, label in LOCATIONS),
        ),
    ]
    if req.location_detail:
        paras.append(("normal", f"     {req.location_detail}"))
    paras += [
        (
            "normal",
            "6.  Scope of alteration (what you are asking to do, with approximate dimensions):",
        ),
        ("small", f"     {req.title}. {req.description}"[:1800]),
        (
            "normal",
            "7.  Materials to be used and contractors (materials or shrubs installed or "
            "removed, and quantities):",
        ),
        ("small", f"     {req.materials}"[:1200] if req.materials else "_" * 88),
        ("normal", ""),
        (
            "normal",
            "8.  Signature of the nearest owners (residing there) to the right and left of "
            "your home, to show they have been made aware of this alteration "
            "(not necessary for like-kind replacement requests).",
        ),
        (
            "small",
            "Note to neighbors: your signature shows you have been notified of this "
            "proposed alteration. Tick no objection, or that you object. If you object, please "
            "also contact the management office, or a board or architectural committee "
            "member, at once.",
        ),
    ]
    for row in rows:
        if row.lot_id:
            who = names.get(row.lot_id, "")
            side = sides.get(row.lot_id)
            heading = (
                (f"{side.title()} neighbor: " if side in ("left", "right") else "Also affected: ")
                + (f"{who}, " if who else "")
                + row.lot.legal_label
            )
        else:
            heading = "Also asked: " + row.label
        paras += [
            ("normal", ""),
            ("normal", heading),
            ("normal", "     [  ] No objection   [  ] I object"),
            (
                "normal",
                "     Name (print): _________________________________   Date: ______________",
            ),
            ("normal", "     Signature: _____________________________________"),
            ("small", f"     Reference: SO-{row.pk}"),
        ]
    if not rows:
        for side in ("Left", "Right"):
            paras += [
                ("normal", ""),
                ("normal", f"{side} neighbor: name and address _________________________________"),
                (
                    "normal",
                    "     [  ] No objection   [  ] I object      Signature: ______________________",
                ),
            ]
    paras += [
        ("normal", ""),
        (
            "normal",
            "9.  Additional information: please attach a drawing of the proposed "
            "alteration, including a site or plot plan, material samples, photographs, "
            "manufacturer's literature and color chips (if applicable), and two 'before' "
            "pictures of the area. Files uploaded in the portal are already attached.",
        ),
        ("normal", "10. Conditions:"),
        ("small", profile.arc_form_conditions or DEFAULT_CONDITIONS),
        ("normal", "Notes: " + "_" * 80),
        ("normal", ""),
        ("normal", "_______________________________________          ____________________"),
        ("normal", "Owner signature                                                   Date"),
    ]
    return pdf.render(paras, title=f"ARC-{req.pk} alteration request form")


# --- Reading a scanned, signed form ---------------------------------------------------


def request_reading(req: ArcRequest, attachment_id: int, *, actor, request=None) -> ScanReading:
    """Queue an automatic reading of an uploaded signed form."""
    if not can(actor, "arc.intake"):
        raise PermissionDenied("signoff")
    from arc.services import is_recused
    from documents.attachments import attachments_for

    if is_recused(actor, req):
        raise PermissionDenied("signoff")
    if not settings.GEMINI_API_KEY:
        raise SignoffError(
            "Automatic reading is not set up (no Gemini key). Enter the answers by hand."
        )
    if not attachments_for(req).filter(pk=attachment_id).exists():
        raise SignoffError("Choose one of this request's files.")
    reading = ScanReading.objects.create(
        arc_request=req, attachment_id=attachment_id, requested_by=actor
    )
    record_event("arc.scan_reading_requested", actor=actor, request=request, target=req)
    from arc.tasks import read_signed_form

    transaction.on_commit(lambda: read_signed_form.defer(reading_id=reading.pk))
    return reading


def run_reading(reading: ScanReading) -> int:
    """Read the form with Gemini and store its proposals (worker task)."""
    from core import ai
    from documents.attachments import attachments_for, current_scan, scan_allows_download

    req = ArcRequest.objects.unscoped("scan reading").get(pk=reading.arc_request_id)
    attachment = attachments_for(req).filter(pk=reading.attachment_id).first()
    if attachment is None or not scan_allows_download(current_scan(attachment)):
        ScanReading.objects.filter(pk=reading.pk).update(state="failed", error="File not available")
        return 0
    with system_access("scan reading"):
        rows = list(
            SignoffRequest.objects.filter(arc_request=req)
            .exclude(state=SignoffState.WITHDRAWN)
            .select_related("lot")
        )
    expected = [{"reference": f"SO-{r.pk}", "who": r.label} for r in rows]
    try:
        with attachment.file.open("rb") as fh:
            data = fh.read()
        found = ai.read_signed_form(data, attachment.content_type, expected)
    except ai.AIError as exc:
        ScanReading.objects.filter(pk=reading.pk).update(state="failed", error=str(exc)[:200])
        return 0
    by_ref = {f"SO-{r.pk}": r for r in rows}
    created = 0
    auto = bool(rule("arc.signoff_auto_confirm"))
    for item in found:
        signoff = by_ref.get(str(item.get("reference", "")).strip().upper())
        position = {"approve": SignoffState.APPROVED, "object": SignoffState.OBJECTED}.get(
            str(item.get("position", "")).lower(), ""
        )
        proposal = SignoffProposal.objects.create(
            reading=reading,
            signoff=signoff,
            signer_name=single_line(str(item.get("signer_name", "")))[:200],
            position=position,
            signed_on=_date(item.get("signed_on")),
            confidence=max(0, min(int(item.get("confidence") or 0), 100)),
        )
        created += 1
        if auto and signoff and position and proposal.signer_name and proposal.confidence >= 80:
            with contextlib.suppress(SignoffError):
                confirm_proposal(proposal, actor=SYSTEM)
    ScanReading.objects.filter(pk=reading.pk).update(state="done")
    record_event(
        "arc.scan_reading_done",
        actor=None,
        target=req,
        metadata={"proposals": created, "automatic": auto},
    )
    return created


def _date(value):
    try:
        return datetime.date.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


def confirm_proposal(
    proposal: SignoffProposal,
    *,
    actor,
    position=None,
    signer_name=None,
    retarget: SignoffRequest | None = None,
    request=None,
) -> SignoffResponse:
    """A person (or SYSTEM when automatic confirming is on) accepts what the
    reader proposed, optionally correcting it. Recorded as a paper sign-off
    with the scanned form as evidence."""
    if actor is not SYSTEM and not can(actor, "arc.intake"):
        raise PermissionDenied("signoff")
    if actor is not SYSTEM:
        from arc.services import is_recused

        if is_recused(actor, proposal.reading.arc_request):
            raise PermissionDenied("recused")
    if proposal.state != "pending":
        raise SignoffError("This has already been dealt with.")
    if retarget is not None:
        if retarget.arc_request_id != proposal.reading.arc_request_id:
            raise SignoffError("That neighbor is not on this request.")
        SignoffProposal.objects.filter(pk=proposal.pk).update(signoff=retarget)
        proposal.refresh_from_db()
    if proposal.signoff is None:
        raise SignoffError("Choose which neighbor this signature belongs to.")
    reading = proposal.reading
    response = respond(
        proposal.signoff,
        position=position or proposal.position,
        signer_name=signer_name or proposal.signer_name,
        channel=SignoffChannel.AI if actor is SYSTEM else SignoffChannel.PAPER,
        actor=actor,
        signed_on=proposal.signed_on,
        evidence_attachment_id=reading.attachment_id,
        request=request,
    )
    SignoffProposal.objects.filter(pk=proposal.pk).update(
        state="confirmed", decided_by=None if actor is SYSTEM else actor
    )
    return response


def reject_proposal(proposal: SignoffProposal, *, actor) -> None:
    require(actor, "arc.intake")
    from arc.services import is_recused

    if is_recused(actor, proposal.reading.arc_request):
        raise PermissionDenied("recused")
    SignoffProposal.objects.filter(pk=proposal.pk, state="pending").update(
        state="rejected", decided_by=actor
    )


def _user(actor):
    return None if actor is SYSTEM else actor
