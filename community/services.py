"""The only supported way to change community records (CLAUDE.md rule 1).

Every public function here:

* checks permission through core.policy first (views and commands pass the
  logged-in user; trusted internal code passes ``core.policy.SYSTEM``);
* refuses self-grants: nobody records an ownership, seat, office or role for
  themselves (decision D4). Ending one's own record (e.g. resigning) is allowed;
* runs in one transaction, locking the rows it changes;
* writes an audit event for each change.
"""

from __future__ import annotations

import datetime
from collections.abc import Sequence

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.http import HttpRequest
from django.utils import timezone

from audit.models import Channel
from audit.services import record_event
from community.models import (
    Account,
    AccountContact,
    AppointedBy,
    BoardSeat,
    ContactKind,
    ContactMethod,
    ContactMethodKind,
    Lot,
    Office,
    OfficeHolding,
    Ownership,
    OwnershipType,
    Person,
    Role,
    RoleAssignment,
    SeatEndReason,
    SeatType,
    today,
)
from core.policy import SYSTEM, can, require
from core.scoping import internal, system_access


class CommunityError(ValueError):
    """A request that breaks a community rule. The message is safe to show."""


def _user(actor):
    """The User to store/audit, or None for SYSTEM."""
    return None if actor is SYSTEM else actor


def _authorize(actor, action: str) -> None:
    if actor is None:
        raise CommunityError("An actor is required (pass request.user or core.policy.SYSTEM).")
    require(actor, action)


def _refuse_self_grant(actor, person: Person, what: str) -> None:
    if actor is SYSTEM:
        return
    with system_access("self-grant check"):
        mine = Person.objects.filter(user=actor).values_list("pk", flat=True).first()
    if mine is not None and mine == person.pk:
        raise CommunityError(f"You cannot record {what} for yourself; ask another board member.")


def _audit(action: str, *, actor, request, target, metadata) -> None:
    record_event(
        action,
        actor=_user(actor),
        request=request,
        target=target,
        metadata=metadata,
        channel=Channel.WEB if request is not None else Channel.CLI,
    )


def _iso(value: datetime.date | None) -> str | None:
    return value.isoformat() if value else None


# --- People -------------------------------------------------------------------


def record_person(
    *, display_name: str, email: str = "", actor, request: HttpRequest | None = None
) -> Person:
    _authorize(actor, "person.manage")
    display_name = (display_name or "").strip()
    if not display_name:
        raise CommunityError("A name is required.")
    with transaction.atomic(), system_access("record_person"):
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
    actor,
    request: HttpRequest | None = None,
) -> Ownership:
    _authorize(actor, "ownership.manage")
    if ownership_type not in OwnershipType.values:
        raise CommunityError("Unknown ownership type.")
    _refuse_self_grant(actor, person, "an ownership")
    try:
        with transaction.atomic(), system_access("record_ownership"):
            ownership = Ownership.objects.create(
                person=person,
                lot=lot,
                account=_account_for(lot, start_date),
                start_date=start_date,
                ownership_type=ownership_type,
                recorded_by=_user(actor),
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
    actor,
    request: HttpRequest | None = None,
) -> Ownership:
    """End an ownership. With the default (today) access stops immediately."""
    _authorize(actor, "ownership.manage")
    end_date = end_date or today()
    with transaction.atomic(), system_access("end_ownership"):
        ownership = Ownership.objects.select_for_update().get(pk=ownership.pk)
        if ownership.end_date is not None and ownership.end_date <= end_date:
            raise CommunityError("This ownership has already ended.")
        if end_date < ownership.start_date:
            raise CommunityError("An ownership cannot end before it starts.")
        ownership.end_date = end_date
        ownership.save(update_fields=["end_date"])
        _close_account_if_empty(ownership.account_id, end_date)
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
    actor,
    request: HttpRequest | None = None,
) -> list[Ownership]:
    """End every ownership active on ``transfer_date`` and record the new owners.

    Previous owners lose access from ``transfer_date`` (today by default, so
    immediately). History stays on the lot.
    """
    _authorize(actor, "ownership.manage")
    transfer_date = transfer_date or today()
    if not new_owners:
        raise CommunityError("A transfer needs at least one new owner.")
    if len({p.pk for p in new_owners}) != len(new_owners):
        raise CommunityError("Each new owner may be listed only once.")
    if ownership_type is None:
        ownership_type = OwnershipType.SOLE if len(new_owners) == 1 else OwnershipType.JOINT
    with transaction.atomic(), system_access("transfer_lot"):
        Lot.objects.select_for_update().get(pk=lot.pk)
        current = list(Ownership.objects.select_for_update().filter(lot=lot).active(transfer_date))
        continuing = {o.person_id for o in current} & {p.pk for p in new_owners}
        for ownership in current:
            end_ownership(ownership, end_date=transfer_date, actor=actor, request=request)
        if continuing:
            # Re-recording the same household (adding a spouse, a trust): the
            # account carries on. A sale gives the buyers a new account.
            _reopen_account(current[0].account_id)
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


