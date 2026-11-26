"""Board and committees (streamlining, owner decision 2026-11-26): every
resident sees who serves, by name and office, and the committees with their
members. Nothing else: no contact details, lots, seat history, committee
topics or files, and nothing a directory opt-out keeps private."""

from unittest import mock

import pytest
from django.urls import reverse

from audit.models import AuditEvent
from community import services
from community.models import Account, ContactProfile, Office, Person, Role
from community.tests.factories import LotFactory, UserFactory, days_ago, enroll_totp, make_member
from core import features, policy
from core.policy import SYSTEM
from core.scoping import system_access
from governance import services as governance

pytestmark = pytest.mark.django_db


def _person(user):
    return Person.objects.unscoped("t").get(user=user)


def _contact(lot, kind):
    user = UserFactory()
    person = services.record_person(display_name=f"{kind} contact", actor=SYSTEM)
    with system_access("test"):
        Person.objects.filter(pk=person.pk).update(user=user)
        account = Account.objects.get(lot=lot, closed_on__isnull=True)
    person.refresh_from_db()
    enroll_totp(user)
    services.add_account_contact(account, person=person, kind=kind, actor=SYSTEM)
    return user


@pytest.fixture
def w():
    lot, lot_b = LotFactory(street_address="1 Alpha Way"), LotFactory()
    president = make_member(board=True, lots=[lot_b], email="president@example.com")
    director = make_member(board=True)
    services.record_office(
        person=_person(president), office=Office.PRESIDENT, start_date=days_ago(10), actor=SYSTEM
    )
    services.record_office(
        person=_person(president), office=Office.TREASURER, start_date=days_ago(10), actor=SYSTEM
    )
    with system_access("t"):
        Person.objects.filter(user=president).update(display_name="Pat President")
        Person.objects.filter(user=director).update(display_name="Dana Director")
        # The president keeps their phone out of the directory (opt-out).
        ContactProfile.objects.update_or_create(
            person=_person(president),
            defaults={"phone": "555-0199", "in_directory": False, "mailing_address": "PO Box 7"},
        )
    with mock.patch("community.tasks.notify_board.defer"):
        committee = governance.create_committee(
            name="Landscaping", purpose="Keeps the entrance planted.", actor=director
        )
        governance.appoint_committee_member(
            committee, person=_person(president), role="chair", actor=director
        )
        governance.start_topic(
            committee, title="Secret mulch plan", body="Board note", actor=president
        )
    return {
        "lot": lot,
        "owner": make_member(lots=[lot]),
        "president": president,
        "director": director,
        "committee": committee,
    }


def test_every_resident_and_member_sees_it(client, w):
    from community.models import Role

    people = {
        "owner": w["owner"],
        "authorized": _contact(w["lot"], "authorized"),
        "view_only": _contact(w["lot"], "view_only"),
        "tenant": _contact(w["lot"], "tenant"),
        "board": w["director"],
        "manager": make_member(roles=[Role.MANAGER]),
        "auditor": make_member(roles=[Role.AUDITOR]),
    }
    for who, user in people.items():
        assert policy.can(user, "board_committees.view"), who
        client.force_login(user)
        response = client.get(reverse("board_and_committees"))
        assert response.status_code == 200, who
        assert b"<h1>Board and committees</h1>" in response.content


def test_sysadmin_only_and_former_owner_cannot_open_it(client, w):
    sysadmin = make_member(roles=[Role.SYSADMIN])
    client.force_login(sysadmin)
    assert client.get(reverse("board_and_committees")).status_code == 403
    buyer = services.record_person(display_name="Buyer", actor=SYSTEM)
    seller = w["owner"]
    services.transfer_lot(w["lot"], new_owners=[buyer], transfer_date=days_ago(1), actor=SYSTEM)
    assert policy.can(seller, "history.view_own")
    client.force_login(seller)
    assert client.get(reverse("board_and_committees")).status_code == 403


def test_names_and_offices_only(client, w):
    client.force_login(w["owner"])
    page = client.get(reverse("board_and_committees")).content.decode()
    main = page.split("<main", 1)[-1]
    assert "Pat President" in main and "President, Treasurer" in main
    assert "Dana Director" in main and "Director" in main
    # Officers first.
    assert main.index("Pat President") < main.index("Dana Director")
    assert "Landscaping" in main and "Keeps the entrance planted." in main and "Chair" in main
    # Never contact details, lots, addresses or committee topics, whatever
    # the directory settings are.
    for private in (
        "president@example.com",
        "555-0199",
        "PO Box 7",
        "1 Alpha Way",
        "Secret mulch plan",
        "@",
    ):
        assert private not in main, private
    # Owner A is not on the committee: no link into its space.
    assert reverse("committee_detail", args=[w["committee"].pk]) not in main


def test_directory_opt_in_details_still_stay_off_this_page(client, w):
    with system_access("t"):
        ContactProfile.objects.filter(person=_person(w["president"])).update(
            in_directory=True, directory_show_email=True, directory_show_phone=True
        )
    client.force_login(w["owner"])
    main = client.get(reverse("board_and_committees")).content.decode().split("<main", 1)[-1]
    assert "555-0199" not in main and "president@example.com" not in main


def test_ended_seats_and_offices_leave_the_page(client, w):
    from community.models import BoardSeat

    with system_access("t"):
        seat = BoardSeat.objects.get(person__user=w["director"])
    services.end_board_seat(seat, reason="resigned", ended_on=days_ago(0), actor=w["director"])
    client.force_login(w["owner"])
    main = client.get(reverse("board_and_committees")).content.decode().split("<main", 1)[-1]
    assert "Dana Director" not in main and "Pat President" in main


def test_committee_member_links_to_their_space(client, w):
    client.force_login(w["president"])
    page = client.get(reverse("board_and_committees")).content.decode()
    assert reverse("committee_detail", args=[w["committee"].pk]) in page


def test_committees_section_follows_the_feature(client, w):
    features.set_feature("committees", False, actor=w["director"])
    client.force_login(w["owner"])
    main = client.get(reverse("board_and_committees")).content.decode().split("<main", 1)[-1]
    assert "Landscaping" not in main and "Pat President" in main


def test_service_refuses_without_the_permission(w):
    from django.core.exceptions import PermissionDenied

    sysadmin = make_member(roles=[Role.SYSADMIN])
    with pytest.raises(PermissionDenied):
        services.board_members_public(actor=sysadmin)
    with pytest.raises(PermissionDenied):
        governance.committees_public(actor=sysadmin)


def test_coverage_and_audit_unchanged(client, w):
    """A resident read: not a board action for the two-person coverage check,
    and opening the page writes no audit event (like the other community pages)."""
    assert "board_committees.view" not in policy.thinly_covered_actions()
    assert policy.PERMISSIONS["board_committees.view"] == policy.COMMUNITY
    client.force_login(w["owner"])
    before = AuditEvent.objects.count()
    client.get(reverse("board_and_committees"))
    assert AuditEvent.objects.count() == before
