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
from core.policy import SYSTEM, can, require, roles_for
from core.policy import invalidate_roles as _invalidate_roles
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


def record_lot(
    *, number: int, street_address: str, actor, request: HttpRequest | None = None
) -> Lot:
    """A new home on the register (the register import, D54). Lots are never
    deleted; an existing number is refused."""
    _authorize(actor, "ownership.manage")
    street_address = " ".join((street_address or "").split())[:200]
    if not 1 <= int(number) <= 9999:
        raise CommunityError("A lot number is a whole number from 1 to 9999.")
    if not street_address:
        raise CommunityError("A home needs its street address.")
    with transaction.atomic(), system_access("record_lot"):
        if Lot.objects.filter(number=number).exists():
            raise CommunityError("That lot number is already on the register.")
        lot = Lot.objects.create(number=number, street_address=street_address)
        _audit(
            "lot.recorded", actor=actor, request=request, target=lot, metadata={"lot_id": lot.pk}
        )
    return lot


def record_imported_contact(
    person: Person,
    *,
    phone: str = "",
    mailing: dict | None = None,
    actor,
    request: HttpRequest | None = None,
) -> None:
    """Contact details from the register import (D54) for a person the import
    has just recorded: a phone on their Phone list, and a mailing address for
    post when the register gives one. Never overwrites a choice the person
    made: a mailing address is set only while they have none."""
    from community.models import ContactProfile

    _authorize(actor, "person.manage")
    with transaction.atomic(), system_access("record imported contact"):
        if phone:
            phone = _valid_phone(phone.strip())
            if not ContactMethod.objects.filter(
                person=person, kind=ContactMethodKind.PHONE, value=phone
            ).exists():
                ContactMethod.objects.create(
                    person=person, kind=ContactMethodKind.PHONE, value=phone[:300]
                )
                _sync_profile_phone(person.pk)
                _audit(
                    "contact.method_added",
                    actor=actor,
                    request=request,
                    target=person,
                    metadata={"kind": ContactMethodKind.PHONE},
                )
        if mailing:
            profile = ContactProfile.objects.filter(person=person).first() or ContactProfile(
                person=person
            )
            if profile.mail_to_home is None and not profile.mail_street:
                profile.mail_to_home = False
                profile.mail_street = mailing["street"][:200]
                profile.mail_city = mailing["city"][:80]
                profile.mail_state = mailing["state"]
                profile.mail_zip = mailing["zip"]
                profile.save()
                _audit(
                    "profile.updated",
                    actor=actor,
                    request=request,
                    target=profile,
                    metadata={"mailing": "other", "via": "register_import"},
                )


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
        with (
            transaction.atomic(),
            system_access("record_ownership"),
            _periods_change(lot.pk, actor=actor, request=request),
        ):
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
        raise CommunityError("That person already owns this home during that period.") from exc
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
    with (
        transaction.atomic(),
        system_access("end_ownership"),
        _periods_change(ownership.lot_id, actor=actor, request=request),
    ):
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
    with (
        transaction.atomic(),
        system_access("transfer_lot"),
        _periods_change(lot.pk, actor=actor, request=request),
    ):
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


def _periods_change(lot_id: int, *, actor, request):
    """Ownership changes move account periods; entries already posted for a
    period that now belongs to another account follow it (D34-fixmoney-1)."""
    from ledger.services import account_periods_change

    return account_periods_change(lot_id, actor=actor, request=request)


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
        # A co-owner whose deed date is earlier than the current account's
        # opening (e.g. recorded later, or a register import) joins the open
        # account, which then counts from that earlier date - as long as no
        # earlier, closed account covers it.
        current = (
            Account.objects.select_for_update().filter(lot=lot, closed_on__isnull=True).first()
        )
        overlaps = Account.objects.filter(lot=lot, closed_on__gt=on).exclude(closed_on__isnull=True)
        if current is not None and not overlaps.exists():
            Account.objects.filter(pk=current.pk).update(opened_on=on)
            current.opened_on = on
            return current
        account = Account.objects.create(lot=lot, number=_next_account_number(lot), opened_on=on)
    return account


@internal("community accounts")
def _close_account_if_empty(account_id: int | None, on: datetime.date) -> None:
    if account_id is None:
        return
    still_owned = Ownership.objects.filter(account_id=account_id).active(on).exists()
    if still_owned:
        return
    Account.objects.filter(pk=account_id, closed_on__isnull=True).update(
        closed_on=on, closed_recorded_at=timezone.now()
    )
    # Contacts and tenants of a dormant account lose access with it.
    AccountContact.objects.filter(account_id=account_id, end_date__isnull=True).update(end_date=on)
    _invalidate_roles()


@internal("community accounts")
def _reopen_account(account_id: int | None) -> None:
    if account_id is not None:
        Account.objects.filter(pk=account_id).update(closed_on=None, closed_recorded_at=None)


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
        _invalidate_roles()
    _audit(
        "account.contact_ended",
        actor=actor,
        request=request,
        target=contact.account,
        metadata={"person_id": contact.person_id, "kind": contact.kind},
    )
    return contact


def _may_edit_methods(actor, person_id: int) -> None:
    if actor is not SYSTEM and not (_person_of(actor) == person_id or can(actor, "person.manage")):
        raise PermissionDenied("contact_method")
    _refuse_read_only(actor)


def _valid_phone(value: str) -> str:
    """A phone number: 7 to 15 digits, with only the usual punctuation
    (+ ( ) . - and spaces); returned as typed, trimmed."""
    import re

    digits = re.sub(r"\D", "", value)
    if not 7 <= len(digits) <= 15 or re.search(r"[^0-9+().\- ]", value):
        raise CommunityError("Enter a phone number, for example 215-555-0123.")
    return value


def _sync_profile_phone(person_id: int) -> None:
    """ContactProfile.phone mirrors the first number on the Phone list, so
    the directory and printed forms keep reading one phone (D44)."""
    from community.models import ContactProfile

    first = (
        ContactMethod.objects.filter(person_id=person_id, kind=ContactMethodKind.PHONE)
        .order_by("id")
        .values_list("value", flat=True)
        .first()
    ) or ""
    profile = ContactProfile.objects.filter(person_id=person_id).first()
    if profile is None and not first:
        return
    if profile is None:
        profile = ContactProfile(person_id=person_id)
    if profile.phone != first:
        profile.phone = first[:40]
        profile.save()