# --- Accounts (Milestone 3) -------------------------------------------------------


@internal("community accounts")
def _next_account_number(lot: Lot) -> str:
    seq = Account.objects.filter(lot=lot).count() + 1
    number = f"{lot.number:03d}-{seq:02d}"
    while Account.objects.filter(number=number).exists():
        seq += 1
        number = f"{lot.number:03d}-{seq:02d}"
    return number


@internal("community accounts")
def _account_for(lot: Lot, on: datetime.date) -> Account:
    """The lot's account open on ``on``, or a new one starting then."""
    account = (
        Account.objects.select_for_update()
        .filter(lot=lot, opened_on__lte=on)
        .filter(Q(closed_on__isnull=True) | Q(closed_on__gt=on))
        .first()
    )
    if account is None:
        account = Account.objects.create(lot=lot, number=_next_account_number(lot), opened_on=on)
    return account


@internal("community accounts")
def _close_account_if_empty(account_id: int | None, on: datetime.date) -> None:
    if account_id is None:
        return
    still_owned = Ownership.objects.filter(account_id=account_id).active(on).exists()
    if still_owned:
        return
    Account.objects.filter(pk=account_id, closed_on__isnull=True).update(closed_on=on)
    # Contacts and tenants of a dormant account lose access with it.
    AccountContact.objects.filter(account_id=account_id, end_date__isnull=True).update(end_date=on)


@internal("community accounts")
def _reopen_account(account_id: int | None) -> None:
    if account_id is not None:
        Account.objects.filter(pk=account_id).update(closed_on=None)


def set_account_number(account: Account, number: str, *, actor, request=None) -> Account:
    """Use the management company's account number (shown on statements)."""
    _authorize(actor, "account.manage")
    number = (number or "").strip()
    if not number or len(number) > 40:
        raise CommunityError("Enter the account number (up to 40 characters).")
    with system_access("set account number"):
        if Account.objects.filter(number=number).exclude(pk=account.pk).exists():
            raise CommunityError("Another account already has that number.")
        Account.objects.filter(pk=account.pk).update(number=number)
    _audit("account.number_set", actor=actor, request=request, target=account, metadata={})
    account.number = number
    return account


def add_account_contact(
    account: Account,
    *,
    person: Person,
    kind: str,
    start_date: datetime.date | None = None,
    actor,
    request=None,
) -> AccountContact:
    """Add an authorized contact, a view-only contact or a tenant. Access
    comes from the kind; a login still needs a board invitation."""
    _authorize(actor, "account.manage")
    if kind not in ContactKind.values:
        raise CommunityError("Choose what kind of contact this is.")
    _refuse_self_grant(actor, person, "an account contact")
    if not account.is_active:
        raise CommunityError("This account is dormant; contacts go on the current account.")
    with system_access("add account contact"):
        if Ownership.objects.filter(account=account, person=person).active().exists():
            raise CommunityError("That person is already an owner on this account.")
        try:
            with transaction.atomic():
                contact = AccountContact.objects.create(
                    account=account,
                    person=person,
                    kind=kind,
                    start_date=start_date or today(),
                    recorded_by=_user(actor),
                )
        except IntegrityError as exc:
            raise CommunityError("That person is already on this account.") from exc
    _audit(
        "account.contact_added",
        actor=actor,
        request=request,
        target=account,
        metadata={"person_id": person.pk, "kind": kind},
    )
    return contact


