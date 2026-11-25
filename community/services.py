"""The only supported way to change community records (CLAUDE.md rule 1).

Every function here:

* runs in one transaction and locks the rows it changes;
* refuses self-grants: nobody records an ownership, seat or office for
  themselves (decision D4). Ending one's own record (e.g. resigning) is allowed;
* writes an audit event for each change.

Who may call which function is decided by the policy layer (step 4); the
portal views and management commands check it before calling in here.
"""

from __future__ import annotations

import datetime
from collections.abc import Sequence

from django.conf import settings
from django.db import IntegrityError, transaction
from django.http import HttpRequest

from audit.models import Channel
from audit.services import record_event
from community.models import (
    BoardSeat,
    Lot,
    Office,
    OfficeHolding,
    Ownership,
    OwnershipType,
    Person,
    SeatEndReason,
    SeatType,
    today,
)


class CommunityError(ValueError):
    """A request that breaks a community rule. The message is safe to show."""


def _person_of(user) -> Person | None:
    if user is None:
        return None
    return Person.objects.filter(user=user).first()


def _refuse_self_grant(actor, person: Person, what: str) -> None:
    actor_person = _person_of(actor)
    if actor_person is not None and actor_person.pk == person.pk:
        raise CommunityError(f"You cannot record {what} for yourself; ask another board member.")


def _audit(action: str, *, actor, request, target, metadata, channel=None) -> None:
    record_event(
        action,
        actor=actor,
        request=request,
        target=target,
        metadata=metadata,
        channel=channel or (Channel.WEB if request is not None else Channel.CLI),
    )


def _iso(value: datetime.date | None) -> str | None:
    return value.isoformat() if value else None


# --- People -------------------------------------------------------------------


def record_person(
    *, display_name: str, email: str = "", actor=None, request: HttpRequest | None = None
) -> Person:
    display_name = (display_name or "").strip()
    if not display_name:
        raise CommunityError("A name is required.")
    with transaction.atomic():
        person = Person.objects.create(display_name=display_name, email=(email or "").strip())
        _audit(
            "person.recorded",
            actor=actor,
            request=request,
            target=person,
            metadata={"person_id": person.pk},
        )
    return person


# --- Ownership ----------------------------------------------------------------


def record_ownership(
    *,
    person: Person,
    lot: Lot,
    start_date: datetime.date,
    ownership_type: str = OwnershipType.SOLE,
    actor=None,
    request: HttpRequest | None = None,
) -> Ownership:
    if ownership_type not in OwnershipType.values:
        raise CommunityError("Unknown ownership type.")
    _refuse_self_grant(actor, person, "an ownership")
    try:
        with transaction.atomic():
            ownership = Ownership.objects.create(
                person=person,
                lot=lot,
                start_date=start_date,
                ownership_type=ownership_type,
                recorded_by=actor,
            )
            _audit(
                "ownership.recorded",
                actor=actor,
                request=request,
                target=ownership,
                metadata={
                    "lot_id": lot.pk,
                    "person_id": person.pk,
                    "ownership_type": ownership_type,
                    "start_date": _iso(start_date),
                },
            )
    except IntegrityError as exc:
        raise CommunityError("That person already owns this lot during that period.") from exc
    return ownership


def end_ownership(
    ownership: Ownership,
    *,
    end_date: datetime.date | None = None,
    actor=None,
    request: HttpRequest | None = None,
) -> Ownership:
    """End an ownership. With the default (today) access stops immediately."""
    end_date = end_date or today()
    with transaction.atomic():
        ownership = Ownership.objects.select_for_update().get(pk=ownership.pk)
        if ownership.end_date is not None and ownership.end_date <= end_date:
            raise CommunityError("This ownership has already ended.")
        if end_date < ownership.start_date:
            raise CommunityError("An ownership cannot end before it starts.")
        ownership.end_date = end_date
        ownership.save(update_fields=["end_date"])
        _audit(
            "ownership.ended",
            actor=actor,
            request=request,
            target=ownership,
            metadata={
                "lot_id": ownership.lot_id,
                "person_id": ownership.person_id,
                "end_date": _iso(end_date),
            },
        )
    return ownership


def transfer_lot(
    lot: Lot,
    *,
    new_owners: Sequence[Person],
    transfer_date: datetime.date | None = None,
    ownership_type: str | None = None,
    actor=None,
    request: HttpRequest | None = None,
) -> list[Ownership]:
    """End every ownership active on ``transfer_date`` and record the new owners.

    Previous owners lose access from ``transfer_date`` (today by default, so
    immediately). History stays on the lot.
    """
    transfer_date = transfer_date or today()
    if not new_owners:
        raise CommunityError("A transfer needs at least one new owner.")
    if len({p.pk for p in new_owners}) != len(new_owners):
        raise CommunityError("Each new owner may be listed only once.")
    if ownership_type is None:
        ownership_type = OwnershipType.SOLE if len(new_owners) == 1 else OwnershipType.JOINT
    with transaction.atomic():
        Lot.objects.select_for_update().get(pk=lot.pk)
        current = list(Ownership.objects.select_for_update().filter(lot=lot).active(transfer_date))
        for ownership in current:
            end_ownership(ownership, end_date=transfer_date, actor=actor, request=request)
        created = [
            record_ownership(
                person=person,
                lot=lot,
                start_date=transfer_date,
                ownership_type=ownership_type,
                actor=actor,
                request=request,
            )
            for person in new_owners
        ]
        _audit(
            "ownership.transferred",
            actor=actor,
            request=request,
            target=lot,
            metadata={
                "lot_id": lot.pk,
                "transfer_date": _iso(transfer_date),
                "ended_count": len(current),
                "new_owner_count": len(created),
            },
        )
    return created