def add_contact_method(
    person: Person,
    *,
    kind: str,
    value: str,
    label: str = "",
    notices: bool = False,
    phone_type: str = "",
    actor,
    request: HttpRequest | None = None,
) -> ContactMethod:
    """The person themselves (or the board) adds another email or a phone.

    Mailing addresses are no longer list entries (D44): each person has one
    postal address, set with ``set_mailing_address``. Adding a phone never
    turns on text alerts (those need a texted code, comms.sms)."""
    from community.models import ContactProfile, PhoneType

    _may_edit_methods(actor, person.pk)
    value = (value or "").strip()
    if kind == ContactMethodKind.ADDRESS:
        raise CommunityError("Mailing addresses go in the Mailing address for post section.")
    if kind not in ContactMethodKind.values or not value or len(value) > 300:
        raise CommunityError("Choose a kind and enter the details.")
    if kind == ContactMethodKind.EMAIL:
        from django.core.validators import validate_email

        try:
            validate_email(value)
        except ValidationError as exc:
            raise CommunityError("Enter a valid email address.") from exc
        phone_type = ""
    else:
        value = _valid_phone(value)
        if phone_type not in PhoneType.values and not (actor is SYSTEM and not phone_type):
            raise CommunityError("Choose Mobile, Home or Work.")
        notices = False
    with transaction.atomic(), system_access("add contact method"):
        if ContactMethod.objects.filter(person=person).count() >= 10:
            raise CommunityError("Ten extra contact details is the most.")
        sign_in = person.user.email.lower() if person.user_id else ""
        if (
            ContactMethod.objects.filter(person=person, kind=kind, value__iexact=value).exists()
            or value.lower() == sign_in
        ):
            raise CommunityError("That is already on your list.")
        if notices:
            # Extra notice addresses only for people who chose email notices.
            notices = ContactProfile.objects.filter(
                person=person, electronic_notice_consent=True
            ).exists()
        method = ContactMethod.objects.create(
            person=person,
            kind=kind,
            value=value,
            label=label.strip()[:60],
            notices=notices,
            phone_type=phone_type,
        )
        if kind == ContactMethodKind.PHONE:
            _sync_profile_phone(person.pk)
    _audit(
        "contact.method_added", actor=actor, request=request, target=person, metadata={"kind": kind}
    )
    return method


def set_method_notices(
    method: ContactMethod, *, notices: bool, actor, request: HttpRequest | None = None
) -> ContactMethod:
    """ "Also send official notices here" on one of the person's extra emails."""
    from community.models import ContactProfile

    _may_edit_methods(actor, method.person_id)
    if method.kind != ContactMethodKind.EMAIL:
        raise CommunityError("Only email addresses receive notices.")
    with transaction.atomic(), system_access("set method notices"):
        if (
            notices
            and not ContactProfile.objects.filter(
                person_id=method.person_id, electronic_notice_consent=True
            ).exists()
        ):
            raise CommunityError("Choose email for official notices first.")
        row = ContactMethod.objects.select_for_update().get(pk=method.pk)
        row.notices = notices
        row.save(update_fields=["notices"])
        method.notices = notices
    _audit(
        "contact.method_changed",
        actor=actor,
        request=request,
        target=method.person,
        metadata={"kind": method.kind, "notices": notices},
    )
    return method


def remove_contact_method(
    method: ContactMethod, *, actor, request: HttpRequest | None = None
) -> None:
    _may_edit_methods(actor, method.person_id)
    with transaction.atomic(), system_access("remove contact method"):
        row = ContactMethod.objects.filter(pk=method.pk).first()
        if row is not None:
            row.delete()  # through the model, so the change history keeps a copy
        if method.kind == ContactMethodKind.PHONE:
            _sync_profile_phone(method.person_id)
    _audit(
        "contact.method_removed",
        actor=actor,
        request=request,
        target=method.person,
        metadata={"kind": method.kind},
    )


# --- Official notices and the mailing address for post (D44) ---------------------------

US_STATES = [
    "AL",
    "AK",
    "AZ",
    "AR",
    "CA",
    "CO",
    "CT",
    "DE",
    "DC",
    "FL",
    "GA",
    "HI",
    "ID",
    "IL",
    "IN",
    "IA",
    "KS",
    "KY",
    "LA",
    "ME",
    "MD",
    "MA",
    "MI",
    "MN",
    "MS",
    "MO",
    "MT",
    "NE",
    "NV",
    "NH",
    "NJ",
    "NM",
    "NY",
    "NC",
    "ND",
    "OH",
    "OK",
    "OR",
    "PA",
    "RI",
    "SC",
    "SD",
    "TN",
    "TX",
    "UT",
    "VT",
    "VA",
    "WA",
    "WV",
    "WI",
    "WY",
    "PR",
    "VI",
    "GU",
    "AS",
    "MP",
    "AA",
    "AE",
    "AP",
]


def _own_profile(user, reason: str):
    """(person, profile locked for update) of this login; call inside atomic()."""
    from community.models import ContactProfile

    with system_access(reason):
        person = Person.objects.filter(user=user).first()
        if person is None:
            raise CommunityError("Your login is not linked to a person yet.")
        profile, _ = ContactProfile.objects.select_for_update().get_or_create(person=person)
    return person, profile


def record_notice_choice(
    *, user, consent: bool, via: str = "profile", request: HttpRequest | None = None
):
    """The person's own answer: official notices by email (True) or by post.

    Either answer is recorded with the time and the wording version shown
    (community.notices), so the association can show what was agreed; the
    sign-up step and the My items to-do stop once there is an answer."""
    from community.notices import CONSENT_VERSION

    _refuse_read_only(user)
    with transaction.atomic():
        _person, profile = _own_profile(user, "record notice choice")
        changed = profile.electronic_notice_consent != bool(consent)
        profile.electronic_notice_consent = bool(consent)
        profile.consent_changed_at = timezone.now()
        profile.notice_consent_version = CONSENT_VERSION
        profile.save()
        _audit(
            "profile.updated",
            actor=user,
            request=request,
            target=profile,
            metadata={
                "notice_consent": bool(consent),
                "consent_changed": changed,
                "notice_consent_version": CONSENT_VERSION,
                "via": "signup" if via == "signup" else "profile",
            },
        )
    return profile


