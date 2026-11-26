"""Milestone 3 phase 1: accounts, the people on them, contact details and
former owners' read-only history."""

import pytest
from django.core import mail
from django.core.exceptions import PermissionDenied
from django.urls import reverse

from community import services
from community.models import Account, AccountContact, ContactMethod, Ownership, Person
from community.tests.factories import LotFactory, UserFactory, days_ago, enroll_totp, make_member
from core import policy
from core.policy import SYSTEM
from core.scoping import system_access

pytestmark = pytest.mark.django_db


def _person(user):
    with system_access("test"):
        return Person.objects.get(user=user)


def _account(lot):
    with system_access("test"):
        return Account.objects.get(lot=lot, closed_on__isnull=True)


def _contact_user(account, kind):
    user = UserFactory()
    person = services.record_person(display_name=f"{kind} person", actor=SYSTEM)
    with system_access("test"):
        Person.objects.filter(pk=person.pk).update(user=user)
    person.refresh_from_db()
    enroll_totp(user)
    services.add_account_contact(account, person=person, kind=kind, actor=SYSTEM)
    return user


@pytest.fixture
def w():
    lot, other = LotFactory(), LotFactory()
    owner = make_member(lots=[lot])
    return {
        "lot": lot,
        "other": other,
        "owner": owner,
        "other_owner": make_member(lots=[other]),
        "board": make_member(board=True),
        "account": _account(lot),
    }


# --- Accounts -----------------------------------------------------------------------------


def test_recording_an_owner_opens_a_numbered_account(w):
    account = w["account"]
    assert account.is_active
    assert account.number.startswith(f"{w['lot'].number:03d}-")


def test_a_second_owner_joins_the_same_account(w):
    spouse = make_member(lots=[w["lot"]])
    with system_access("test"):
        assert Ownership.objects.get(person__user=spouse).account_id == w["account"].pk
        assert Account.objects.filter(lot=w["lot"]).count() == 1


def test_a_sale_makes_the_account_dormant_and_opens_a_new_one(w):
    buyer = services.record_person(display_name="Buyer", actor=SYSTEM)
    services.transfer_lot(lot=w["lot"], new_owners=[buyer], transfer_date=days_ago(1), actor=SYSTEM)
    with system_access("test"):
        old = Account.objects.get(pk=w["account"].pk)
        new = Account.objects.get(lot=w["lot"], closed_on__isnull=True)
    assert not old.is_active and old.closed_on == days_ago(1)
    assert new.pk != old.pk


def test_account_number_is_board_set_and_unique(w):
    services.set_account_number(w["account"], "A-100", actor=w["board"])
    with pytest.raises(services.CommunityError):
        services.set_account_number(_account(w["other"]), "A-100", actor=w["board"])
    with pytest.raises(PermissionDenied):
        services.set_account_number(w["account"], "A-101", actor=w["owner"])


# --- Contacts by kind --------------------------------------------------------------------------


def test_authorized_contact_sees_the_account_but_cannot_vote_or_apply(client, w):
    user = _contact_user(w["account"], "authorized")
    assert policy.can(user, "ledger.view_own")
    assert not policy.can(user, "vote.cast")
    assert not policy.can(user, "arc.submit")
    client.force_login(user)
    assert client.get(reverse("my_account")).status_code == 200
    assert client.get(reverse("my_lot")).status_code == 200


def test_view_only_contact_sees_but_cannot_act(w):
    user = _contact_user(w["account"], "view_only")
    assert policy.can(user, "ledger.view_own")
    assert policy.can(user, "announcement.view")
    assert not policy.can(user, "vote.cast")
    assert not policy.can(user, "arc.submit")


def test_tenant_sees_community_pages_but_no_balance(client, w):
    user = _contact_user(w["account"], "tenant")
    assert policy.can(user, "announcement.view")
    assert not policy.can(user, "ledger.view_own")
    assert not policy.can(user, "vote.view")
    client.force_login(user)
    assert client.get(reverse("my_account")).status_code == 403
    assert client.get(reverse("announcements")).status_code == 200


def test_contact_sees_only_their_own_account(client, w):
    user = _contact_user(w["account"], "authorized")
    client.force_login(user)
    assert client.get(reverse("my_statement_pdf", args=[w["other"].pk])).status_code == 404
    assert client.get(reverse("lot_detail", args=[w["other"].pk])).status_code in (403, 404)
    other_account = _account(w["other"])
    assert not Account.objects.for_user(user).filter(pk=other_account.pk).exists()


def test_ending_a_contact_removes_access(w):
    user = _contact_user(w["account"], "authorized")
    with system_access("test"):
        contact = AccountContact.objects.get(person__user=user)
    services.end_account_contact(contact, actor=w["board"])
    # A contact's period is [start, end): ended today means no access today.
    assert not policy.can(user, "ledger.view_own")


def test_board_adds_and_ends_contacts_from_the_lot_page(client, w):
    client.force_login(w["board"])
    page = client.get(reverse("lot_detail", args=[w["lot"].pk]))
    assert b"Add someone to this account" in page.content
    response = client.post(
        reverse("lot_account_contact_add", args=[w["account"].pk]),
        {"kind": "view_only", "name": "Adult child"},
    )
    assert response.status_code == 302
    with system_access("test"):
        contact = AccountContact.objects.get(person__display_name="Adult child")
    client.post(reverse("lot_account_contact_end", args=[contact.pk]))
    contact.refresh_from_db()
    assert contact.end_date is not None


def test_owners_cannot_add_contacts(client, w):
    client.force_login(w["owner"])
    response = client.post(
        reverse("lot_account_contact_add", args=[w["account"].pk]),
        {"kind": "authorized", "name": "My friend"},
    )
    assert response.status_code in (403, 404)


