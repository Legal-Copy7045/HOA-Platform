"""Homeowner-view review (2026-11-25): a board member who owns a home gets a
separate Homeowner section; owners ask the board to change who is on their
account; ARC project types are configured by the board; and a POC command
lets the first officer test the owner side as themselves."""

from io import StringIO

import pytest
from django.core.exceptions import PermissionDenied
from django.core.management import CommandError, call_command
from django.urls import reverse

from arc import services as arc
from community import services
from community.models import Account, AccountChangeRequest, AccountContact, Person
from community.tests.factories import (
    LotFactory,
    UserFactory,
    days_ago,
    enroll_totp,
    make_member,
)
from core import my_items, policy
from core.policy import SYSTEM
from core.scoping import system_access

pytestmark = pytest.mark.django_db


def _person(user):
    with system_access("t"):
        return Person.objects.get(user=user)


def _account(lot):
    with system_access("t"):
        return Account.objects.get(lot=lot, closed_on__isnull=True)


@pytest.fixture
def w():
    lot, other = LotFactory(), LotFactory()
    board_owner = make_member(board=True, lots=[lot])
    return {
        "lot": lot,
        "other": other,
        "board_owner": board_owner,
        "board2": make_member(board=True),
        "owner": make_member(lots=[other]),
    }


# --- Separate homeowner and board sections ---------------------------------------------


def test_menu_has_dashboard_and_a_homeowner_section(client, w):
    client.force_login(w["board_owner"])
    page = client.get(reverse("home")).content.decode()
    assert ">Dashboard<" in page
    assert ">Homeowner<" in page and ">My home<" in page
    assert "/arc/?mine=1" in page  # my own ARC requests
    assert ">Architectural review<" in page  # the board's review list, separately


def test_board_owner_raises_their_own_arc_from_the_homeowner_side(client, w):
    arc.create_request(lot=w["other"], title="Neighbor shed", description="x", actor=w["owner"])
    client.force_login(w["board_owner"])
    mine = client.get(reverse("arc_list"), {"mine": "1"}).content
    assert b"Ask for approval of a project" in mine
    assert b"Neighbor shed" not in mine
    board = client.get(reverse("arc_list")).content
    assert b"Ask for approval of a project" not in board
    response = client.post(
        reverse("arc_list"),
        {
            "lot": w["lot"].pk,
            "title": "My fence",
            "description": "Rear, 4 ft",
            "project_type": "Fence",
            "planned_start": "2027-04-01",
        },
    )
    assert response.status_code == 302


def test_owner_only_lists_ignore_the_mine_switch(client, w):
    client.force_login(w["owner"])
    assert client.get(reverse("arc_list"), {"mine": "1"}).status_code == 200
    assert client.get(reverse("violations"), {"mine": "1"}).status_code == 200
    assert client.get(reverse("requests"), {"mine": "1"}).status_code == 200


def test_my_lot_moved_to_my_home(client, w):
    client.force_login(w["owner"])
    response = client.get("/my-lot/")
    assert response.status_code == 302 and response["Location"] == reverse("my_lot")
    assert reverse("my_lot") == "/my-home/"


# --- Owners ask the board to change who is on the account --------------------------------


def test_owner_asks_another_board_member_approves(client, w, django_capture_on_commit_callbacks):
    client.force_login(w["owner"])
    account = _account(w["other"])
    client.post(
        reverse("account_change_request", args=[account.pk]),
        {"action": "add", "name": "Teen Driver", "kind": "view_only"},
    )
    change = AccountChangeRequest.objects.for_user(w["owner"]).get()
    assert change.state == "pending"
    assert any("Lot" in i.title for i in my_items.items_for(w["board2"]) if i.area == "Approvals")
    client.force_login(w["board2"])
    client.post(reverse("account_change_decide", args=[change.pk, "approve"]))
    change.refresh_from_db()
    assert change.state == "approved"
    with system_access("t"):
        assert AccountContact.objects.filter(
            account=account, person__display_name="Teen Driver", kind="view_only"
        ).exists()


def test_owner_asks_to_remove_and_board_declines(w):
    account = _account(w["other"])
    person = services.record_person(display_name="Old tenant", actor=SYSTEM)
    contact = services.add_account_contact(account, person=person, kind="tenant", actor=SYSTEM)
    change = services.request_account_change(
        account, action="remove", contact=contact, actor=w["owner"]
    )
    services.decide_account_change(
        change, approve=False, note="Lease runs to June", actor=w["board2"]
    )
    change.refresh_from_db()
    assert change.state == "declined"
    contact.refresh_from_db()
    assert contact.end_date is None


def test_requester_on_the_board_cannot_approve_their_own_change(w):
    change = services.request_account_change(
        _account(w["lot"]), action="add", name="Spouse", kind="authorized", actor=w["board_owner"]
    )
    with pytest.raises(services.CommunityError):
        services.decide_account_change(change, approve=True, actor=w["board_owner"])
    services.decide_account_change(change, approve=True, actor=w["board2"])