def set_mailing_address(
    *,
    user,
    to_home: bool,
    street: str = "",
    unit: str = "",
    city: str = "",
    state: str = "PA",
    zip_code: str = "",
    request: HttpRequest | None = None,
):
    """The one postal address for this person's paper notices and letters:
    their home's address, or a structured address they enter."""
    import re

    _refuse_read_only(user)
    street, unit, city = (" ".join((v or "").split()) for v in (street, unit, city))
    state = (state or "").strip().upper()
    zip_code = (zip_code or "").strip()
    if not to_home:
        problems = []
        if not street:
            problems.append("Enter the street address.")
        if not city:
            problems.append("Enter the city or town.")
        if state not in US_STATES:
            problems.append("Choose a state.")
        if not re.fullmatch(r"[0-9]{5}(-?[0-9]{4})?", zip_code):
            problems.append("Enter a 5-digit ZIP code (or ZIP+4, like 19380-1234).")
        if problems:
            raise CommunityError(" ".join(problems))
        if len(zip_code) == 9:
            zip_code = f"{zip_code[:5]}-{zip_code[5:]}"
    with transaction.atomic():
        _person, profile = _own_profile(user, "set mailing address")
        profile.mail_to_home = bool(to_home)
        if not to_home:
            profile.mail_street, profile.mail_unit = street[:200], unit[:60]
            profile.mail_city, profile.mail_state, profile.mail_zip = city[:80], state, zip_code
        profile.save()
        _audit(
            "profile.updated",
            actor=user,
            request=request,
            target=profile,
            metadata={"mailing": "home" if to_home else "other", "via": "profile"},
        )
    return profile


def structured_address(profile) -> str:
    """The structured mailing address as letter lines ("" if none)."""
    if not profile or not profile.mail_street:
        return ""
    first = profile.mail_street + (f", {profile.mail_unit}" if profile.mail_unit else "")
    second = f"{profile.mail_city}, {profile.mail_state} {profile.mail_zip}".strip()
    return f"{first}\n{second}"


def old_address(person_id: int, profile=None) -> str:
    """Mailing address typed before D44 (free text or an old list entry)."""
    if profile is not None and profile.mailing_address.strip():
        return profile.mailing_address.strip()
    with system_access("old mailing address"):
        value = (
            ContactMethod.objects.filter(person_id=person_id, kind=ContactMethodKind.ADDRESS)
            .order_by("id")
            .values_list("value", flat=True)
            .first()
        )
    return (value or "").strip()


def postal_address(person: Person, lot: Lot | None = None) -> tuple[str, str]:
    """(address, source) for paper notices and letters to this person.

    The structured address they entered first; if they chose their home's
    address, that; otherwise an address typed before D44; otherwise the
    home's address. ``source`` is "structured", "home", "old" or "none"."""
    from community.models import ContactProfile

    with system_access("postal address"):
        profile = ContactProfile.objects.filter(person=person).first()
        if lot is None:
            own = Ownership.objects.active().filter(person=person).select_related("lot").first()
            lot = own.lot if own else None
    home = (lot.street_address or "").strip() if lot else ""
    if profile and profile.mail_to_home is False and profile.mail_street:
        return structured_address(profile), "structured"
    if not (profile and profile.mail_to_home):
        old = old_address(person.pk, profile)
        if old:
            return old, "old"
    return home, ("home" if home else "none")


def needs_address_check(person_id: int) -> bool:
    """An address typed before D44 that the person has not re-entered."""
    from community.models import ContactProfile

    with system_access("address check"):
        profile = ContactProfile.objects.filter(person_id=person_id).first()
    if profile is not None and profile.mail_to_home is not None:
        return False
    return bool(old_address(person_id, profile))


def notice_choice_due(user) -> bool:
    """Should this login be asked how to get official notices? Only current
    owners on the deed (official notices go to them: meeting, budget and
    collection notices), never tenants, view-only contacts or former owners,
    and only until they answer."""
    from community.models import ContactProfile
    from community.rules import rule

    if not getattr(user, "is_authenticated", False):
        return False
    roles = roles_for(user)
    if not roles.person_id or not roles.owned_lot_ids:
        return False
    if rule("notices.electronic_consent_required") is False:
        return False
    with system_access("notice choice due"):
        if not Ownership.objects.active().filter(person_id=roles.person_id).exists():
            return False
        answered = ContactProfile.objects.filter(
            person_id=roles.person_id, consent_changed_at__isnull=False
        ).exists()
    return not answered


def notice_choice_counts() -> dict[str, int]:
    """Homes by how they get official notices, for the board's Invitations
    and sign-ups page (the view checks ``invite.send``): "email" (an owner on
    the deed chose email), "paper" (every owner answered, none chose email)
    and "unanswered" (no email yet and someone has not answered; these homes
    get paper until then). Counts only, no names."""
    from community.models import ContactProfile

    with system_access("notice choice counts"):
        owners: dict[int, set[int]] = {}
        for lot_id, person_id in Ownership.objects.active().values_list("lot_id", "person_id"):
            owners.setdefault(lot_id, set()).add(person_id)
        profiles = {
            p["person_id"]: p
            for p in ContactProfile.objects.values(
                "person_id", "electronic_notice_consent", "consent_changed_at"
            )
        }
    out = {"email": 0, "paper": 0, "unanswered": 0}
    for people in owners.values():
        rows = [profiles.get(pid) for pid in people]
        if any(r and r["electronic_notice_consent"] for r in rows):
            out["email"] += 1
        elif all(r and r["consent_changed_at"] for r in rows):
            out["paper"] += 1
        else:
            out["unanswered"] += 1
    return out


def notice_emails(person: Person) -> list[str]:
    """Extra email addresses this person asked to receive notices at."""
    with system_access("notice emails"):
        return list(
            ContactMethod.objects.filter(
                person=person, kind=ContactMethodKind.EMAIL, notices=True
            ).values_list("value", flat=True)
        )


def has_board_seat(person: Person) -> bool:
    with system_access("has board seat"):
        return BoardSeat.objects.filter(person=person).active().exists()


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


