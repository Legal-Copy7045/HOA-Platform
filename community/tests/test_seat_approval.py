"""Decision D21 / security review A3: in production a new board seat needs a
second board member; on the POC one member may record it."""

import pytest
from django.urls import reverse

from community import services
from community.models import BoardSeat
from community.tests.factories import PersonFactory, days_ago, make_member
from core import my_items, policy
from core.scoping import system_access

pytestmark = pytest.mark.django_db


@pytest.fixture
def board(settings):
    settings.BOARD_SEAT_SECOND_APPROVAL = True
    return make_member(board=True), make_member(board=True)


def _propose(actor, person=None):
    return services.propose_board_seat(
        person=person or PersonFactory(),
        seat_type="owner_elected",
        term_start=days_ago(1),
        actor=actor,
    )


def test_with_two_board_members_a_seat_is_only_proposed(board):
    first, second = board
    status, nomination = _propose(first)
    assert status == "proposed"
    with system_access("t"):
        assert not BoardSeat.objects.filter(person=nomination.person).exists()
    with pytest.raises(services.CommunityError):
        services.record_board_seat(
            person=nomination.person,
            seat_type="owner_elected",
            term_start=days_ago(1),
            actor=first,
        )
    assert any(i.nav == "approvals" for i in my_items.items_for(second))
    assert not any(i.title == "Proposed board seat" for i in my_items.items_for(first))


def test_a_different_board_member_approves(board):
    first, second = board
    _, nomination = _propose(first)
    with pytest.raises(services.CommunityError):
        services.decide_seat_nomination(nomination, approve=True, actor=first)
    services.decide_seat_nomination(nomination, approve=True, actor=second)
    with system_access("t"):
        seat = BoardSeat.objects.get(person=nomination.person)
    assert seat.recorded_by_id == second.pk


def test_the_proposer_can_withdraw(board):
    first, _second = board
    _, nomination = _propose(first)
    services.decide_seat_nomination(nomination, approve=False, actor=first)
    nomination.refresh_from_db()
    assert nomination.outcome == "declined" and nomination.seat_id is None


def test_first_seats_are_exempt_while_fewer_than_two_members(settings):
    settings.BOARD_SEAT_SECOND_APPROVAL = True
    only = make_member(board=True)
    status, _ = _propose(only)
    assert status == "recorded"


def test_poc_setting_records_directly(settings):
    settings.BOARD_SEAT_SECOND_APPROVAL = False
    first, _ = make_member(board=True), make_member(board=True)
    status, _ = _propose(first)
    assert status == "recorded"


def test_approvals_page_shows_the_proposal(client, board):
    first, second = board
    _, nomination = _propose(first)
    client.force_login(second)
    assert nomination.person.display_name.encode() in client.get(reverse("approvals")).content
    client.post(reverse("seat_nomination_decide", args=[nomination.pk, "approve"]))
    nomination.refresh_from_db()
    assert nomination.outcome == "approved"
    assert policy.can(second, "seat.manage")
