"""Retention of former owners' contact details (Milestone 4)."""

import pytest
from django.core.exceptions import PermissionDenied
from django.urls import reverse

from audit.models import AuditEvent
from community import retention, services
from community.models import (
    Account,
    ContactMethod,
    ContactProfile,
    Ownership,
    RetentionRun,
    Role,
)
from community.tests.factories import LotFactory, PersonFactory, days_ago, make_member
from core.append_only import AppendOnlyError
from core.policy import SYSTEM
from core.scoping import system_access

pytestmark = pytest.mark.django_db


def _with_details(person, phone="555-0101"):
    with system_access("test"):
        ContactProfile.objects.create(
            person=person,
            phone=phone,
            mailing_address="1 Elsewhere Road",
            in_directory=True,
            directory_show_phone=True,
        )
    services.add_contact_method(person, kind="email", value="old@example.com", actor=SYSTEM)


def _former_owner(left_days_ago: int, lot=None):
    person = PersonFactory()
    lot = lot or LotFactory()
    services.record_ownership(person=person, lot=lot, start_date=days_ago(3000), actor=SYSTEM)
    ownership = Ownership.objects.unscoped("t").get(person=person, lot=lot)
    services.end_ownership(ownership, end_date=days_ago(left_days_ago), actor=SYSTEM)
    _with_details(person)
    return person


def _profile(person):
    return ContactProfile.objects.unscoped("t").get(person=person)


def test_rule_is_registered_with_a_default_and_guidance():
    from community.pa_law import guide
    from community.rules import REGISTRY

    assert REGISTRY["retention.former_owner_contact_days"].default == "730"
    assert guide("retention.former_owner_contact_days") is not None
    assert retention.retention_days() == 730


def test_clears_contact_details_of_long_gone_owners_only():
    gone = _former_owner(800)
    recent = _former_owner(100)
    current = PersonFactory()
    services.record_ownership(
        person=current, lot=LotFactory(), start_date=days_ago(900), actor=SYSTEM
    )
    _with_details(current)
    due = retention.candidates()
    assert [c.person for c in due] == [gone]

    result = retention.run(actor=SYSTEM)
    assert (result.people, result.profiles_cleared, result.methods_removed) == (1, 1, 1)
    profile = _profile(gone)
    assert profile.phone == "" and profile.mailing_address == ""
    assert not (profile.in_directory or profile.directory_show_phone)
    assert not ContactMethod.objects.unscoped("t").filter(person=gone).exists()
    # Name and login identity kept; the others untouched.
    gone.refresh_from_db()
    assert gone.display_name
    assert _profile(recent).phone == "555-0101"
    assert _profile(current).phone == "555-0101"
    event = AuditEvent.objects.get(action="retention.run")
    assert event.metadata["people"] == 1 and event.metadata["mode"] == "scheduled"
    assert "555" not in str(event.metadata) and gone.display_name not in str(event.metadata)
    # A second run finds nothing more to clear.
    assert retention.run(actor=SYSTEM).people == 0


def test_person_who_still_owns_another_lot_is_untouched():
    person = _former_owner(800)
    services.record_ownership(
        person=person, lot=LotFactory(), start_date=days_ago(10), actor=SYSTEM
    )
    assert retention.candidates() == []
    retention.run(actor=SYSTEM)
    assert _profile(person).phone == "555-0101"


def test_board_members_and_role_holders_are_untouched():
    board_user = make_member(board=True)
    manager_user = make_member(roles=[Role.MANAGER])
    for user in (board_user, manager_user):
        person = user.person
        lot = LotFactory()
        services.record_ownership(person=person, lot=lot, start_date=days_ago(3000), actor=SYSTEM)
        ownership = Ownership.objects.unscoped("t").get(person=person, lot=lot)
        services.end_ownership(ownership, end_date=days_ago(900), actor=SYSTEM)
        _with_details(person)
    assert retention.candidates() == []


def test_active_contact_keeps_details_and_dormant_contact_is_cleared():
    lot = LotFactory()
    owner = PersonFactory()
    services.record_ownership(person=owner, lot=lot, start_date=days_ago(3000), actor=SYSTEM)
    account = Account.objects.unscoped("t").get(lot=lot, closed_on__isnull=True)
    tenant = PersonFactory()
    services.add_account_contact(
        account, person=tenant, kind="tenant", start_date=days_ago(2000), actor=SYSTEM
    )
    _with_details(tenant)
    assert retention.candidates() == []
    # The lot was sold long ago: the account went dormant, so the contact ended.
    Account.objects.unscoped("t").filter(pk=account.pk).update(closed_on=days_ago(800))
    Ownership.objects.unscoped("t").filter(person=owner).update(end_date=days_ago(800))
    due = {c.person.pk for c in retention.candidates()}
    assert tenant.pk in due


def test_zero_days_switches_it_off():
    _former_owner(800)
    services.set_rule(
        key="retention.former_owner_contact_days",
        value="0",
        citation="Board resolution 2027-11",
        actor=SYSTEM,
    )
    assert retention.candidates() == []
    assert retention.run(actor=SYSTEM) is None


def test_never_touches_records():
    """Only the profile and contact methods change: no ledger, ARC, vote or
    audit row is edited or removed."""
    from ledger import services as ledger
    from ledger.models import LedgerEntry

    lot = LotFactory()
    person = _former_owner(800, lot=lot)
    ledger.post_charge(
        lot=lot, kind="other_charge", amount_cents=500, description="Old fine", actor=SYSTEM
    )
    before_ledger = list(LedgerEntry.objects.unscoped("t").values_list("pk", "amount_cents"))
    before_audit = AuditEvent.objects.count()
    retention.run(actor=SYSTEM)
    assert list(LedgerEntry.objects.unscoped("t").values_list("pk", "amount_cents")) == (
        before_ledger
    )
    assert AuditEvent.objects.count() == before_audit + 1
    assert Ownership.objects.unscoped("t").filter(person=person).exists()


def test_runs_are_append_only():
    result = retention.run(actor=SYSTEM)
    with pytest.raises(AppendOnlyError):
        result.save()
    with pytest.raises(AppendOnlyError):
        RetentionRun.objects.all().delete()


def test_board_page_preview_and_run(client):
    board = make_member(board=True)
    owner = make_member(lots=[LotFactory()])
    gone = _former_owner(800)
    client.force_login(owner)
    assert client.get(reverse("retention")).status_code == 403
    assert client.post(reverse("retention_run")).status_code == 403
    assert _profile(gone).phone == "555-0101"
    client.force_login(board)
    page = client.get(reverse("retention")).content.decode()
    assert gone.display_name in page and "Due at the next run (1)" in page
    assert _profile(gone).phone == "555-0101"  # the preview changes nothing
    client.post(reverse("retention_run"))
    assert _profile(gone).phone == ""
    run = RetentionRun.objects.get()
    assert run.ran_by == board
    assert AuditEvent.objects.get(action="retention.run").metadata["mode"] == "manual"


def test_sysadmin_and_manager_cannot_run_it():
    for user in (make_member(roles=[Role.SYSADMIN]), make_member(roles=[Role.MANAGER])):
        with pytest.raises(PermissionDenied):
            retention.run(actor=user)


def test_daily_task_runs_as_the_system():
    from community.tasks import contact_retention

    _former_owner(800)
    assert contact_retention(timestamp=0) == 1
