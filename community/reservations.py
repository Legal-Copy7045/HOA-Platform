"""Common-area reservations (Milestone 4, optional feature "reservations").

The only supported way to change bookings and booking rules. Every function
checks permission itself, locks what it changes, and writes an audit event.

* Booking rules are per area (``ReservationSettings``), set by the board,
  each with a citation where it comes from the governing documents.
* Owners book for their own lot; authorized contacts and tenants only where
  the area's rules allow it. Former owners and view-only contacts cannot.
* Two confirmed bookings of one area never overlap: requests and approvals
  for an area are serialized on its rules row, and a database exclusion
  constraint refuses an overlap even if that lock were bypassed.
* Owners see other lots' bookings only as "Booked" (no names, no lots).
* Fees and deposits are shown as text only. Nothing is charged here.
"""

from __future__ import annotations

import datetime

from django.conf import settings
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone

from audit.services import record_event
from community.models import (
    OPEN_RESERVATION_STATES,
    AccountContact,
    CommonArea,
    ContactKind,
    Reservation,
    ReservationSettings,
    ReservationState,
)
from core import policy
from core.money import format_cents
from core.policy import SYSTEM, require
from core.scoping import own_records, system_access


class ReservationError(ValueError):
    """A booking that breaks a rule. The message is safe to show."""


def _user(actor):
    return None if actor is SYSTEM else actor


def _audit(action, *, actor, request, target, metadata):
    record_event(action, actor=_user(actor), request=request, target=target, metadata=metadata)


# --- Rules per area ----------------------------------------------------------------


def settings_for(area: CommonArea) -> ReservationSettings:
    """The area's booking rules, or unsaved defaults (not bookable)."""
    found = ReservationSettings.objects.filter(area=area).first()
    return found or ReservationSettings(area=area)


def save_area_settings(
    area: CommonArea,
    *,
    actor,
    request=None,
    bookable: bool,
    needs_approval: bool = True,
    max_hours: int | None = None,
    advance_days: int | None = None,
    monthly_limit_per_lot: int | None = None,
    fee_cents: int | None = None,
    deposit_cents: int | None = None,
    allow_authorized: bool = False,
    allow_tenants: bool = False,
    terms: str = "",
    citation: str = "",
) -> ReservationSettings:
    require(actor, "reservation.configure")
    for value in (fee_cents, deposit_cents):
        if value is not None and value < 0:
            raise ReservationError("Fees and deposits cannot be negative.")
    if max_hours is not None and not 1 <= max_hours <= 24 * 14:
        raise ReservationError("Longest booking: between 1 hour and 14 days.")
    with transaction.atomic():
        row, _ = ReservationSettings.objects.select_for_update().get_or_create(area=area)
        row.bookable = bool(bookable)
        row.needs_approval = bool(needs_approval)
        row.max_hours = max_hours
        row.advance_days = advance_days
        row.monthly_limit_per_lot = monthly_limit_per_lot
        row.fee_cents = fee_cents
        row.deposit_cents = deposit_cents
        row.allow_authorized = bool(allow_authorized)
        row.allow_tenants = bool(allow_tenants)
        row.terms = (terms or "").strip()[:4000]
        row.citation = (citation or "").strip()[:200]
        row.updated_by = _user(actor)
        row.save()
        _audit(
            "reservation.settings_saved",
            actor=actor,
            request=request,
            target=row,
            metadata={"area_id": area.pk, "bookable": row.bookable},
        )
    return row


def bookable_areas() -> list[tuple[CommonArea, ReservationSettings]]:
    rows = ReservationSettings.objects.filter(bookable=True, area__active=True).select_related(
        "area"
    )
    return sorted(((r.area, r) for r in rows), key=lambda pair: pair[0].name)