def end_account_contact(contact: AccountContact, *, actor, request=None) -> AccountContact:
    _authorize(actor, "account.manage")
    with system_access("end account contact"):
        AccountContact.objects.filter(pk=contact.pk, end_date__isnull=True).update(end_date=today())
    _audit(
        "account.contact_ended",
        actor=actor,
        request=request,
        target=contact.account,
        metadata={"person_id": contact.person_id, "kind": contact.kind},
    )
    return contact


def add_contact_method(
    person: Person, *, kind: str, value: str, label: str = "", notices: bool = False, actor
) -> ContactMethod:
    """The person themselves (or the board) adds a second email, phone or
    mailing address."""
    if actor is not SYSTEM and not (_person_of(actor) == person.pk or can(actor, "person.manage")):
        raise PermissionDenied("contact_method")
    _refuse_read_only(actor)
    value = (value or "").strip()
    if kind not in ContactMethodKind.values or not value or len(value) > 300:
        raise CommunityError("Choose a kind and enter the details.")
    if kind == ContactMethodKind.EMAIL:
        from django.core.validators import validate_email

        try:
            validate_email(value)
        except ValidationError as exc:
            raise CommunityError("Enter a valid email address.") from exc
    with system_access("add contact method"):
        if ContactMethod.objects.filter(person=person).count() >= 10:
            raise CommunityError("Ten extra contact details is the most.")
        method = ContactMethod.objects.create(
            person=person, kind=kind, value=value, label=label.strip()[:60], notices=notices
        )
    _audit(
        "contact.method_added", actor=actor, request=None, target=person, metadata={"kind": kind}
    )
    return method


def remove_contact_method(method: ContactMethod, *, actor) -> None:
    if actor is not SYSTEM and not (
        _person_of(actor) == method.person_id or can(actor, "person.manage")
    ):
        raise PermissionDenied("contact_method")
    _refuse_read_only(actor)
    with system_access("remove contact method"):
        ContactMethod.objects.filter(pk=method.pk).delete()
    _audit(
        "contact.method_removed",
        actor=actor,
        request=None,
        target=method.person,
        metadata={"kind": method.kind},
    )


def notice_emails(person: Person) -> list[str]:
    """Extra email addresses this person asked to receive notices at."""
    with system_access("notice emails"):
        return list(
            ContactMethod.objects.filter(
                person=person, kind=ContactMethodKind.EMAIL, notices=True
            ).values_list("value", flat=True)
        )


def lots_by_ids(ids) -> list[Lot]:
    """Lots by id, for another app's service that has already checked the
    caller's scope (e.g. the lots someone may raise a request for)."""
    with system_access("lots by ids"):
        return list(Lot.objects.filter(pk__in=list(ids)).order_by("number"))


def staff_people(action: str) -> list[Person]:
    """People whose login may perform ``action`` (for assignment pickers)."""
    with system_access("staff people"):
        people = list(Person.objects.filter(user__isnull=False, user__is_active=True))
    return [p for p in people if can(p.user, action)]


def notice_emails_for_users(users) -> list[str]:
    """Consented extra notice emails of the people behind these logins."""
    with system_access("notice emails for users"):
        return list(
            ContactMethod.objects.filter(
                person__user__in=list(users), kind=ContactMethodKind.EMAIL, notices=True
            ).values_list("value", flat=True)
        )


