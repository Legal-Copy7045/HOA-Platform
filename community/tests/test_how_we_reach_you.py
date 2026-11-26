"""D44: My profile's "How we reach you" card - official notices choice,
emails, phones (with opt-in text alerts), and one structured mailing address
for post, read first by every letter and paper notice."""

import json

import pytest
from django.urls import reverse

from audit.models import AuditEvent
from community import services
from community.models import ContactMethod, ContactProfile, Person
from community.tests.factories import LotFactory, make_member
from core import my_items
from core.policy import SYSTEM
from core.scoping import system_access

pytestmark = pytest.mark.django_db


def _person(user):
    with system_access("t"):
        return Person.objects.get(user=user)


def _profile(user):
    return ContactProfile.objects.unscoped("t").filter(person__user=user).first()


def _methods(user, kind=None):
    qs = ContactMethod.objects.unscoped("t").filter(person__user=user)
    return list(qs.filter(kind=kind) if kind else qs)


@pytest.fixture
def owner(client):
    user = make_member(lots=[LotFactory(street_address="12 Oak Creek Lane")])
    client.force_login(user)
    return user


# --- The card ----------------------------------------------------------------------------


def test_card_has_four_sections_with_notices_first(client, owner):
    page = client.get(reverse("my_profile")).content.decode()
    assert "<h2>How we reach you</h2>" in page
    order = [
        page.index("<h3>Official notices</h3>"),
        page.index("<h3>Email</h3>"),
        page.index("<h3>Phone</h3>"),
        page.index("<h3>Mailing address for post</h3>"),
    ]
    assert order == sorted(order)
    assert "You sign in with this" in page and "ask the board" in page
    assert 'type="email"' in page and 'type="tel"' in page
    assert '<option value="mobile">Mobile</option>' in page
    assert "More ways to reach you" not in page and 'name="kind"' not in page


def test_unanswered_notices_choice_is_not_preticked(client, owner):
    page = client.get(reverse("my_profile")).content.decode()
    notices = page[page.index('id="notices"') : page.index('id="email"')]
    assert 'value="email"' in notices and "checked" not in notices
    assert "You haven't chosen yet" in notices


def test_notices_section_is_for_owners_on_the_deed(client):
    client.force_login(make_member(board=True))
    page = client.get(reverse("my_profile")).content.decode()
    assert "Official notices go to the owners on the deed." in page
    client.post(reverse("my_profile"), {"section": "notices", "choice": "email"})
    profile = ContactProfile.objects.unscoped("t").first()
    assert profile is None or profile.consent_changed_at is None


# --- Email -------------------------------------------------------------------------------


def test_extra_email_added_with_label_and_removed(client, owner):
    response = client.post(
        reverse("my_profile"),
        {"section": "email_add", "email": "second@example.test", "label": "Work"},
    )
    assert response["Location"] == reverse("my_profile") + "#email"
    (method,) = _methods(owner, "email")
    assert method.value == "second@example.test" and method.label == "Work"
    assert not method.notices
    event = AuditEvent.objects.get(action="contact.method_added")
    assert event.metadata == {"kind": "email"}
    client.post(reverse("contact_method_remove", args=[method.pk]))
    assert _methods(owner) == []
    # The removed detail stays in the change history.
    assert ContactMethod.history.filter(id=method.pk, history_type="-").exists()
    assert "example.test" not in json.dumps(
        list(AuditEvent.objects.values_list("metadata", flat=True))
    )


def test_bad_email_re_renders_bound_with_errors(client, owner):
    response = client.post(
        reverse("my_profile"), {"section": "email_add", "email": "not-an-email", "label": "x"}
    )
    assert response.status_code == 200
    page = response.content.decode()
    assert "form-errors" in page and 'value="not-an-email"' in page
    assert _methods(owner) == []


def test_notices_to_an_extra_email_only_after_choosing_email(client, owner):
    client.post(reverse("my_profile"), {"section": "email_add", "email": "two@example.test"})
    (method,) = _methods(owner, "email")
    page = client.get(reverse("my_profile")).content.decode()
    assert "Choose email above to send them here too" in page
    assert reverse("contact_method_notices", args=[method.pk]) not in page
    client.post(reverse("contact_method_notices", args=[method.pk]), {"notices": "on"})
    method.refresh_from_db()
    assert not method.notices
    services.record_notice_choice(user=owner, consent=True)
    page = client.get(reverse("my_profile")).content.decode()
    assert reverse("contact_method_notices", args=[method.pk]) in page
    client.post(reverse("contact_method_notices", args=[method.pk]), {"notices": "on"})
    method.refresh_from_db()
    assert method.notices
    assert AuditEvent.objects.filter(
        action="contact.method_changed", metadata={"kind": "email", "notices": True}
    ).exists()
    emails, _paper = services.owner_notice_recipients()
    assert "two@example.test" in emails