def board_members_public(*, actor, on=None) -> list[dict]:
    """Board and committees page: each current board member's name and
    offices, nothing else (no contact details, lots or seat history), for
    everyone who sees the community pages."""
    from community.models import BoardSeat, Office, OfficeHolding

    require(actor, "board_committees.view")
    with system_access("board and committees page"):
        seated = dict(
            BoardSeat.objects.active(on)
            .order_by("person__display_name")
            .values_list("person_id", "person__display_name")
        )
        offices: dict[int, list[str]] = {}
        for person_id, office in OfficeHolding.objects.active(on).values_list(
            "person_id", "office"
        ):
            offices.setdefault(person_id, []).append(office)
    order = {value: i for i, value in enumerate(Office.values)}
    labels = dict(Office.choices)
    rows = []
    for person_id, name in seated.items():
        held = sorted((o for o in offices.get(person_id, []) if o in labels), key=order.get)
        rank = order[held[0]] if held else len(order)
        rows.append({"name": name, "offices": [labels[o] for o in held], "rank": rank})
    # Officers first, in the usual order; then the other directors by name.
    rows.sort(key=lambda r: (r["rank"], r["name"]))
    return rows


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
    _second_approval: bool = False,
) -> BoardSeat:
    _authorize(actor, "seat.manage")
    if seat_type not in SeatType.values:
        raise CommunityError("Unknown seat type.")
    if term_end is not None and term_end < term_start:
        raise CommunityError("A term cannot end before it starts.")
    _refuse_self_grant(actor, person, "a board seat")
    if seat_needs_second_approval(actor) and not _second_approval:
        raise CommunityError(
            "A new board seat needs a second board member's approval: propose it instead."
        )
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
    *,
    legal_name: str,
    fiscal_year_start_month: int,
    welcome_message: str | None = None,
    arc_form_return_to: str | None = None,
    arc_form_conditions: str | None = None,
    signin_help_contact: str | None = None,
    actor,
    request: HttpRequest | None = None,
):
    from community.models import CommunityProfile

    _authorize(actor, "community.manage_rules")
    if not 1 <= int(fiscal_year_start_month) <= 12:
        raise CommunityError("The fiscal year must start in month 1-12.")
    with transaction.atomic():
        profile = CommunityProfile.get()
        profile.legal_name = legal_name.strip()[:200] or profile.legal_name
        profile.fiscal_year_start_month = int(fiscal_year_start_month)
        if welcome_message is not None:
            profile.welcome_message = welcome_message.strip()[:2000]
        if arc_form_return_to is not None:
            profile.arc_form_return_to = " ".join(arc_form_return_to.split())[:300]
        if arc_form_conditions is not None:
            profile.arc_form_conditions = arc_form_conditions.strip()[:3000]
        if signin_help_contact is not None:
            profile.signin_help_contact = " ".join(signin_help_contact.split())[:200]
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
    phone: str | None = None,
    mailing_address: str | None = None,
    electronic_notice_consent: bool | None = None,
    in_directory: bool = False,
    directory_show_email: bool = False,
    directory_show_phone: bool = False,
    weekly_digest: bool | None = None,
    statement_emails: bool | None = None,
    request: HttpRequest | None = None,
):
    """Anyone with a linked Person edits only their own profile: the
    directory and routine-update choices. ``None`` leaves a field as it is.

    Since D44 My profile sets phones with ``add_contact_method``, the postal
    address with ``set_mailing_address`` and the notices choice with
    ``record_notice_choice``; ``phone``, ``mailing_address`` and
    ``electronic_notice_consent`` remain for older callers."""
    _refuse_read_only(user)
    from community.models import ContactProfile
    from community.notices import CONSENT_VERSION

    with transaction.atomic(), system_access("update_my_profile"):
        person = Person.objects.filter(user=user).first()
        if person is None:
            raise CommunityError("Your login is not linked to a person yet.")
        profile, _ = ContactProfile.objects.select_for_update().get_or_create(person=person)
        consent_changed = (
            electronic_notice_consent is not None
            and profile.electronic_notice_consent != electronic_notice_consent
        )
        if phone is not None:
            profile.phone = phone.strip()[:40]
        if mailing_address is not None:
            profile.mailing_address = mailing_address.strip()[:1000]
        if consent_changed:
            profile.electronic_notice_consent = electronic_notice_consent
            profile.consent_changed_at = timezone.now()
            profile.notice_consent_version = CONSENT_VERSION
        profile.in_directory = in_directory
        profile.directory_show_email = in_directory and directory_show_email
        profile.directory_show_phone = in_directory and directory_show_phone
        if weekly_digest is not None:
            profile.weekly_digest = weekly_digest
        if statement_emails is not None:
            profile.statement_emails = statement_emails
        profile.save()
        _audit(
            "profile.updated",
            actor=user,
            request=request,
            target=profile,
            metadata={
                "notice_consent": profile.electronic_notice_consent,
                "consent_changed": consent_changed,
                "in_directory": in_directory,
            },
        )
    return profile


def sms_settings(user) -> tuple[str, bool]:
    """(verified mobile number, opted in) of this login's own profile."""
    from community.models import ContactProfile

    with system_access("own sms settings"):
        profile = ContactProfile.objects.filter(person__user=user).first()
    if profile is None or not profile.sms_verified_at:
        return "", False
    return profile.sms_number, profile.sms_opt_in


def set_sms_number(*, user, number: str) -> None:
    """Save a number the owner just proved (comms.sms checks the code)."""
    _refuse_read_only(user)
    from community.models import ContactProfile

    with transaction.atomic(), system_access("set sms number"):
        person = Person.objects.filter(user=user).first()
        if person is None:
            raise CommunityError("Your login is not linked to a person yet.")
        profile, _ = ContactProfile.objects.select_for_update().get_or_create(person=person)
        profile.sms_number = number
        profile.sms_verified_at = timezone.now()
        profile.sms_opt_in = True
        profile.save()


def stop_sms(*, user=None, number: str = "") -> int:
    """Turn urgent texts off: for one login (their choice), or for every
    profile with ``number`` (the carrier reported a STOP reply)."""
    from community.models import ContactProfile

    with transaction.atomic(), system_access("stop sms"):
        if user is not None:
            profiles = ContactProfile.objects.filter(person__user=user)
        elif number:
            profiles = ContactProfile.objects.filter(sms_number=number)
        else:
            return 0
        changed = 0
        for profile in profiles.select_for_update():
            profile.sms_number = ""
            profile.sms_verified_at = None
            profile.sms_opt_in = False
            profile.save()
            changed += 1
    return changed