def _refuse_read_only(actor) -> None:
    """Former owners keep a read-only login: they change nothing."""
    if actor is SYSTEM:
        return
    from core.policy import FORMER_OWNER, roles_for

    groups = roles_for(actor).groups
    if FORMER_OWNER in groups and not (groups - {FORMER_OWNER}):
        raise PermissionDenied("read_only")


def _person_of(user) -> int | None:
    with system_access("person of user"):
        return Person.objects.filter(user=user).values_list("pk", flat=True).first()


# --- Board seats ----------------------------------------------------------------


def record_board_seat(
    *,
    person: Person,
    seat_type: str,
    term_start: datetime.date,
    term_end: datetime.date | None = None,
    citation: str = "",
    actor,
    request: HttpRequest | None = None,
) -> BoardSeat:
    _authorize(actor, "seat.manage")
    if seat_type not in SeatType.values:
        raise CommunityError("Unknown seat type.")
    if term_end is not None and term_end < term_start:
        raise CommunityError("A term cannot end before it starts.")
    _refuse_self_grant(actor, person, "a board seat")
    try:
        with transaction.atomic(), system_access("record_board_seat"):
            seat = BoardSeat.objects.create(
                person=person,
                seat_type=seat_type,
                term_start=term_start,
                term_end=term_end,
                citation=citation.strip(),
                recorded_by=_user(actor),
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
    if actor is not SYSTEM:
        # Review A3: one member recording a seat could create a second board
        # login for themselves; the whole board now hears about every seat.
        _board_notice("board_seat_recorded")
    return seat


def end_board_seat(
    seat: BoardSeat,
    *,
    reason: str,
    ended_on: datetime.date | None = None,
    actor,
    request: HttpRequest | None = None,
) -> BoardSeat:
    """End a seat. Offices that require a seat end on the same day (D1).

    Board members may always end their own seat (resignation).
    """
    with system_access("end_board_seat: own seat?"):
        own = actor is not SYSTEM and seat.person.user_id == getattr(actor, "pk", None)
    if not own:
        _authorize(actor, "seat.manage")
    if reason not in SeatEndReason.values:
        raise CommunityError("Choose why the seat ended.")
    ended_on = ended_on or today()
    with transaction.atomic(), system_access("end_board_seat"):
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
                _end_office(holding, end_date=ended_on, actor=actor, request=request)
    return seat


# --- Offices ---------------------------------------------------------------------


def record_office(
    *,
    person: Person,
    office: str,
    start_date: datetime.date,
    actor,
    request: HttpRequest | None = None,
) -> OfficeHolding:
    _authorize(actor, "office.manage")
    if office not in Office.values:
        raise CommunityError("Unknown office.")
    _refuse_self_grant(actor, person, "an office")
    with system_access("record_office: seat check"):
        seated = BoardSeat.objects.filter(person=person).active(start_date).exists()
    if settings.OFFICERS_REQUIRE_BOARD_SEAT and not seated:
        raise CommunityError("Officers must hold an active board seat on the start date.")
    try:
        with transaction.atomic(), system_access("record_office"):
            holding = OfficeHolding.objects.create(
                person=person, office=office, start_date=start_date, recorded_by=_user(actor)
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
    actor,
    request: HttpRequest | None = None,
) -> OfficeHolding:
    _authorize(actor, "office.manage")
    return _end_office(holding, end_date=end_date, actor=actor, request=request)


def _end_office(holding, *, end_date, actor, request) -> OfficeHolding:
    end_date = end_date or today()
    with transaction.atomic(), system_access("end_office"):
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


# --- Roles (ARC reviewer, auditor; sysadmin via two-person approval) ----------


def grant_role(
    *,
    person: Person,
    role: str,
    start_date: datetime.date | None = None,
    appointed_by: str = "",
    citation: str = "",
    actor,
    request: HttpRequest | None = None,
) -> RoleAssignment:
    if role == Role.SYSADMIN:
        raise CommunityError("Sysadmin needs a nomination and a second board member's approval.")
    if role not in Role.values:
        raise CommunityError("Unknown role.")
    _authorize(actor, f"role.{role}.manage")
    if role == Role.ARC_REVIEWER and appointed_by not in AppointedBy.values:
        raise CommunityError("Record who appointed this ARC member (board or declarant).")
    _refuse_self_grant(actor, person, "a role")
    if actor is not SYSTEM:
        _board_notice("role_granted")
    return _record_role(
        person=person,
        role=role,
        start_date=start_date or today(),
        appointed_by=appointed_by,
        citation=citation,
        actor=actor,
        request=request,
    )


def _record_role(*, person, role, start_date, appointed_by, citation, actor, request):
    """Write path shared with the two-person sysadmin approval (step 7)."""
    try:
        with transaction.atomic(), system_access("record_role"):
            assignment = RoleAssignment.objects.create(
                person=person,
                role=role,
                start_date=start_date,
                appointed_by=appointed_by,
                citation=citation.strip(),
                granted_by=_user(actor),
            )
            _audit(
                "role.granted",
                actor=actor,
                request=request,
                target=assignment,
                metadata={
                    "person_id": person.pk,
                    "role": role,
                    "start_date": _iso(start_date),
                    "appointed_by": appointed_by or None,
                },
            )
    except IntegrityError as exc:
        raise CommunityError("That person already holds this role then.") from exc
    return assignment


def end_role(
    assignment: RoleAssignment,
    *,
    end_date: datetime.date | None = None,
    actor,
    request: HttpRequest | None = None,
) -> RoleAssignment:
    """End a role. Revoking sysadmin needs only one board member (D4)."""
    action = (
        "role.sysadmin.revoke"
        if assignment.role == Role.SYSADMIN
        else f"role.{assignment.role}.manage"
    )
    _authorize(actor, action)
    end_date = end_date or today()
    with transaction.atomic(), system_access("end_role"):
        assignment = RoleAssignment.objects.select_for_update().get(pk=assignment.pk)
        if assignment.end_date is not None and assignment.end_date <= end_date:
            raise CommunityError("This role has already ended.")
        if end_date < assignment.start_date:
            raise CommunityError("A role cannot end before it starts.")
        assignment.end_date = end_date
        assignment.save(update_fields=["end_date"])
        _audit(
            "role.ended",
            actor=actor,
            request=request,
            target=assignment,
            metadata={
                "person_id": assignment.person_id,
                "role": assignment.role,
                "end_date": _iso(end_date),
            },
        )
        if assignment.role == Role.SYSADMIN:
            _board_notice("sysadmin_revoked")
    return assignment


# --- Logins --------------------------------------------------------------------


def link_login(person: Person, user, *, actor, request: HttpRequest | None = None) -> Person:
    """Attach a login to a Person (called when an invitation is accepted)."""
    if actor is not SYSTEM:
        _authorize(actor, "person.manage")
    with transaction.atomic(), system_access("link_login"):
        person = Person.objects.select_for_update().get(pk=person.pk)
        if person.user_id is not None:
            raise CommunityError("This person already has a login.")
        if Person.objects.filter(user=user).exists():
            raise CommunityError("That login already belongs to someone else.")
        person.user = user
        person.save(update_fields=["user"])
        _audit(
            "person.login_linked",
            actor=actor,
            request=request,
            target=person,
            metadata={"person_id": person.pk, "user_id": user.pk},
        )
    return person


# --- Sysadmin: two board members to grant, one to revoke (D4) ------------------


def _board_notice(kind: str) -> None:
    from community.tasks import notify_board

    transaction.on_commit(lambda: notify_board.defer(kind=kind))


def nominate_sysadmin(*, person: Person, actor, request: HttpRequest | None = None):
    from community.models import SysadminNomination

    _authorize(actor, "role.sysadmin.nominate")
    if person.user_id is not None and person.user_id == getattr(actor, "pk", None):
        raise CommunityError("You cannot nominate yourself.")
    now = timezone.now()
    with transaction.atomic(), system_access("nominate_sysadmin"):
        if RoleAssignment.objects.filter(person=person, role=Role.SYSADMIN).active().exists():
            raise CommunityError("This person is already a sysadmin.")
        open_ = SysadminNomination.objects.filter(person=person, outcome="", expires_at__gt=now)
        if open_.exists():
            raise CommunityError("There is already an open nomination for this person.")
        nomination = SysadminNomination.objects.create(
            person=person,
            nominated_by=actor,
            expires_at=now + datetime.timedelta(days=settings.SYSADMIN_NOMINATION_TTL_DAYS),
        )
        _audit(
            "role.sysadmin_nominated",
            actor=actor,
            request=request,
            target=nomination,
            metadata={"person_id": person.pk},
        )
        _board_notice("sysadmin_nominated")
    return nomination


def approve_sysadmin(nomination, *, actor, request: HttpRequest | None = None):
    from community.models import NominationOutcome, SysadminNomination

    _authorize(actor, "role.sysadmin.approve")
    with transaction.atomic(), system_access("approve_sysadmin"):
        nomination = SysadminNomination.objects.select_for_update().get(pk=nomination.pk)
        if not nomination.is_open:
            raise CommunityError("This nomination is no longer open.")
        if nomination.nominated_by_id == actor.pk:
            raise CommunityError("A different board member must approve your nomination.")
        if nomination.person.user_id == actor.pk:
            raise CommunityError("You cannot approve your own nomination.")
        assignment = _record_role(
            person=nomination.person,
            role=Role.SYSADMIN,
            start_date=today(),
            appointed_by="",
            citation="",
            actor=actor,
            request=request,
        )
        nomination.outcome = NominationOutcome.APPROVED
        nomination.decided_by = actor
        nomination.decided_at = timezone.now()
        nomination.assignment = assignment
        nomination.save(update_fields=["outcome", "decided_by", "decided_at", "assignment"])
        _audit(
            "role.sysadmin_approved",
            actor=actor,
            request=request,
            target=nomination,
            metadata={"person_id": nomination.person_id},
        )
        _board_notice("sysadmin_granted")
    return nomination


def decline_sysadmin(nomination, *, actor, request: HttpRequest | None = None):
    """Decline (any board member) or withdraw (the nominator)."""
    from community.models import NominationOutcome, SysadminNomination

    _authorize(actor, "role.sysadmin.nominate")
    with transaction.atomic(), system_access("decline_sysadmin"):
        nomination = SysadminNomination.objects.select_for_update().get(pk=nomination.pk)
        if not nomination.is_open:
            raise CommunityError("This nomination is no longer open.")
        nomination.outcome = NominationOutcome.DECLINED
        nomination.decided_by = actor
        nomination.decided_at = timezone.now()
        nomination.save(update_fields=["outcome", "decided_by", "decided_at"])
        _audit(
            "role.sysadmin_declined",
            actor=actor,
            request=request,
            target=nomination,
            metadata={"person_id": nomination.person_id},
        )
    return nomination


def board_emails() -> list[str]:
    with system_access("board notification recipients"):
        return list(
            BoardSeat.objects.active()
            .filter(person__user__is_active=True)
            .values_list("person__user__email", flat=True)
            .distinct()
        )


# --- Community definition -------------------------------------------------------


def set_rule(
    *,
    key: str,
    value: str,
    citation: str = "",
    notes: str = "",
    actor,
    request: HttpRequest | None = None,
):
    from community.models import GoverningRule
    from community.rules import REGISTRY, validate_value

    _authorize(actor, "community.manage_rules")
    if key not in REGISTRY:
        raise CommunityError("Unknown rule.")
    try:
        value = validate_value(key, value)
    except ValueError as exc:
        raise CommunityError(str(exc)) from exc
    with transaction.atomic():
        row, _ = GoverningRule.objects.select_for_update().get_or_create(key=key)
        row.value, row.citation, row.notes = value, citation.strip()[:200], notes.strip()
        row.updated_by = _user(actor)
        row.save()
        _audit(
            "community.rule_changed",
            actor=actor,
            request=request,
            target=row,
            metadata={"key": key, "has_citation": bool(row.citation)},
        )
    return row


def update_profile(
    *, legal_name: str, fiscal_year_start_month: int, actor, request: HttpRequest | None = None
):
    from community.models import CommunityProfile

    _authorize(actor, "community.manage_rules")
    if not 1 <= int(fiscal_year_start_month) <= 12:
        raise CommunityError("The fiscal year must start in month 1-12.")
    with transaction.atomic():
        profile = CommunityProfile.get()
        profile.legal_name = legal_name.strip()[:200] or profile.legal_name
        profile.fiscal_year_start_month = int(fiscal_year_start_month)
        profile.save()
        _audit(
            "community.profile_changed", actor=actor, request=request, target=profile, metadata={}
        )
    return profile


def save_common_area(
    *,
    name: str,
    kind: str,
    notes: str = "",
    active: bool = True,
    area=None,
    actor,
    request: HttpRequest | None = None,
):
    from community.models import CommonArea, CommonAreaKind

    _authorize(actor, "community.manage_rules")
    if kind not in CommonAreaKind.values or not name.strip():
        raise CommunityError("Give the area a name and a kind.")
    with transaction.atomic():
        area = area or CommonArea()
        area.name, area.kind, area.notes, area.active = name.strip()[:120], kind, notes, active
        area.save()
        _audit(
            "community.common_area_saved",
            actor=actor,
            request=request,
            target=area,
            metadata={"kind": kind, "active": active},
        )
    return area


# --- Owner's own profile and the directory ----------------------------------------


def update_my_profile(
    *,
    user,
    phone: str = "",
    mailing_address: str = "",
    electronic_notice_consent: bool = False,
    in_directory: bool = False,
    directory_show_email: bool = False,
    directory_show_phone: bool = False,
    request: HttpRequest | None = None,
):
    """Anyone with a linked Person edits only their own profile."""
    _refuse_read_only(user)
    from community.models import ContactProfile

    with transaction.atomic(), system_access("update_my_profile"):
        person = Person.objects.filter(user=user).first()
        if person is None:
            raise CommunityError("Your login is not linked to a person yet.")
        profile, _ = ContactProfile.objects.select_for_update().get_or_create(person=person)
        consent_changed = profile.electronic_notice_consent != electronic_notice_consent
        profile.phone = phone.strip()[:40]
        profile.mailing_address = mailing_address.strip()[:1000]
        profile.electronic_notice_consent = electronic_notice_consent
        if consent_changed:
            profile.consent_changed_at = timezone.now()
        profile.in_directory = in_directory
        profile.directory_show_email = in_directory and directory_show_email
        profile.directory_show_phone = in_directory and directory_show_phone
        profile.save()
        _audit(
            "profile.updated",
            actor=user,
            request=request,
            target=profile,
            metadata={
                "notice_consent": electronic_notice_consent,
                "consent_changed": consent_changed,
                "in_directory": in_directory,
            },
        )
    return profile


def directory_entries():
    """Current owners who opted in, with only the fields they chose to share."""
    from community.models import ContactProfile

    with system_access("directory: opted-in fields only"):
        profiles = {p.person_id: p for p in ContactProfile.objects.filter(in_directory=True)}
        ownerships = (
            Ownership.objects.active()
            .filter(person_id__in=profiles)
            .select_related("person", "lot")
            .order_by("lot__number")
        )
        entries = []
        for o in ownerships:
            p = profiles[o.person_id]
            entries.append(
                {
                    "name": o.person.display_name,
                    "lot": o.lot.number,
                    "street_address": o.lot.street_address,
                    "email": o.person.email if p.directory_show_email else "",
                    "phone": p.phone if p.directory_show_phone else "",
                }
            )
    return entries