def test_another_persons_email_cannot_be_changed_or_removed(client, owner):
    other = make_member(lots=[LotFactory()])
    services.record_notice_choice(user=other, consent=True)
    method = services.add_contact_method(
        _person(other), kind="email", value="theirs@example.test", actor=SYSTEM
    )
    assert (
        client.post(
            reverse("contact_method_notices", args=[method.pk]), {"notices": "on"}
        ).status_code
        == 404
    )
    assert client.post(reverse("contact_method_remove", args=[method.pk])).status_code == 404
    method.refresh_from_db()
    assert not method.notices


# --- Phone -------------------------------------------------------------------------------


def test_phone_added_with_a_type_and_mirrored_as_the_main_phone(client, owner):
    client.post(
        reverse("my_profile"),
        {"section": "phone_add", "phone_type": "mobile", "number": "215-555-0123"},
    )
    (phone,) = _methods(owner, "phone")
    assert phone.phone_type == "mobile" and phone.value == "215-555-0123"
    assert _profile(owner).phone == "215-555-0123"  # the directory's phone
    page = client.get(reverse("my_profile")).content.decode()
    assert "215-555-0123" in page and "Mobile" in page
    client.post(reverse("contact_method_remove", args=[phone.pk]))
    assert _profile(owner).phone == ""


@pytest.mark.parametrize(
    "data",
    [
        {"phone_type": "mobile", "number": "12345"},
        {"phone_type": "pager", "number": "215-555-0123"},
        {"phone_type": "home", "number": "<script>"},
    ],
)
def test_bad_phone_re_renders_with_errors(client, owner, data):
    response = client.post(reverse("my_profile"), {"section": "phone_add", **data})
    assert response.status_code == 200
    assert "form-errors" in response.content.decode() or "alert danger" in (
        response.content.decode()
    )
    assert _methods(owner, "phone") == []


def test_adding_a_phone_never_opts_in_to_texts(client, owner):
    client.post(
        reverse("my_profile"),
        {"section": "phone_add", "phone_type": "mobile", "number": "215-555-0123"},
    )
    profile = _profile(owner)
    assert not profile.sms_opt_in and not profile.sms_verified_at and profile.sms_number == ""


def test_address_kind_is_no_longer_added_to_the_list(owner):
    with pytest.raises(services.CommunityError):
        services.add_contact_method(_person(owner), kind="address", value="1 Rd", actor=owner)


# --- Mailing address for post ---------------------------------------------------------


def test_default_is_the_homes_address(client, owner):
    page = client.get(reverse("my_profile")).content.decode()
    mailing = page[page.index('id="mailing"') :]
    assert "Same as my home&#x27;s address" in mailing or "Same as my home's address" in mailing
    assert 'value="home"' in mailing and "checked" in mailing.split('value="other"')[0]
    assert services.postal_address(_person(owner)) == ("12 Oak Creek Lane", "home")


def test_a_different_address_is_validated_and_saved(client, owner):
    bad = client.post(
        reverse("my_profile"),
        {"section": "mailing", "where": "other", "street": "", "city": "Media", "zip_code": "123"},
    )
    assert bad.status_code == 200
    page = bad.content.decode()
    assert "form-errors" in page and "Enter the street address." in page
    assert "5-digit ZIP" in page and 'value="Media"' in page
    ok = client.post(
        reverse("my_profile"),
        {
            "section": "mailing",
            "where": "other",
            "street": "9 Elm St",
            "unit": "Apt 2",
            "city": "Media",
            "state": "PA",
            "zip_code": "190631234",
        },
    )
    assert ok["Location"] == reverse("my_profile") + "#mailing"
    profile = _profile(owner)
    assert profile.mail_to_home is False and profile.mail_zip == "19063-1234"
    assert services.postal_address(_person(owner)) == (
        "9 Elm St, Apt 2\nMedia, PA 19063-1234",
        "structured",
    )
    event = AuditEvent.objects.filter(action="profile.updated").last()
    assert event.metadata == {"mailing": "other", "via": "profile"}
    assert "Elm" not in json.dumps(event.metadata)
    assert profile.history.count() >= 2