def sms_recipients() -> list[str]:
    """Verified numbers of current owners who opted in to urgent texts."""
    from community.models import ContactProfile

    with system_access("urgent text recipients"):
        numbers = ContactProfile.objects.filter(
            sms_opt_in=True,
            sms_verified_at__isnull=False,
            person__in=Ownership.objects.active().values("person_id"),
        ).exclude(sms_number="")
        return sorted(set(numbers.values_list("sms_number", flat=True)))


def board_roster_public():
    """Who is on the board now: names, seat type, office and term end only."""
    with system_access("directory: board roster"):
        offices = {
            h.person_id: h.get_office_display()
            for h in OfficeHolding.objects.active().select_related("person")
        }
        return [
            {
                "name": seat.person.display_name,
                "seat": seat.get_seat_type_display(),
                "office": offices.get(seat.person_id, ""),
                "term_end": seat.term_end,
            }
            for seat in BoardSeat.objects.active()
            .select_related("person")
            .order_by("person__display_name")
        ]


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
                    "address": o.lot.label,
                    "street_address": o.lot.street_address,
                    "email": o.person.email if p.directory_show_email else "",
                    "phone": p.phone if p.directory_show_phone else "",
                    "_sort": o.lot.sort_key,
                }
            )
    entries.sort(key=lambda e: e.pop("_sort"))
    return entries


# --- Owner-requested account changes (board approval) ---------------------------------


def _added_person_email(email: str, no_login: bool) -> str:
    """The email of someone an owner asks to add: required, because they sign
    in with it, unless the owner ticked "They don't use email" (D41)."""
    email = (email or "").strip()
    if email:
        from django.core.validators import validate_email

        try:
            validate_email(email)
        except ValidationError as exc:
            raise CommunityError("Enter a valid email address.") from exc
    elif not no_login:
        raise CommunityError(
            "Enter their email: they sign in with it. If they don't use email, tick"
            ' "They don\'t use email".'
        )
    return email


def _check_deed_date(deed_date) -> None:
    if not isinstance(deed_date, datetime.date):
        raise CommunityError("Enter the date on the recorded deed.")
    if deed_date > today():
        raise CommunityError("The deed date cannot be in the future.")


def _deed_before_account(account: Account, deed_date: datetime.date) -> bool:
    """A co-owner whose deed is dated before the account began would move
    the account's start (and possibly money between accounts): the board
    records that from the home's page instead."""
    return deed_date < account.opened_on


def _remaining_owners(account: Account, ownership: Ownership, on: datetime.date):
    """Owners of ``account`` other than ``ownership`` who hold it on ``on``
    and are still owners."""
    return (
        Ownership.objects.filter(account=account, end_date__isnull=True, start_date__lte=on)
        .exclude(pk=ownership.pk)
        .order_by("start_date", "id")
    )


LAST_OWNER_MESSAGE = (
    "The account must keep at least one owner on the deed. A sale to new owners is"
    " recorded by the board as a transfer."
)


def request_account_change(
    account: Account,
    *,
    action: str,
    kind: str = "",
    name: str = "",
    email: str = "",
    no_login: bool = False,
    contact: AccountContact | None = None,
    ownership_type: str = "",
    deed_date: datetime.date | None = None,
    ownership: Ownership | None = None,
    deed=None,
    reason: str = "",
    actor,
    request=None,
):
    """An owner on the deed asks to add or remove a contact on their account,
    or (with the recorded deed attached) an owner on the deed (D41)."""
    from community.models import (
        ADDED_OWNER_TYPES,
        DEED_FILE_LABEL,
        AccountChangeAction,
        AccountChangeRequest,
    )

    require(actor, "account.request_change")
    _refuse_read_only(actor)
    if actor is not SYSTEM and account.lot_id not in roles_for(actor).owned_lot_ids:
        raise PermissionDenied("account_change")
    if not account.is_active:
        raise CommunityError("This account is dormant.")
    fields = {"reason": (reason or "").strip()[:2000]}
    if action in (AccountChangeAction.ADD, AccountChangeAction.ADD_OWNER):
        name = " ".join((name or "").split())[:200]
        if not name:
            raise CommunityError("Enter their name.")
        no_login = bool(no_login)
        fields.update(name=name, email=_added_person_email(email, no_login), no_login=no_login)
        if action == AccountChangeAction.ADD:
            if kind not in ContactKind.values:
                raise CommunityError("Choose what they may do.")
            fields["kind"] = kind
        else:
            if ownership_type not in ADDED_OWNER_TYPES:
                raise CommunityError("Choose how they hold the deed: joint owner, trust or entity.")
            _check_deed_date(deed_date)
            if _deed_before_account(account, deed_date):
                raise CommunityError(
                    "The deed is dated before your account began. Send the board a message"
                    " instead: they record it from the home's page."
                )
            fields.update(ownership_type=ownership_type, deed_date=deed_date)
    elif action == AccountChangeAction.REMOVE:
        if contact is None or contact.account_id != account.pk or contact.end_date is not None:
            raise CommunityError("Choose someone currently on this account.")
        fields.update(contact=contact, kind=contact.kind)
    elif action == AccountChangeAction.REMOVE_OWNER:
        if (
            ownership is None
            or ownership.account_id != account.pk
            or ownership.end_date is not None
        ):
            raise CommunityError("Choose a current owner on this account.")
        _check_deed_date(deed_date)
        if deed_date < ownership.start_date:
            raise CommunityError("The deed is dated before they became an owner.")
        fields.update(ownership=ownership, deed_date=deed_date)
    else:
        raise CommunityError("Choose add or remove.")
    owner_change = action in (AccountChangeAction.ADD_OWNER, AccountChangeAction.REMOVE_OWNER)
    if owner_change and deed is None:
        raise CommunityError("Attach a copy of the recorded deed.")
    with transaction.atomic(), system_access("request account change"):
        Account.objects.select_for_update().filter(pk=account.pk).exists()
        pending = AccountChangeRequest.objects.filter(account=account, state="pending")
        if pending.count() >= 10:
            raise CommunityError("There are already several changes waiting for the board.")
        if action == AccountChangeAction.REMOVE_OWNER:
            if not _remaining_owners(account, ownership, deed_date).exists():
                raise CommunityError(LAST_OWNER_MESSAGE)
            if pending.filter(action=action, ownership=ownership).exists():
                raise CommunityError("That change is already waiting for the board.")
        change = AccountChangeRequest.objects.create(
            account=account, action=action, requested_by=actor, **fields
        )
        if owner_change:
            from documents import attachments
            from documents.services import DocumentError

            try:
                attachments.attach(
                    change,
                    deed,
                    actor=_user(actor),
                    label=DEED_FILE_LABEL,
                    allowed=attachments.IMAGES_AND_PDF,
                    request=request,
                )
            except DocumentError as exc:
                raise CommunityError(str(exc)) from exc
        _audit(
            "account.change_requested",
            actor=actor,
            request=request,
            target=account,
            metadata={"action": action, "kind": fields.get("kind", "")},
        )
        _board_notice("account_change_requested")
    return change