def rule_lines(rules: ReservationSettings) -> list[str]:
    """The area's rules in plain words, for owners."""
    lines = []
    if rules.max_hours:
        lines.append(f"Up to {rules.max_hours} hour{'s' if rules.max_hours != 1 else ''}.")
    if rules.advance_days is not None:
        lines.append(f"Book up to {rules.advance_days} days ahead.")
    if rules.monthly_limit_per_lot:
        lines.append(
            f"{rules.monthly_limit_per_lot} booking"
            f"{'s' if rules.monthly_limit_per_lot != 1 else ''} per home per month."
        )
    lines.append(
        "A board member or the manager confirms each booking."
        if rules.needs_approval
        else "Confirmed at once if the time is free."
    )
    if rules.fee_cents:
        lines.append(f"Fee: {format_cents(rules.fee_cents)} (paid to the association, not here).")
    if rules.deposit_cents:
        lines.append(f"Deposit: {format_cents(rules.deposit_cents)} (refundable, not paid here).")
    return lines


# --- Who may book for which lot ----------------------------------------------------


def bookable_lot_ids(actor, rules: ReservationSettings) -> set[int]:
    """Lots this person may book ``rules.area`` for: their own lots, plus
    lots where they are an authorized contact or tenant if the rules allow."""
    if actor is SYSTEM or not policy.can(actor, "reservation.request"):
        return set()
    roles = policy.roles_for(actor)
    lots = set(roles.owned_lot_ids)
    if rules.allow_tenants:
        lots |= set(roles.tenant_lot_ids)
    if rules.allow_authorized and roles.person_id:
        with system_access("reservations: authorized contact lots"):
            lots |= set(
                AccountContact.objects.filter(
                    person_id=roles.person_id,
                    kind=ContactKind.AUTHORIZED,
                    account__closed_on__isnull=True,
                )
                .active()
                .values_list("account__lot_id", flat=True)
            )
    return lots


def _overlapping(area_id, starts_at, ends_at, states, exclude_pk=None):
    with system_access("reservations: overlap check"):
        qs = Reservation.objects.filter(
            area_id=area_id, state__in=states, starts_at__lt=ends_at, ends_at__gt=starts_at
        )
        if exclude_pk:
            qs = qs.exclude(pk=exclude_pk)
        return qs.exists()


def _check_rules(rules, lot_id, starts_at, ends_at, now) -> None:
    if ends_at <= starts_at:
        raise ReservationError("The end must be after the start.")
    if starts_at <= now:
        raise ReservationError("Choose a time in the future.")
    hours = (ends_at - starts_at).total_seconds() / 3600
    if rules.max_hours and hours > rules.max_hours:
        raise ReservationError(f"Bookings here can be at most {rules.max_hours} hours.")
    if rules.advance_days is not None:
        latest = timezone.localdate(now) + datetime.timedelta(days=rules.advance_days)
        if timezone.localdate(starts_at) > latest:
            raise ReservationError(f"Bookings open {rules.advance_days} days ahead.")
    if rules.monthly_limit_per_lot:
        local = timezone.localtime(starts_at)
        with system_access("reservations: monthly limit"):
            used = Reservation.objects.filter(
                area_id=rules.area_id,
                lot_id=lot_id,
                state__in=OPEN_RESERVATION_STATES,
                starts_at__year=local.year,
                starts_at__month=local.month,
            ).count()
        if used >= rules.monthly_limit_per_lot:
            raise ReservationError(
                f"Your home already has {used} booking{'s' if used != 1 else ''} here that "
                "month, the most allowed."
            )


def _confirmed_insert(fn):
    """Run ``fn`` in a savepoint; turn the no-double-booking constraint into
    a message the owner can read."""
    try:
        with transaction.atomic():
            return fn()
    except IntegrityError as exc:
        if "reservation_no_double_booking" in str(exc):
            raise ReservationError("That time was just booked by someone else.") from None
        raise