def test_old_free_text_address_is_kept_shown_and_flagged(client, owner):
    with system_access("t"):
        ContactProfile.objects.create(person=_person(owner), mailing_address="PO Box 7\nMedia PA")
    assert services.postal_address(_person(owner)) == ("PO Box 7\nMedia PA", "old")
    page = client.get(reverse("my_profile")).content.decode()
    assert "Address on file (please check)" in page and "PO Box 7" in page
    titles = [i.title for i in my_items.items_for(owner)]
    assert "Check your mailing address" in titles
    client.post(reverse("my_profile"), {"section": "mailing", "where": "home", "state": "PA"})
    assert "Check your mailing address" not in [i.title for i in my_items.items_for(owner)]
    profile = _profile(owner)
    assert profile.mailing_address == "PO Box 7\nMedia PA"  # never lost
    assert services.postal_address(_person(owner)) == ("12 Oak Creek Lane", "home")


def test_old_address_list_entry_is_flagged_too(client, owner):
    with system_access("t"):
        ContactMethod.objects.create(person=_person(owner), kind="address", value="1 Far Rd")
    assert services.postal_address(_person(owner)) == ("1 Far Rd", "old")
    assert "Check your mailing address" in [i.title for i in my_items.items_for(owner)]
    page = client.get(reverse("my_profile")).content.decode()
    assert "1 Far Rd" in page


def test_letters_read_structured_then_old_then_home():
    from ledger.letters import _recipients

    lot = LotFactory(street_address="5 Mill Rd")
    first, second = make_member(lots=[lot]), make_member(lots=[lot])
    assert _recipients(lot)[2] == "5 Mill Rd"
    with system_access("t"):
        ContactProfile.objects.create(person=_person(second), mailing_address="Old text address")
    assert _recipients(lot)[2] == "Old text address"
    services.set_mailing_address(
        user=first, to_home=False, street="1 New St", city="Media", state="PA", zip_code="19063"
    )
    assert _recipients(lot)[2] == "1 New St\nMedia, PA 19063"


def test_board_sees_the_structured_address_on_the_person_page(client, owner):
    services.set_mailing_address(
        user=owner, to_home=False, street="9 Elm St", city="Media", state="PA", zip_code="19063"
    )
    client.force_login(make_member(board=True))
    page = client.get(reverse("person_detail", args=[_person(owner).pk])).content.decode()
    assert "How we reach them" in page and "9 Elm St" in page and "Media, PA 19063" in page
    assert "not answered" in page


def test_owner_cannot_open_the_board_person_page(client, owner):
    other = make_member(lots=[LotFactory()])
    assert client.get(reverse("person_detail", args=[_person(other).pk])).status_code in (403, 404)


def test_former_owner_cannot_change_contact_details(client):
    lot = LotFactory()
    seller = make_member(lots=[lot])
    buyer = services.record_person(display_name="Buyer", actor=SYSTEM)
    services.transfer_lot(lot=lot, new_owners=[buyer], transfer_date=lot_today(), actor=SYSTEM)
    from django.core.exceptions import PermissionDenied

    with pytest.raises(PermissionDenied):
        services.set_mailing_address(user=seller, to_home=True)
    with pytest.raises(PermissionDenied):
        services.record_notice_choice(user=seller, consent=True)


def lot_today():
    from community.tests.factories import days_ago

    return days_ago(0)


# --- Routine updates wording --------------------------------------------------------


def test_routine_updates_choice_reads_plainly(client, owner):
    page = client.get(reverse("my_profile")).content.decode()
    assert "Routine updates (replies, discussions, requests to you)" in page
    assert "One email per update" in page
    assert "only sent in weeks when there is something new" in page
    assert "Official notices always come straight away." in page
    client.post(reverse("my_profile"), {"section": "prefs", "weekly_digest": "weekly"})
    assert _profile(owner).weekly_digest
    client.post(reverse("my_profile"), {"section": "prefs", "weekly_digest": "now"})
    assert not _profile(owner).weekly_digest
