"""Retention of former owners' contact details (Milestone 4).

Rule ``retention.former_owner_contact_days`` (community/rules.py, a board
privacy policy; 0 means never). A person qualifies when:

* they were an owner or an account contact at some time;
* every ownership and account contact has ended (a contact also ends when
  its account goes dormant) more than that many days ago;
* they hold no active board seat, office or role.

For them the portal clears their phone, mailing address and directory
choices, and removes their extra contact methods. Their name, login and
every record (ledger, ARC, violations, votes, audit) are kept: those are the
association's records and are never edited here. Each run writes an
append-only ``RetentionRun`` with counts only, and an audit event.

Limit: the change history of the contact profile (django-simple-history) is
insert-only for the runtime database role by design (D8), so earlier
versions of a cleared profile stay in that history table.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field

from django.db import transaction
from django.utils import timezone

from audit.services import record_event
from community.models import (
    AccountContact,
    BoardSeat,
    ContactMethod,
    ContactProfile,
    OfficeHolding,
    Ownership,
    Person,
    RetentionRun,
    RoleAssignment,
)
from community.rules import rule
from core.policy import SYSTEM, require
from core.scoping import system_access

RULE_KEY = "retention.former_owner_contact_days"


@dataclass
class Candidate:
    person: Person
    left_on: datetime.date
    profile: ContactProfile | None = None
    methods: list = field(default_factory=list)


def retention_days() -> int:
    return int(rule(RULE_KEY) or 0)


def _left_on(on: datetime.date) -> dict[int, datetime.date | None]:
    """person_id -> the day their last ownership or contact ended (None while
    one is still active)."""
    ended: dict[int, datetime.date | None] = {}

    def note(person_id, end):
        if end is None or end > on:
            ended[person_id] = None  # still active
        elif person_id not in ended or (ended[person_id] is not None and end > ended[person_id]):
            ended[person_id] = end

    for person_id, end in Ownership.objects.values_list("person_id", "end_date"):
        note(person_id, end)
    for person_id, end, closed in AccountContact.objects.values_list(
        "person_id", "end_date", "account__closed_on"
    ):
        ends = [d for d in (end, closed) if d is not None]
        note(person_id, min(ends) if ends else None)
    return ended


def candidates(on: datetime.date | None = None, days: int | None = None) -> list[Candidate]:
    """People whose contact details are due to be cleared (with something to clear)."""
    on = on or timezone.localdate()
    days = retention_days() if days is None else days
    if days <= 0:
        return []
    cutoff = on - datetime.timedelta(days=days)
    with system_access("retention: find former owners"):
        left = {pid: end for pid, end in _left_on(on).items() if end is not None and end <= cutoff}
        if not left:
            return []
        held = set(BoardSeat.objects.active(on).values_list("person_id", flat=True))
        held |= set(OfficeHolding.objects.active(on).values_list("person_id", flat=True))
        held |= set(RoleAssignment.objects.active(on).values_list("person_id", flat=True))
        ids = [pid for pid in left if pid not in held]
        profiles = {p.person_id: p for p in ContactProfile.objects.filter(person_id__in=ids)}
        methods: dict[int, list] = {}
        for m in ContactMethod.objects.filter(person_id__in=ids):
            methods.setdefault(m.person_id, []).append(m)
        out = []
        for person in Person.objects.filter(pk__in=ids).order_by("display_name", "pk"):
            profile = profiles.get(person.pk)
            if profile is not None and not _has_details(profile):
                profile = None
            if profile is None and not methods.get(person.pk):
                continue
            out.append(Candidate(person, left[person.pk], profile, methods.get(person.pk, [])))
    return out


def _has_details(profile: ContactProfile) -> bool:
    return bool(
        profile.phone
        or profile.mailing_address
        or profile.mail_street
        or profile.in_directory
        or profile.directory_show_email
        or profile.directory_show_phone
        or profile.sms_number
        or profile.sms_opt_in
    )


def run(*, actor, request=None, on: datetime.date | None = None) -> RetentionRun | None:
    """Clear contact details of everyone due. ``actor`` is SYSTEM for the
    daily task, or a board member pressing "Run now". Returns None when the
    rule is switched off (0 days)."""
    require(actor, "retention.manage")
    days = retention_days()
    if days <= 0:
        return None
    mode = "scheduled" if actor is SYSTEM else "manual"
    profiles_cleared = methods_removed = 0
    with transaction.atomic():
        due = candidates(on, days)
        with system_access("retention: clear contact details"):
            for c in due:
                if c.profile is not None:
                    profile = ContactProfile.objects.select_for_update().get(pk=c.profile.pk)
                    profile.phone = ""
                    profile.mailing_address = ""
                    # The structured postal address (D40).
                    profile.mail_to_home = None
                    profile.mail_street = profile.mail_unit = profile.mail_city = ""
                    profile.mail_state = profile.mail_zip = ""
                    profile.in_directory = False
                    profile.directory_show_email = False
                    profile.directory_show_phone = False
                    # Text-message number too (review R2-006, D27.5).
                    profile.sms_number = ""
                    profile.sms_verified_at = None
                    profile.sms_opt_in = False
                    profile.save()
                    profiles_cleared += 1
                if c.methods:
                    removed, _ = ContactMethod.objects.filter(
                        pk__in=[m.pk for m in c.methods]
                    ).delete()
                    methods_removed += removed
        result = RetentionRun.objects.create(
            ran_by=None if actor is SYSTEM else actor,
            days=days,
            people=len(due),
            profiles_cleared=profiles_cleared,
            methods_removed=methods_removed,
        )
        record_event(
            "retention.run",
            actor=None if actor is SYSTEM else actor,
            request=request,
            target=result,
            metadata={
                "days": days,
                "people": len(due),
                "profiles_cleared": profiles_cleared,
                "methods_removed": methods_removed,
                "mode": mode,
            },
        )
    return result