def request_reservation(
    *, area: CommonArea, lot_id: int, starts_at, ends_at, purpose: str = "", actor, request=None
) -> Reservation:
    require(actor, "reservation.request")
    from community.services import _refuse_read_only

    _refuse_read_only(actor)
    purpose = " ".join((purpose or "").split())[:200]
    with transaction.atomic():
        rules = (
            ReservationSettings.objects.select_for_update()
            .select_related("area")
            .filter(area=area)
            .first()
        )
        if rules is None or not rules.bookable or not rules.area.active:
            raise ReservationError("This area cannot be booked.")
        if lot_id not in bookable_lot_ids(actor, rules):
            raise ReservationError("You cannot book this area for that home.")
        _check_rules(rules, lot_id, starts_at, ends_at, timezone.now())
        if _overlapping(area.pk, starts_at, ends_at, [ReservationState.CONFIRMED]):
            raise ReservationError("That time is already booked. Choose another time.")
        state = ReservationState.PENDING if rules.needs_approval else ReservationState.CONFIRMED

        def create():
            return Reservation.objects.create(
                area=area,
                lot_id=lot_id,
                requested_by=actor,
                starts_at=starts_at,
                ends_at=ends_at,
                purpose=purpose,
                state=state,
                decided_at=None if rules.needs_approval else timezone.now(),
            )

        res = _confirmed_insert(create)
        _audit(
            "reservation.requested",
            actor=actor,
            request=request,
            target=res,
            metadata={"area_id": area.pk, "lot_id": lot_id, "state": state},
        )
        _email(res, "received" if state == ReservationState.PENDING else "confirmed")
    return res


def _locked(res: Reservation) -> tuple[Reservation, ReservationSettings | None]:
    rules = ReservationSettings.objects.select_for_update().filter(area_id=res.area_id).first()
    with system_access("reservations: lock"):
        res = Reservation.objects.select_for_update().select_related("area").get(pk=res.pk)
    return res, rules


def _refuse_own(actor, res: Reservation, action: str, request=None) -> None:
    """Own matters (D65): a booking for one's own home (or one they made) is
    another board member's to confirm or decline; they may still cancel it
    as the owner."""
    from core import conflicts

    conflicts.refuse(
        actor, res, area="reservation", action=action, request=request, error=ReservationError
    )


def approve(res: Reservation, *, actor, request=None) -> Reservation:
    require(actor, "reservation.manage")
    _refuse_own(actor, res, "approve", request)
    with transaction.atomic():
        res, _rules = _locked(res)
        if res.state != ReservationState.PENDING:
            raise ReservationError("Only a booking waiting for approval can be confirmed.")
        if res.ends_at <= timezone.now():
            raise ReservationError("That time has passed.")
        if _overlapping(res.area_id, res.starts_at, res.ends_at, [ReservationState.CONFIRMED]):
            raise ReservationError("Another booking is already confirmed for that time.")
        res.state = ReservationState.CONFIRMED
        res.decided_by = _user(actor)
        res.decided_at = timezone.now()
        _confirmed_insert(lambda: res.save(update_fields=["state", "decided_by", "decided_at"]))
        _audit(
            "reservation.approved",
            actor=actor,
            request=request,
            target=res,
            metadata={"area_id": res.area_id, "lot_id": res.lot_id},
        )
        _email(res, "confirmed")
    return res


def decline(res: Reservation, *, note: str = "", actor, request=None) -> Reservation:
    require(actor, "reservation.manage")
    _refuse_own(actor, res, "decline", request)
    with transaction.atomic():
        res, _rules = _locked(res)
        if res.state != ReservationState.PENDING:
            raise ReservationError("Only a booking waiting for approval can be declined.")
        res.state = ReservationState.DECLINED
        res.decided_by = _user(actor)
        res.decided_at = timezone.now()
        res.note = (note or "").strip()[:2000]
        res.save(update_fields=["state", "decided_by", "decided_at", "note"])
        _audit(
            "reservation.declined",
            actor=actor,
            request=request,
            target=res,
            metadata={"area_id": res.area_id, "lot_id": res.lot_id},
        )
        _email(res, "declined")
    return res


def can_cancel(res: Reservation, actor) -> bool:
    if not res.is_open or res.ends_at <= timezone.now():
        return False
    if policy.can(actor, "reservation.manage"):
        return True
    return _is_own(res, actor)


def _is_own(res: Reservation, actor) -> bool:
    if actor is SYSTEM or not policy.can(actor, "reservation.request"):
        return False
    roles = policy.roles_for(actor)
    return res.requested_by_id == actor.pk or res.lot_id in roles.owned_lot_ids


