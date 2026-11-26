"""UX review (2026-11): the owner profile page is grouped and plain, and the
board's approvals page says exactly whose sign-in a reset removes."""

import pytest
from django.urls import reverse

from accounts import mfa_reset
from accounts.models import VerificationMethod
from community import services
from community.models import ContactProfile
from community.tests.factories import LotFactory, PersonFactory, days_ago, make_member
from core.people import display_name

pytestmark = pytest.mark.django_db


# --- Profile ------------------------------------------------------------------------


def test_profile_is_grouped_with_legends(client):
    client.force_login(make_member(lots=[LotFactory()]))
    page = client.get(reverse("my_profile")).content.decode()
    # D40: contact details live in "How we reach you"; the choices below.
    assert "<h2>How we reach you</h2>" in page
    for legend in ("Owner directory", "Email updates"):
        assert f"<legend>{legend}</legend>" in page, legend
    # The "show my email/phone" choices sit inside the directory group.
    directory = page[page.index("<legend>Owner directory</legend>") :]
    directory = directory[: directory.index("<legend>Email updates</legend>")]
    assert 'class="nested"' in directory
    assert 'name="directory_show_email"' in directory and 'name="directory_show_phone"' in directory
    assert "...and show" not in page


def test_profile_explains_consent_email_and_export(client):
    client.force_login(make_member(lots=[LotFactory()]))
    page = client.get(reverse("my_profile")).content.decode()
    assert "instead of mailing paper copies" in page and "change your choice at any time" in page
    assert "To change your sign-in email, ask the board." in page
    assert "JSON" not in page
    assert "for your records" in page


def test_profile_still_saves_every_choice(client):
    user = make_member(lots=[LotFactory()])
    client.force_login(user)
    client.post(
        reverse("my_profile"),
        {"section": "phone_add", "phone_type": "home", "number": "215-555-0199"},
    )
    client.post(reverse("my_profile"), {"section": "notices", "choice": "email"})
    client.post(
        reverse("my_profile"),
        {
            "section": "prefs",
            "in_directory": "on",
            "directory_show_phone": "on",
            "weekly_digest": "weekly",
        },
    )
    profile = ContactProfile.objects.unscoped("t").get(person__user=user)
    assert profile.phone == "215-555-0199"
    assert profile.electronic_notice_consent and profile.in_directory
    assert profile.directory_show_phone and not profile.directory_show_email
    assert profile.weekly_digest


# --- Approvals ----------------------------------------------------------------------


def test_reset_approval_names_the_person_email_lot_and_starter(client):
    lot = LotFactory()
    owner = make_member(lots=[lot], email="reset-me@example.com")
    starter, approver = make_member(board=True), make_member(board=True)
    mfa_reset.start_reset(
        subject=owner, actor=starter, verification_method=VerificationMethod.IN_PERSON
    )
    client.force_login(approver)
    page = client.get(reverse("approvals")).content.decode()
    assert display_name(owner) in page
    assert "reset-me@example.com" in page
    assert f"Lot {lot.number}" in page
    assert f"Started by {display_name(starter)}" in page
    assert "Account #" not in page


def test_reset_for_someone_without_a_lot_says_so(client):
    board_member = make_member(board=True)
    starter, approver = make_member(board=True), make_member(board=True)
    mfa_reset.start_reset(
        subject=board_member, actor=starter, verification_method=VerificationMethod.IN_PERSON
    )
    client.force_login(approver)
    assert "no current lot" in client.get(reverse("approvals")).content.decode()


def test_declining_a_seat_nomination_asks_first(client, settings):
    settings.BOARD_SEAT_SECOND_APPROVAL = True
    first, second = make_member(board=True), make_member(board=True)
    person = PersonFactory(display_name="Nominee Example")
    status, nomination = services.propose_board_seat(
        person=person, seat_type="owner_elected", term_start=days_ago(1), actor=first
    )
    assert status == "proposed"
    client.force_login(second)
    page = client.get(reverse("approvals")).content.decode()
    decline = reverse("seat_nomination_decide", args=[nomination.pk, "decline"])
    form = page[page.index(f'action="{decline}"') :][:300]
    assert 'data-confirm="Decline this board seat for Nominee Example?"' in form
    client.force_login(first)
    page = client.get(reverse("approvals")).content.decode()
    form = page[page.index(f'action="{decline}"') :][:300]
    assert 'data-confirm="Withdraw this board seat proposal?"' in form
