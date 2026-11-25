"""ARC neighbour sign-off (Milestone 3 phase 7).

* Neighbours come from an adjacency list the board records once from the
  development plan; they are pre-selected on each request, and extra
  addresses can be added.
* A neighbour who owns a lot here and has a login signs in the portal. Anyone
  else gets a single-use, expiring link by email (only its hash is stored)
  that shows that one request's summary and plans, and nothing else.
* A printable form prefilled from the request can be signed on paper,
  scanned and uploaded; Gemini proposes what each signature says, and a
  person confirms it unless the board switched on automatic confirming.
* Sign-offs inform the reviewers. They never decide the request: that stays
  with the committee, with recusal.
"""

from __future__ import annotations

import contextlib
import datetime
import hashlib
import secrets

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.core.mail import send_mail
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone

from arc.models import (
    ArcRequest,
    ArcState,
    LotNeighbour,
    ScanReading,
    SignoffChannel,
    SignoffProposal,
    SignoffRequest,
    SignoffResponse,
    SignoffState,
)
from audit.services import record_event
from community.models import Lot, Ownership
from community.rules import rule
from core import policy
from core.policy import SYSTEM, can, require
from core.scoping import system_access
from core.text import single_line

OPEN_FOR_SIGNOFF = {
    ArcState.DRAFT,
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


def neighbours_of(lot: Lot) -> list[Lot]:
    with system_access("neighbours of lot"):
        ids = LotNeighbour.objects.filter(lot=lot).values_list("neighbour_id", flat=True)
        return list(Lot.objects.filter(pk__in=list(ids)).order_by("number"))


def set_neighbours(lot: Lot, neighbours, *, actor, request=None) -> list[Lot]:
    """Replace a lot's neighbours (kept symmetric: A next to B means B next to A)."""
    require(actor, "arc.configure")
    wanted = {n.pk for n in neighbours if n.pk != lot.pk}
    with transaction.atomic(), system_access("set neighbours"):
        LotNeighbour.objects.filter(lot=lot).exclude(neighbour_id__in=wanted).delete()
        LotNeighbour.objects.filter(neighbour=lot).exclude(lot_id__in=wanted).delete()
        for other in wanted:
            LotNeighbour.objects.get_or_create(lot=lot, neighbour_id=other)
            LotNeighbour.objects.get_or_create(lot_id=other, neighbour=lot)
    record_event(
        "arc.neighbours_set",
        actor=actor,
        request=request,
        target=lot,
        metadata={"lot_id": lot.pk, "count": len(wanted)},
    )
    return neighbours_of(lot)


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


def _require_arrange(actor, req: ArcRequest) -> None:
    if not can_arrange(actor, req):
        raise PermissionDenied("signoff")
    if req.state not in OPEN_FOR_SIGNOFF:
        raise SignoffError("Sign-offs can be arranged only while the request is open.")


def prepare_signoffs(req: ArcRequest, *, actor, request=None) -> int:
    """Add the neighbouring lots (from the adjacency list) not already asked."""
    _require_arrange(actor, req)
    created = 0
    with system_access("prepare signoffs"):
        for lot in neighbours_of(req.lot):
            try:
                with transaction.atomic():
                    SignoffRequest.objects.create(arc_request=req, lot=lot, created_by=_user(actor))
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
        raise SignoffError("That is the applicant's own lot.")
    try:
        with transaction.atomic(), system_access("add signoff lot"):
            signoff = SignoffRequest.objects.create(
                arc_request=req, lot=lot, created_by=_user(actor)
            )
    except IntegrityError as exc:
        raise SignoffError("That lot is already on the list.") from exc
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
            arc_request=req, name=name, address=address, email=email, created_by=_user(actor)
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
    _require_arrange(actor, signoff.arc_request)
    with system_access("withdraw signoff"):
        SignoffRequest.objects.filter(pk=signoff.pk, state=SignoffState.PENDING).update(
            state=SignoffState.WITHDRAWN,
            token_hash="",  # nosec B106 - clearing a used link's hash, not a password
        )
    record_event(
        "arc.signoff_withdrawn", actor=_user(actor), request=request, target=signoff.arc_request
    )


# --- Sending --------------------------------------------------------------------------


def _owners_of(lot_id: int):
    with system_access("signoff owners"):
        return list(Ownership.objects.active().filter(lot_id=lot_id).select_related("person__user"))


def send_signoffs(req: ArcRequest, *, actor, request=None) -> dict:
    """Email every waiting neighbour: portal users get a sign-in link, everyone
    else a single-use link. Emails carry only the link and the address of the
    project, never the plans or anyone's details."""
    _require_arrange(actor, req)
    base = settings.PORTAL_BASE_URL
    portal_link = base + reverse("signoffs_mine")
    sent = {"portal": 0, "link": 0, "no_contact": 0}
    messages: list[tuple[str, str]] = []
    expires = timezone.now() + datetime.timedelta(days=settings.SIGNOFF_LINK_DAYS)
    with transaction.atomic(), system_access("send signoffs"):
        pending = SignoffRequest.objects.select_for_update(of=("self",)).filter(
            arc_request=req, state=SignoffState.PENDING
        )
        for signoff in pending.select_related("lot"):
            portal_emails, link_emails = set(), set()
            if signoff.lot_id:
                for o in _owners_of(signoff.lot_id):
                    user = o.person.user
                    if user is not None and user.is_active:
                        portal_emails.add(user.email)
                    elif o.person.email:
                        link_emails.add(o.person.email)
            elif signoff.email:
                link_emails.add(signoff.email)
            token = None
            if link_emails:
                token = secrets.token_urlsafe(32)
                SignoffRequest.objects.filter(pk=signoff.pk).update(
                    token_hash=_hash(token), token_expires_at=expires
                )
            if not portal_emails and not link_emails:
                sent["no_contact"] += 1
                continue
            for email in portal_emails:
                messages.append((email, portal_link))
                sent["portal"] += 1
            for email in link_emails:
                messages.append((email, base + reverse("signoff_link", args=[token])))
                sent["link"] += 1
            SignoffRequest.objects.filter(pk=signoff.pk).update(sent_at=timezone.now())
    where = single_line(req.lot.street_address)
    subject = f"[{settings.SITE_NAME}] A neighbour asks for your sign-off"
    for email, link in messages:
        body = (
            f"Your neighbour at {where} has applied to the architectural committee for "
            f"an exterior project and asks whether you approve or object.\n\n"
            f"See the details and plans, and answer, here:\n{link}\n\n"
            "Your answer is shared with the committee. If you did not expect this, you can "
            f"ignore it.\n\n-- \n{settings.SITE_NAME}"
        )
        transaction.on_commit(
            lambda e=email, b=body: send_mail(subject, b, None, [e], fail_silently=True)
        )
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
    return signoff


def may_answer_in_portal(user, signoff: SignoffRequest) -> bool:
    if signoff.state != SignoffState.PENDING or not signoff.lot_id:
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
        raise SignoffError("Choose approve or object.")
    signer_name = single_line(signer_name)[:200]
    if not signer_name:
        raise SignoffError("Type your name to sign.")
    if channel == SignoffChannel.PORTAL:
        if actor is None or not may_answer_in_portal(actor, signoff):
            raise PermissionDenied("signoff")
    elif channel in (SignoffChannel.PAPER, SignoffChannel.AI):
        if actor is not SYSTEM and not can(actor, "arc.intake"):
            raise PermissionDenied("signoff")
    elif channel != SignoffChannel.LINK:
        raise SignoffError("Unknown channel.")
    with transaction.atomic(), system_access("record signoff"):
        locked = SignoffRequest.objects.select_for_update().get(pk=signoff.pk)
        if locked.state == SignoffState.WITHDRAWN:
            raise SignoffError("This sign-off is no longer being asked for.")
        if channel in (SignoffChannel.PORTAL, SignoffChannel.LINK) and locked.state != "pending":
            raise SignoffError("This sign-off has already been answered.")
        if locked.arc_request.state not in OPEN_FOR_SIGNOFF:
            raise SignoffError("The request is no longer open for sign-offs.")
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
    return response


def summary(req: ArcRequest) -> dict:
    with system_access("signoff summary"):
        rows = list(
            SignoffRequest.objects.filter(arc_request=req)
            .exclude(state=SignoffState.WITHDRAWN)
            .select_related("lot")
            .prefetch_related("responses")
        )
    counts = dict.fromkeys((SignoffState.PENDING, SignoffState.APPROVED, SignoffState.OBJECTED), 0)
    for r in rows:
        answers = list(r.responses.all())
        r.last_response = answers[-1] if answers else None
        counts[r.state] = counts.get(r.state, 0) + 1
    return {"rows": rows, "counts": counts}


# --- The printable form ---------------------------------------------------------------


def signoff_form_pdf(req: ArcRequest) -> bytes:
    """A form prefilled from the request, with a signature block for each
    neighbour, to print, sign and upload back."""
    from core import pdf

    with system_access("signoff form"):
        applicant = req.applicant.display_name
        rows = list(
            SignoffRequest.objects.filter(arc_request=req)
            .exclude(state=SignoffState.WITHDRAWN)
            .select_related("lot")
        )
    paras = [
        ("heading", f"Neighbour sign-off: architectural request ARC-{req.pk}"),
        ("normal", f"Applicant: {applicant}"),
        ("normal", f"Property: Lot {req.lot.number}, {req.lot.street_address}"),
        (
            "normal",
            f"Project: {req.title}" + (f" ({req.project_type})" if req.project_type else ""),
        ),
        ("normal", f"Planned start: {req.planned_start:%B %-d, %Y}" if req.planned_start else ""),
        ("normal", "Description:"),
        ("small", req.description[:1500]),
        (
            "normal",
            "Each neighbour below: tick APPROVE or OBJECT, print and sign your name, and date it. "
            "Your answer goes to the architectural committee, which makes the decision.",
        ),
    ]
    for i, row in enumerate(rows, start=1):
        paras += [
            ("heading", f"{i}. {row.label}"),
            ("normal", "[  ] APPROVE      [  ] OBJECT"),
            ("normal", "Name (print): ______________________________________"),
            ("normal", "Signature: _________________________   Date: ____________"),
            ("normal", "Comment: ___________________________________________"),
            ("small", f"Reference: SO-{row.pk}"),
        ]
    if not rows:
        paras.append(("normal", "No neighbours have been added to this request yet."))
    return pdf.render(paras, title=f"ARC-{req.pk} sign-off form")


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
    proposal: SignoffProposal, *, actor, position=None, signer_name=None, request=None
) -> SignoffResponse:
    """A person (or SYSTEM when automatic confirming is on) accepts what the
    reader proposed, optionally correcting it. Recorded as a paper sign-off
    with the scanned form as evidence."""
    if actor is not SYSTEM and not can(actor, "arc.intake"):
        raise PermissionDenied("signoff")
    if proposal.state != "pending":
        raise SignoffError("This has already been dealt with.")
    if proposal.signoff is None:
        raise SignoffError("Choose which neighbour this signature belongs to.")
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
    SignoffProposal.objects.filter(pk=proposal.pk, state="pending").update(
        state="rejected", decided_by=actor
    )


def _user(actor):
    return None if actor is SYSTEM else actor