def cancel(res: Reservation, *, note: str = "", actor, request=None) -> Reservation:
    """The board or manager cancels any open booking; an owner cancels their
    own lot's (or one they made)."""
    staff = actor is SYSTEM or policy.can(actor, "reservation.manage")
    if not staff:
        require(actor, "reservation.request")
        from community.services import _refuse_read_only

        _refuse_read_only(actor)
    with transaction.atomic():
        res, _rules = _locked(res)
        if not staff and not _is_own(res, actor):
            raise ReservationError("You can cancel only your own bookings.")
        if not res.is_open:
            raise ReservationError("This booking is already closed.")
        if res.ends_at <= timezone.now():
            raise ReservationError("That time has passed.")
        res.state = ReservationState.CANCELLED
        res.decided_by = _user(actor)
        res.decided_at = timezone.now()
        res.note = (note or "").strip()[:2000]
        res.save(update_fields=["state", "decided_by", "decided_at", "note"])
        by = "owner" if not staff or _is_own(res, actor) else "board"
        _audit(
            "reservation.cancelled",
            actor=actor,
            request=request,
            target=res,
            metadata={"area_id": res.area_id, "lot_id": res.lot_id, "by": by},
        )
        if by == "board":
            _email(res, "cancelled")
    return res


# --- What people see ---------------------------------------------------------------


def schedule(user, *, days: int = 60, start=None) -> list[dict]:
    """Upcoming pending and confirmed bookings of every area. Staff see who;
    everyone else sees only "Booked" (their own bookings are marked)."""
    start = start or timezone.now()
    end = start + datetime.timedelta(days=days)
    staff = policy.can(user, "reservation.manage")
    visible = set(
        own_records(Reservation.objects.for_user(user), user).values_list("pk", flat=True)
    )
    with system_access("reservations: busy times"):
        rows = list(
            Reservation.objects.filter(
                state__in=OPEN_RESERVATION_STATES, ends_at__gt=start, starts_at__lt=end
            )
            .select_related("area", "lot", "requested_by__person")
            .order_by("starts_at")
        )
    out = []
    for r in rows:
        mine = r.pk in visible
        if not (staff or mine) and r.state != ReservationState.CONFIRMED:
            continue  # owners see only confirmed times of other homes
        row = {
            "area": r.area.name,
            "starts_at": r.starts_at,
            "ends_at": r.ends_at,
            "state": r.state,
            "mine": mine,
            "pk": r.pk if (staff or mine) else None,
            "label": "Your booking" if mine else "Booked",
        }
        if staff:
            person = getattr(r.requested_by, "person", None)
            row["label"] = r.lot.label + (f" · {person.display_name}" if person is not None else "")
            row["purpose"] = r.purpose
        out.append(row)
    return out


def calendar_entries(user, *, days: int = 60) -> list[dict]:
    """Confirmed bookings for the community calendar (no names)."""
    return [
        {"area": r["area"], "starts_at": r["starts_at"], "ends_at": r["ends_at"], "mine": r["mine"]}
        for r in schedule(user, days=days)
        if r["state"] == ReservationState.CONFIRMED
    ]


def pending_for_staff(user):
    if not policy.can(user, "reservation.manage"):
        return Reservation.objects.unscoped("nothing to show").none()
    return (
        Reservation.objects.for_user(user)
        .filter(state=ReservationState.PENDING, ends_at__gt=timezone.now())
        .select_related("area", "lot")
        .order_by("starts_at")
    )


# --- Email (routine update: honours the weekly digest) -----------------------------

_SUBJECTS = {
    "received": "We received your booking request",
    "confirmed": "Your booking is confirmed",
    "declined": "Your booking request was declined",
    "cancelled": "Your booking was cancelled",
}


def _email(res: Reservation, kind: str) -> None:
    from comms.notify import send_update

    email = res.requested_by.email if res.requested_by.is_active else ""
    if not email:
        return
    link = settings.PORTAL_BASE_URL + reverse("reservation_detail", args=[res.pk])
    when = timezone.localtime(res.starts_at)
    subject = f"[{settings.SITE_NAME}] {_SUBJECTS[kind]}"
    body = (
        f"{_SUBJECTS[kind]}: {res.area.name}, {when:%a %b %-d, %Y at %-I:%M %p}.\n\n"
        f"Sign in to see the details:\n{link}\n\n-- \n{settings.SITE_NAME}"
    )
    send_update({email}, subject=subject, body=body, link=link)