def test_only_owners_of_that_lot_may_ask(w):
    with pytest.raises(PermissionDenied):
        services.request_account_change(
            _account(w["lot"]), action="add", name="X", kind="tenant", actor=w["owner"]
        )
    contact_user = UserFactory()
    person = services.record_person(display_name="Contact", actor=SYSTEM)
    with system_access("t"):
        Person.objects.filter(pk=person.pk).update(user=contact_user)
    person.refresh_from_db()
    enroll_totp(contact_user)
    services.add_account_contact(
        _account(w["other"]), person=person, kind="authorized", actor=SYSTEM
    )
    assert not policy.can(contact_user, "account.request_change")


def test_other_owners_cannot_see_or_decide_changes(client, w):
    change = services.request_account_change(
        _account(w["other"]), action="add", name="Guest", kind="tenant", actor=w["owner"]
    )
    intruder = make_member(lots=[LotFactory()])
    client.force_login(intruder)
    assert client.post(reverse("account_change_withdraw", args=[change.pk])).status_code == 404
    assert (
        client.post(reverse("account_change_decide", args=[change.pk, "approve"])).status_code
        == 403
    )
    assert client.get(reverse("my_lot")).content.count(b"Guest") == 0


def test_owner_withdraws_a_waiting_change(w):
    change = services.request_account_change(
        _account(w["other"]), action="add", name="Guest", kind="tenant", actor=w["owner"]
    )
    services.withdraw_account_change(change, actor=w["owner"])
    change.refresh_from_db()
    assert change.state == "withdrawn"
    with pytest.raises(services.CommunityError):
        services.decide_account_change(change, approve=True, actor=w["board2"])


# --- ARC project types -------------------------------------------------------------------------


def test_board_configures_types_and_owners_must_choose_one(client, w):
    client.force_login(w["board2"])
    client.post(
        reverse("arc_project_types"),
        {"name": "Gazebo", "guidance": "Plot plan", "sort_order": "10", "active": "on"},
    )
    with pytest.raises(arc.ArcError):
        arc.create_request(
            lot=w["other"], title="x", description="y", project_type="Moat", actor=w["owner"]
        )
    req = arc.create_request(
        lot=w["other"], title="x", description="y", project_type="Gazebo", actor=w["owner"]
    )
    assert req.project_type == "Gazebo"
    client.force_login(w["owner"])
    page = client.get(reverse("arc_list"), {"mine": "1"}).content
    assert b"Kind of project" in page and b"Gazebo" in page and b"Fence" in page
    assert client.get(reverse("arc_project_types")).status_code == 403


def test_new_request_form_needs_a_kind_and_a_start_and_shows_the_address(client, w):
    from arc.views import NewRequestForm

    form = NewRequestForm({"lot": w["other"].pk, "title": "t", "description": "d"}, user=w["owner"])
    assert not form.is_valid()
    assert {"project_type", "planned_start"} <= set(form.errors)
    assert form.only_lot == w["other"]  # one home: no dropdown
    client.force_login(w["owner"])
    page = client.get(reverse("arc_list"), {"mine": "1"}).content.decode()
    assert w["other"].street_address in page
    assert 'type="hidden" name="lot"' in page


def test_retired_types_are_not_offered(w):
    t = arc.save_project_type(name="Pool", actor=w["board2"])
    arc.save_project_type(name="Pool", active=False, project_type=t, actor=w["board2"])
    assert "Pool" not in {t.name for t in arc.active_project_types()}


# --- Testing the owner side on the POC -------------------------------------------------------


def test_demo_make_me_owner(settings, w):
    from community.models import Ownership

    settings.SEED_DEMO_ALLOWED = True
    demo_lot = LotFactory()
    demo_owner = services.record_person(display_name="Pat Example (demo)", actor=SYSTEM)
    services.record_ownership(
        person=demo_owner, lot=demo_lot, start_date=days_ago(100), actor=SYSTEM
    )
    officer = services.record_person(
        display_name="Officer", email="officer@example.com", actor=SYSTEM
    )
    out = StringIO()
    call_command("demo_make_me_owner", email="officer@example.com", lot=demo_lot.number, stdout=out)
    with system_access("t"):
        assert Ownership.objects.active().filter(person=officer, lot=demo_lot).exists()
    with pytest.raises(CommandError, match="not demo people"):
        call_command("demo_make_me_owner", email="officer@example.com", lot=w["lot"].number)
    settings.SEED_DEMO_ALLOWED = False
    with pytest.raises(CommandError, match="demo deployment"):
        call_command("demo_make_me_owner", email="officer@example.com", lot=demo_lot.number)


def test_an_invalid_arc_form_keeps_what_was_typed(client, w):
    client.force_login(w["owner"])
    response = client.post(
        reverse("arc_list"),
        {"lot": w["other"].pk, "title": "Long typed title", "description": "A long description"},
    )
    assert response.status_code == 200
    page = response.content.decode()
    assert "A long description" in page and "Long typed title" in page
    assert "This field is required" in page  # the missing kind and start are marked


def test_badges_use_the_records_own_words(client, w):
    from core.templatetags.portal import status

    assert "Awaiting neighbor acknowledgements" in status("neighbors")
    assert "no objection" in status("approved", "Acknowledged, no objection")