def _refuse_own_owner_change(change, actor) -> None:
    """No self-grants (D41): the approver is not the person added to, or
    removed from, the deed."""
    from community.models import AccountChangeAction

    if actor is SYSTEM:
        return
    if change.action == AccountChangeAction.REMOVE_OWNER:
        mine = _person_of(actor)
        if mine is not None and mine == change.ownership.person_id:
            raise CommunityError("Another board member must decide a change to your own ownership.")
    elif change.action == AccountChangeAction.ADD_OWNER and change.email:
        from accounts.models import normalize_email_address

        addresses = {normalize_email_address(actor.email or "")}
        with system_access("self-grant check"):
            person = Person.objects.filter(user=actor).first()
        if person is not None:
            addresses.add(normalize_email_address(person.email or ""))
        if normalize_email_address(change.email) in addresses - {""}:
            raise CommunityError("Another board member must decide a change that adds you.")


def set_ownership_type(
    ownership: Ownership, ownership_type: str, *, actor, request: HttpRequest | None = None
) -> Ownership:
    """Correct how an owner holds the deed (e.g. a sole owner becomes a joint
    owner when a co-owner is added). Kept in the ownership's history."""
    _authorize(actor, "ownership.manage")
    if ownership_type not in OwnershipType.values:
        raise CommunityError("Unknown ownership type.")
    with transaction.atomic(), system_access("set ownership type"):
        ownership = Ownership.objects.select_for_update().get(pk=ownership.pk)
        if ownership.ownership_type == ownership_type:
            return ownership
        ownership.ownership_type = ownership_type
        ownership.save(update_fields=["ownership_type"])
        _audit(
            "ownership.type_changed",
            actor=actor,
            request=request,
            target=ownership,
            metadata={
                "lot_id": ownership.lot_id,
                "person_id": ownership.person_id,
                "ownership_type": ownership_type,
            },
        )
    return ownership


def _approve_owner_change(change, account: Account, *, actor, request):
    """Make an approved deed change through the ownership services. Returns
    the person added, if any."""
    from community.models import AccountChangeAction

    if not account.is_active:
        raise CommunityError("This account is dormant.")
    if change.action == AccountChangeAction.ADD_OWNER:
        if _deed_before_account(account, change.deed_date):
            raise CommunityError(
                "The deed is dated before this account began: record this owner from the"
                " home's page (Owners), where an earlier date is handled."
            )
        person = record_person(
            display_name=change.name, email=change.email, actor=actor, request=request
        )
        added = record_ownership(
            person=person,
            lot=account.lot,
            start_date=change.deed_date,
            ownership_type=change.ownership_type,
            actor=actor,
            request=request,
        )
        if added.account_id != account.pk:
            raise CommunityError("This owner would not join the current account.")
        for other in Ownership.objects.filter(
            account=account, end_date__isnull=True, ownership_type=OwnershipType.SOLE
        ).exclude(pk=added.pk):
            set_ownership_type(other, OwnershipType.JOINT, actor=actor, request=request)
        return person
    ownership = Ownership.objects.get(pk=change.ownership_id)
    if ownership.end_date is not None:
        raise CommunityError("This ownership has already ended.")
    if not _remaining_owners(account, ownership, change.deed_date).exists():
        raise CommunityError(LAST_OWNER_MESSAGE)
    end_ownership(ownership, end_date=change.deed_date, actor=actor, request=request)
    left = list(Ownership.objects.filter(account=account, end_date__isnull=True))
    if len(left) == 1 and left[0].ownership_type == OwnershipType.JOINT:
        set_ownership_type(left[0], OwnershipType.SOLE, actor=actor, request=request)
    return None


def decide_account_change(change, *, approve: bool, note: str = "", actor, request=None):
    """A board member other than the requester (and, for a deed change, not
    the person added or removed) approves or declines. The change is made
    through the normal services as that board member. An added person with
    an email is then invited to sign in; if the invitation cannot be made,
    the approval stands and ``invitation_sent`` is False (D41)."""
    from community.models import AccountChangeAction, AccountChangeRequest

    require(actor, "account.approve_change")
    person = None
    with transaction.atomic(), system_access("decide account change"):
        change = AccountChangeRequest.objects.select_for_update().get(pk=change.pk)
        if change.state != "pending":
            raise CommunityError("This request has already been decided.")
        if actor is not SYSTEM and change.requested_by_id == actor.pk:
            raise CommunityError("Another board member must decide a change you asked for.")
        if approve:
            account = Account.objects.select_related("lot").get(pk=change.account_id)
            if change.is_owner_change:
                _refuse_own_owner_change(change, actor)
                person = _approve_owner_change(change, account, actor=actor, request=request)
            elif change.action == AccountChangeAction.ADD:
                person = record_person(
                    display_name=change.name, email=change.email, actor=actor, request=request
                )
                add_account_contact(
                    account, person=person, kind=change.kind, actor=actor, request=request
                )
            else:
                contact = AccountContact.objects.get(pk=change.contact_id)
                end_account_contact(contact, actor=actor, request=request)
        AccountChangeRequest.objects.filter(pk=change.pk).update(
            state="approved" if approve else "declined",
            decided_by=None if actor is SYSTEM else actor,
            decided_at=timezone.now(),
            decision_note=(note or "").strip()[:2000],
            person=person,
        )
        _audit(
            "account.change_decided",
            actor=actor,
            request=request,
            target=change.account,
            metadata={"action": change.action, "decision": "approved" if approve else "declined"},
        )
    if approve and person is not None and change.will_invite:
        # After the approval is saved: a failed invitation never undoes it.
        from accounts.services import InvitationError, create_invitation

        try:
            create_invitation(person=person, actor=actor, request=request)
        except (InvitationError, PermissionDenied):
            sent = False
        else:
            sent = True
        with system_access("record invitation outcome"):
            AccountChangeRequest.objects.filter(pk=change.pk).update(invitation_sent=sent)
    change.refresh_from_db()
    return change


