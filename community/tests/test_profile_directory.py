import pytest
from django.urls import reverse

from audit.models import AuditEvent
from community import services
from community.models import Ownership
from community.tests.factories import LotFactory, make_member
from core.scoping import system_access

pytestmark = pytest.mark.django_db


def test_owner_edits_own_profile_and_consent_is_timestamped(client):
    owner = make_member(lots=[LotFactory()])
    client.force_login(owner)
    client.post(
        reverse("my_profile"),
        {"section": "phone_add", "phone_type": "mobile", "number": "555-555-0100"},
    )
    client.post(reverse("my_profile"), {"section": "notices", "choice": "email"})
    from community.models import ContactProfile

    with system_access("t"):
        p = ContactProfile.objects.get(person__user=owner)
    assert p.phone == "555-555-0100" and p.electronic_notice_consent and p.consent_changed_at
    event = AuditEvent.objects.get(action="profile.updated")
    assert event.metadata["consent_changed"] is True
    assert "555" not in str(event.metadata)


def test_directory_shows_only_opted_in_current_owners_and_chosen_fields(client):
    listed = make_member(lots=[LotFactory(number=10)], email="listed@example.com")
    hidden = make_member(lots=[LotFactory(number=11)], email="hidden@example.com")
    phone_only = make_member(lots=[LotFactory(number=12)], email="phoneonly@example.com")
    services.update_my_profile(
        user=listed, phone="555-0101", in_directory=True, directory_show_email=True
    )
    services.update_my_profile(
        user=hidden, phone="555-0102", in_directory=False, directory_show_email=True
    )
    services.update_my_profile(
        user=phone_only, phone="555-0103", in_directory=True, directory_show_phone=True
    )
    entries = {e["lot"]: e for e in services.directory_entries()}
    assert set(entries) == {10, 12}
    listed_email = listed.person.email  # the contact email on the person record
    assert entries[10]["email"] == listed_email and entries[10]["phone"] == ""
    assert entries[12]["email"] == "" and entries[12]["phone"] == "555-0103"
    # A former owner drops out even if they had opted in.
    with system_access("t"):
        ownership = Ownership.objects.get(person__user=listed)
    from core.policy import SYSTEM

    services.end_ownership(ownership, actor=SYSTEM)
    assert 10 not in {e["lot"] for e in services.directory_entries()}


def test_directory_is_for_members(client):
    from community.models import Role

    client.force_login(make_member(roles=[Role.SYSADMIN]))
    assert client.get(reverse("directory")).status_code == 403