# --- Board seats ----------------------------------------------------------------


def record_board_seat(
    *,
    person: Person,
    seat_type: str,
    term_start: datetime.date,
    term_end: datetime.date | None = None,
    citation: str = "",
    actor=None,
    request: HttpRequest | None = None,
) -> BoardSeat:
    if seat_type not in SeatType.values:
        raise CommunityError("Unknown seat type.")
    if term_end is not None and term_end < term_start:
        raise CommunityError("A term cannot end before it starts.")
    _refuse_self_grant(actor, person, "a board seat")
    try:
        with transaction.atomic():
            seat = BoardSeat.objects.create(
                person=person,
                seat_type=seat_type,
                term_start=term_start,
                term_end=term_end,
                citation=citation.strip(),
                recorded_by=actor,
            )
            _audit(
                "seat.recorded",
                actor=actor,
                request=request,
                target=seat,
                metadata={
                    "person_id": person.pk,
                    "seat_type": seat_type,
                    "term_start": _iso(term_start),
                    "term_end": _iso(term_end),
                },
            )
    except IntegrityError as exc:
        raise CommunityError("That person already holds a seat during that period.") from exc
    return seat


def end_board_seat(
    seat: BoardSeat,
    *,
    reason: str,
    ended_on: datetime.date | None = None,
    actor=None,
    request: HttpRequest | None = None,
) -> BoardSeat:
    """End a seat. Offices that require a seat end on the same day (D1)."""
    if reason not in SeatEndReason.values:
        raise CommunityError("Choose why the seat ended.")
    ended_on = ended_on or today()
    with transaction.atomic():
        seat = BoardSeat.objects.select_for_update().get(pk=seat.pk)
        if seat.ended_on is not None:
            raise CommunityError("This seat has already ended.")
        if ended_on < seat.term_start:
            raise CommunityError("A seat cannot end before its term starts.")
        seat.ended_on = ended_on
        seat.end_reason = reason
        seat.save(update_fields=["ended_on", "end_reason"])
        _audit(
            "seat.ended",
            actor=actor,
            request=request,
            target=seat,
            metadata={"person_id": seat.person_id, "ended_on": _iso(ended_on), "reason": reason},
        )
        if settings.OFFICERS_REQUIRE_BOARD_SEAT:
            holdings = (
                OfficeHolding.objects.select_for_update()
                .filter(person_id=seat.person_id)
                .active(ended_on)
            )
            for holding in holdings:
                end_office(holding, end_date=ended_on, actor=actor, request=request)
    return seat


# --- Offices ---------------------------------------------------------------------


def record_office(
    *,
    person: Person,
    office: str,
    start_date: datetime.date,
    actor=None,
    request: HttpRequest | None = None,
) -> OfficeHolding:
    if office not in Office.values:
        raise CommunityError("Unknown office.")
    _refuse_self_grant(actor, person, "an office")
    if settings.OFFICERS_REQUIRE_BOARD_SEAT and not (
        BoardSeat.objects.filter(person=person).active(start_date).exists()
    ):
        raise CommunityError("Officers must hold an active board seat on the start date.")
    try:
        with transaction.atomic():
            holding = OfficeHolding.objects.create(
                person=person, office=office, start_date=start_date, recorded_by=actor
            )
            _audit(
                "office.recorded",
                actor=actor,
                request=request,
                target=holding,
                metadata={"person_id": person.pk, "office": office, "start_date": _iso(start_date)},
            )
    except IntegrityError as exc:
        raise CommunityError(
            "Someone already holds that office then; end their term first."
        ) from exc
    return holding


def end_office(
    holding: OfficeHolding,
    *,
    end_date: datetime.date | None = None,
    actor=None,
    request: HttpRequest | None = None,
) -> OfficeHolding:
    end_date = end_date or today()
    with transaction.atomic():
        holding = OfficeHolding.objects.select_for_update().get(pk=holding.pk)
        if holding.end_date is not None and holding.end_date <= end_date:
            raise CommunityError("This office holding has already ended.")
        if end_date < holding.start_date:
            raise CommunityError("An office holding cannot end before it starts.")
        holding.end_date = end_date
        holding.save(update_fields=["end_date"])
        _audit(
            "office.ended",
            actor=actor,
            request=request,
            target=holding,
            metadata={
                "person_id": holding.person_id,
                "office": holding.office,
                "end_date": _iso(end_date),
            },
        )
    return holding