def withdraw_account_change(change, *, actor, request=None):
    from community.models import AccountChangeRequest

    if actor is not SYSTEM and change.requested_by_id != actor.pk:
        raise PermissionDenied("account_change")
    with system_access("withdraw account change"):
        updated = AccountChangeRequest.objects.filter(pk=change.pk, state="pending").update(
            state="withdrawn"
        )
    if not updated:
        raise CommunityError("Only a waiting request can be withdrawn.")
    _audit(
        "account.change_withdrawn",
        actor=actor,
        request=request,
        target=change.account,
        metadata={"action": change.action},
    )


# --- Board roster ------------------------------------------------------------------------


def roster_rows(*, actor, include_dormant: bool = False) -> list[dict]:
    """One row per account: number, lot, address, owners, other people,
    contact details on record, balance. Dormant (previous) accounts only when
    asked. For the board, manager and auditor."""
    require(actor, "roster.view")
    from community.models import ContactProfile
    from ledger.services import account_balances

    with system_access("board roster"):
        accounts = Account.objects.select_related("lot").order_by("lot__number", "-opened_on")
        if not include_dormant:
            accounts = accounts.filter(closed_on__isnull=True)
        accounts = sorted(accounts, key=lambda a: a.lot.sort_key)  # address order (D40)
        ownerships = list(
            Ownership.objects.filter(account__in=accounts).select_related("person", "account")
        )
        contacts = list(
            AccountContact.objects.filter(account__in=accounts).select_related("person")
        )
        phones = dict(ContactProfile.objects.values_list("person_id", "phone"))
        owed = account_balances()
    by_account: dict[int, dict] = {a.pk: {"owners": [], "contacts": []} for a in accounts}
    for o in ownerships:
        if o.account_id in by_account and (o.end_date is None or not o.account.is_active):
            by_account[o.account_id]["owners"].append(o.person)
    for c in contacts:
        if c.end_date is None:
            by_account[c.account_id]["contacts"].append(c)
    rows = []
    for a in accounts:
        owners = by_account[a.pk]["owners"]
        rows.append(
            {
                "account": a,
                "lot": a.lot,
                "owners": owners,
                "contacts": by_account[a.pk]["contacts"],
                "emails": [p.email for p in owners if p.email],
                "phones": [phones[p.pk] for p in owners if phones.get(p.pk)],
                "balance_cents": owed.get(a.pk, 0) if a.is_active else None,
            }
        )
    return rows


# --- Board seats with a second approval (decision D21) ------------------------------------


def seat_needs_second_approval(actor) -> bool:
    """On in production. Exempt: trusted internal code, and the start-up phase
    while fewer than two board members hold seats (nobody to approve)."""
    if actor is SYSTEM or not settings.BOARD_SEAT_SECOND_APPROVAL:
        return False
    with system_access("count board seats"):
        return BoardSeat.objects.active().count() >= 2


def propose_board_seat(
    *,
    person: Person,
    seat_type: str,
    term_start: datetime.date,
    term_end: datetime.date | None = None,
    citation: str = "",
    actor,
    request: HttpRequest | None = None,
):
    """Record the seat now if no second approval is needed, otherwise create a
    nomination for another board member. Returns ("recorded", seat) or
    ("proposed", nomination)."""
    from community.models import BoardSeatNomination

    _authorize(actor, "seat.manage")
    if not seat_needs_second_approval(actor):
        return "recorded", record_board_seat(
            person=person,
            seat_type=seat_type,
            term_start=term_start,
            term_end=term_end,
            citation=citation,
            actor=actor,
            request=request,
        )
    if seat_type not in SeatType.values:
        raise CommunityError("Unknown seat type.")
    if term_end is not None and term_end < term_start:
        raise CommunityError("A term cannot end before it starts.")
    _refuse_self_grant(actor, person, "a board seat")
    with transaction.atomic(), system_access("propose board seat"):
        if BoardSeatNomination.objects.filter(person=person, outcome="").exists():
            raise CommunityError("There is already a proposed seat for this person.")
        nomination = BoardSeatNomination.objects.create(
            person=person,
            seat_type=seat_type,
            term_start=term_start,
            term_end=term_end,
            citation=citation.strip()[:200],
            nominated_by=_user(actor),
        )
        _audit(
            "seat.proposed",
            actor=actor,
            request=request,
            target=nomination,
            metadata={"person_id": person.pk, "seat_type": seat_type},
        )
        _board_notice("board_seat_proposed")
    return "proposed", nomination


def decide_seat_nomination(nomination, *, approve: bool, actor, request=None):
    """A board member other than the proposer (and not the nominee) approves,
    which records the seat as them, or declines. The proposer may withdraw."""
    from community.models import BoardSeatNomination

    _authorize(actor, "seat.manage")
    with transaction.atomic(), system_access("decide seat nomination"):
        nomination = BoardSeatNomination.objects.select_for_update().get(pk=nomination.pk)
        if nomination.outcome:
            raise CommunityError("This proposal has already been decided.")
        if approve:
            if nomination.nominated_by_id == _user(actor).pk:
                raise CommunityError("A different board member must approve your proposal.")
            seat = record_board_seat(
                person=nomination.person,
                seat_type=nomination.seat_type,
                term_start=nomination.term_start,
                term_end=nomination.term_end,
                citation=nomination.citation,
                actor=actor,
                request=request,
                _second_approval=True,
            )
        else:
            seat = None
        BoardSeatNomination.objects.filter(pk=nomination.pk).update(
            outcome="approved" if approve else "declined",
            decided_by=_user(actor),
            decided_at=timezone.now(),
            seat=seat,
        )
        _audit(
            "seat.proposal_decided",
            actor=actor,
            request=request,
            target=nomination,
            metadata={
                "person_id": nomination.person_id,
                "decision": "approved" if approve else "declined",
            },
        )
    nomination.refresh_from_db()
    return nomination