def test_no_contacts_on_a_dormant_account(w):
    buyer = services.record_person(display_name="Buyer", actor=SYSTEM)
    services.transfer_lot(lot=w["lot"], new_owners=[buyer], transfer_date=days_ago(1), actor=SYSTEM)
    with system_access("test"):
        old = Account.objects.get(pk=w["account"].pk)
    person = services.record_person(display_name="Late", actor=SYSTEM)
    with pytest.raises(services.CommunityError):
        services.add_account_contact(old, person=person, kind="tenant", actor=SYSTEM)


def test_my_account_lists_people_and_upcoming_charges(client, w):
    services.set_rule(
        key="dues.monthly_cents", value="4500", citation="Declaration §6.1", actor=SYSTEM
    )
    _contact_user(w["account"], "view_only")
    client.force_login(w["owner"])
    page = client.get(reverse("my_account")).content
    assert b"On this account" in page
    assert b"view_only person" in page
    assert b"Upcoming charges" in page and b"$45.00" in page


# --- Former owners -------------------------------------------------------------------------------


@pytest.fixture
def sold(w):
    from ledger import services as ledger

    ledger.post_charge(
        lot=w["lot"],
        kind="other_charge",
        amount_cents=1200,
        description="Seller-era charge",
        posted_on=days_ago(5),
        actor=w["board"],
    )
    buyer = make_member()
    services.transfer_lot(
        lot=w["lot"], new_owners=[_person(buyer)], transfer_date=days_ago(0), actor=SYSTEM
    )
    return {**w, "buyer": buyer}


def test_former_owner_gets_read_only_history(client, sold):
    seller = sold["owner"]
    groups = policy.roles_for(seller).groups
    assert groups == {policy.FORMER_OWNER}
    client.force_login(seller)
    page = client.get(reverse("my_history"))
    assert page.status_code == 200
    assert b"Seller-era charge" in page.content
    for name in ("my_account", "my_lot", "announcements", "documents", "directory", "votes"):
        assert client.get(reverse(name)).status_code == 403, name


def test_former_owner_cannot_change_anything(client, sold):
    seller = sold["owner"]
    with pytest.raises(PermissionDenied):
        services.add_contact_method(_person(seller), kind="phone", value="555-0101", actor=seller)
    with pytest.raises(PermissionDenied):
        services.update_my_profile(user=seller, phone="555")


def test_former_owner_does_not_see_charges_after_the_sale(client, sold):
    from ledger import services as ledger

    ledger.post_charge(
        lot=sold["lot"],
        kind="other_charge",
        amount_cents=900,
        description="Buyer-era charge",
        actor=sold["board"],
    )
    client.force_login(sold["owner"])
    page = client.get(reverse("my_history")).content
    assert b"Buyer-era charge" not in page


def test_owners_cannot_open_my_history(client, w):
    client.force_login(w["other_owner"])
    assert client.get(reverse("my_history")).status_code == 403


# --- Contact methods and notices ------------------------------------------------------------------


def test_owner_adds_and_removes_own_contact_methods(client, w):
    # Extra notice addresses only once the owner chose email notices (D44).
    services.record_notice_choice(user=w["owner"], consent=True)
    client.force_login(w["owner"])
    client.post(
        reverse("contact_method_add"),
        {"kind": "email", "value": "second@example.test", "notices": "on"},
    )
    method = ContactMethod.objects.for_user(w["owner"]).get()
    assert method.notices
    assert b"second@example.test" in client.get(reverse("my_profile")).content
    client.post(reverse("contact_method_remove", args=[method.pk]))
    with system_access("test"):
        assert not ContactMethod.objects.exists()


def test_owner_cannot_remove_someone_elses_contact_method(client, w):
    method = services.add_contact_method(
        _person(w["other_owner"]), kind="phone", value="555-0199", actor=SYSTEM
    )
    client.force_login(w["owner"])
    assert client.post(reverse("contact_method_remove", args=[method.pk])).status_code == 404
    with system_access("test"):
        assert ContactMethod.objects.filter(pk=method.pk).exists()


def test_invalid_extra_email_is_refused(w):
    with pytest.raises(services.CommunityError):
        services.add_contact_method(
            _person(w["owner"]), kind="email", value="not-an-email", actor=w["owner"]
        )


def test_meeting_notice_goes_to_consented_extra_emails(w):
    import datetime

    from django.utils import timezone

    from community.models import ContactProfile
    from governance import services as gov

    person = _person(w["owner"])
    with system_access("test"):
        ContactProfile.objects.update_or_create(
            person=person, defaults={"electronic_notice_consent": True}
        )
    services.add_contact_method(
        person, kind="email", value="extra@example.test", notices=True, actor=SYSTEM
    )
    services.add_contact_method(
        person, kind="email", value="quiet@example.test", notices=False, actor=SYSTEM
    )
    meeting = gov.create_meeting(
        title="Annual meeting",
        kind="annual",
        starts_at=timezone.now() + datetime.timedelta(days=30),
        actor=w["board"],
    )
    gov.send_notice(meeting, actor=w["board"])
    sent_to = {a for m in mail.outbox for a in m.to}
    assert "extra@example.test" in sent_to
    assert "quiet@example.test" not in sent_to


def test_co_owner_with_an_earlier_deed_date_joins_the_open_account(w):
    """Recording a joint owner dated before the account opened (e.g. a late
    entry or a register import) must not try to open a second account."""
    spouse = services.record_person(display_name="Spouse", actor=SYSTEM)
    services.record_ownership(person=spouse, lot=w["lot"], start_date=days_ago(900), actor=SYSTEM)
    with system_access("test"):
        accounts = list(Account.objects.filter(lot=w["lot"]))
    assert len(accounts) == 1 and accounts[0].opened_on == days_ago(900)