def owner_notice_recipients() -> tuple[list[str], list[Person]]:
    """Official notices: (emails of current owners who consented to electronic
    notice, plus their consented extra addresses; owners who need paper)."""
    from community.models import ContactProfile

    with system_access("notice recipients"):
        owners = list(
            Person.objects.filter(
                pk__in=Ownership.objects.active().values("person_id")
            ).select_related("user")
        )
        consent = set(
            ContactProfile.objects.filter(electronic_notice_consent=True).values_list(
                "person_id", flat=True
            )
        )
    emails, paper = [], []
    for person in owners:
        address = person.user.email if person.user_id else person.email
        if person.pk in consent and address:
            emails.append(address)
            emails.extend(e for e in notice_emails(person) if e not in emails)
        else:
            paper.append(person)
    return sorted(set(emails)), paper


def _address_key(address: str) -> tuple[str, ...]:
    """Two postal addresses are the same letter when they match line by line
    ignoring case, spacing and punctuation at the ends."""
    return tuple(
        " ".join(line.split()).strip(" ,.").casefold()
        for line in address.splitlines()
        if line.strip()
    )


def _names(names: list[str]) -> str:
    if len(names) <= 2:
        return " & ".join(names)
    return ", ".join(names[:-1]) + " & " + names[-1]


def paper_mail_groups(*, everyone: bool = False, lot_id: int | None = None) -> list[dict]:
    """Who gets a paper letter (D55): one letter per home and postal address.

    Current owners on the deed only (tenants, contacts and former owners
    never). Unless ``everyone``, an owner who agreed to official notices by
    email and has an email address is left out, exactly as the notice emails
    decide (governance.services.send_notice, owner_notice_recipients);
    everyone else, including owners who withdrew their consent or never
    answered, gets paper. Owners of one home who share a postal address are
    named on one letter; a different address gets its own letter.
    ``lot_id`` limits it to one home. Internal: the caller checks
    ``mail.print``. Returns dicts with lot, people (ids), names, address."""
    from community.models import ContactProfile

    with system_access("paper mail recipients"):
        ownerships = Ownership.objects.active().select_related("person", "person__user", "lot")
        if lot_id is not None:
            ownerships = ownerships.filter(lot_id=lot_id)
        ownerships = list(ownerships.order_by("lot_id", "start_date", "pk"))
        consent = set(
            ContactProfile.objects.filter(electronic_notice_consent=True).values_list(
                "person_id", flat=True
            )
        )
    groups: dict[tuple, dict] = {}
    seen: set[tuple[int, int]] = set()
    for own in ownerships:
        person, lot = own.person, own.lot
        if (lot.pk, person.pk) in seen:
            continue
        seen.add((lot.pk, person.pk))
        email = person.user.email if person.user_id else person.email
        if not everyone and person.pk in consent and email:
            continue
        address, _source = postal_address(person, lot)
        key = (lot.pk, _address_key(address))
        group = groups.setdefault(
            key, {"lot": lot, "people": [], "names": [], "address": address.strip()}
        )
        group["people"].append(person.pk)
        group["names"].append(person.display_name)
    out = list(groups.values())
    out.sort(key=lambda g: (g["lot"].sort_key, g["names"]))
    for g in out:
        g["owner_names"] = _names(g["names"])
    return out


def paper_mail_letters(groups: list[dict]) -> list[dict]:
    """The letters of a saved mailing, from its ``[{"lot", "people"}]``:
    names and the postal address as they are now (the first person's
    address), plus the home and its current account number. Internal."""
    from community.models import Account

    lot_ids = {int(g["lot"]) for g in groups}
    person_ids = {int(p) for g in groups for p in g["people"]}
    with system_access("paper mail letters"):
        lots = {lot.pk: lot for lot in Lot.objects.filter(pk__in=lot_ids)}
        people = {p.pk: p for p in Person.objects.filter(pk__in=person_ids)}
        numbers = dict(
            Account.objects.filter(lot_id__in=lot_ids, closed_on__isnull=True).values_list(
                "lot_id", "number"
            )
        )
    out = []
    for index, g in enumerate(groups):
        lot = lots.get(int(g["lot"]))
        members = [people[int(p)] for p in g["people"] if int(p) in people]
        if lot is None or not members:
            continue
        address, _source = postal_address(members[0], lot)
        names = [p.display_name for p in members]
        out.append(
            {
                "index": index,
                "lot": lot,
                "names": names,
                "owner_names": _names(names),
                "address": address.strip(),
                "account_number": str(numbers.get(lot.pk) or ""),
            }
        )
    return out


# --- Letters and resale certificate settings (Milestone 4) ----------------------------

LETTER_FIELDS = ("letterhead", "remit_to", "letter_contact", "letter_signature")
RESALE_TEXT_FIELDS = (
    "resale_restraints",
    "resale_other_fees",
    "resale_capital_expenditures",
    "resale_reserve_designations",
    "resale_judgments",
    "resale_insurance_company",
)
RESALE_MONEY_FIELDS = ("resale_reserves_cents", "resale_insurance_cents")


def update_letter_settings(*, actor, request=None, **values):
    """Letterhead, remittance, contact and signature; resale certificate facts."""
    from community.models import CommunityProfile

    _authorize(actor, "letters.manage")
    with transaction.atomic():
        profile = CommunityProfile.get()
        for field in LETTER_FIELDS + RESALE_TEXT_FIELDS:
            if field in values:
                limit = CommunityProfile._meta.get_field(field).max_length or 3000
                setattr(profile, field, (values[field] or "").strip()[:limit])
        for field in RESALE_MONEY_FIELDS:
            if field in values:
                value = values[field]
                if value is not None and int(value) < 0:
                    raise CommunityError("Amounts cannot be negative.")
                setattr(profile, field, value)
        profile.save()
        _audit(
            "community.letters_changed", actor=actor, request=request, target=profile, metadata={}
        )
    return profile
