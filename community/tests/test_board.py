import pytest
from django.core.exceptions import PermissionDenied

from audit.models import AuditEvent
from community.models import BoardSeat, Office, OfficeHolding, SeatEndReason, SeatType, today
from community.services import (
    CommunityError,
    end_board_seat,
    record_board_seat,
    record_office,
)
from community.tests.factories import PersonFactory, days_ago, make_member

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _poc_seat_rules(settings):
    # These tests exercise offices and terms; the second approval for new
    # seats (production) is tested in test_seat_approval.py.
    settings.BOARD_SEAT_SECOND_APPROVAL = False


@pytest.fixture
def board_user():
    return make_member(board=True)


def seat_for(person, actor, **kwargs):
    kwargs.setdefault("seat_type", SeatType.OWNER_ELECTED)
    kwargs.setdefault("term_start", days_ago(100))
    return record_board_seat(person=person, actor=actor, **kwargs)


def test_seat_records_type_term_and_citation(board_user):
    person = PersonFactory()
    seat = seat_for(
        person,
        board_user,
        seat_type=SeatType.DECLARANT_APPOINTED,
        term_end=days_ago(-265),
        citation="Bylaws §3.2",
    )
    assert BoardSeat.objects.unscoped("test").active().get(person=person) == seat
    assert seat.citation == "Bylaws §3.2"
    assert AuditEvent.objects.filter(action="seat.recorded", target_id=str(seat.pk)).exists()


def test_seat_continues_past_term_end_until_ended_but_is_flagged(board_user):
    seat = seat_for(PersonFactory(), board_user, term_end=days_ago(1))
    assert BoardSeat.objects.unscoped("test").active().filter(pk=seat.pk).exists()
    assert list(BoardSeat.objects.unscoped("test").past_term_end()) == [seat]


def test_one_seat_at_a_time_per_person(board_user):
    person = PersonFactory()
    seat_for(person, board_user)
    with pytest.raises(CommunityError):
        seat_for(person, board_user, term_start=days_ago(10))


def test_officer_must_hold_a_seat(board_user):
    with pytest.raises(CommunityError, match="board seat"):
        record_office(
            person=PersonFactory(), office=Office.PRESIDENT, start_date=today(), actor=board_user
        )


def test_officer_seat_rule_is_configurable(board_user, settings):
    settings.OFFICERS_REQUIRE_BOARD_SEAT = False
    holding = record_office(
        person=PersonFactory(), office=Office.TREASURER, start_date=today(), actor=board_user
    )
    assert holding.pk


def test_one_holder_per_office(board_user):
    first, second = PersonFactory(), PersonFactory()
    seat_for(first, board_user)
    seat_for(second, board_user)
    record_office(person=first, office=Office.SECRETARY, start_date=days_ago(5), actor=board_user)
    with pytest.raises(CommunityError, match="already holds"):
        record_office(person=second, office=Office.SECRETARY, start_date=today(), actor=board_user)


def test_ending_a_seat_ends_that_persons_offices_the_same_day(board_user):
    person = PersonFactory()
    seat = seat_for(person, board_user)
    holding = record_office(
        person=person, office=Office.PRESIDENT, start_date=days_ago(50), actor=board_user
    )

    end_board_seat(seat, reason=SeatEndReason.RESIGNED, actor=board_user)

    holding.refresh_from_db()
    assert holding.end_date == today()
    assert not OfficeHolding.objects.unscoped("test").filter(person=person).active().exists()
    assert not BoardSeat.objects.unscoped("test").filter(person=person).active().exists()
    assert AuditEvent.objects.filter(action="office.ended").count() == 1


def test_end_reason_required_and_seat_ends_once(board_user):
    seat = seat_for(PersonFactory(), board_user)
    with pytest.raises(CommunityError):
        end_board_seat(seat, reason="bored", actor=board_user)
    end_board_seat(seat, reason=SeatEndReason.TERM_EXPIRED, actor=board_user)
    with pytest.raises(CommunityError):
        end_board_seat(seat, reason=SeatEndReason.TERM_EXPIRED, actor=board_user)


def test_no_self_grants_but_resigning_is_allowed(board_user):
    me = board_user.person
    with pytest.raises(CommunityError):
        record_office(person=me, office=Office.PRESIDENT, start_date=today(), actor=board_user)
    my_seat = BoardSeat.objects.unscoped("test").get(person=me)
    end_board_seat(my_seat, reason=SeatEndReason.RESIGNED, actor=board_user)
    assert not BoardSeat.objects.unscoped("test").filter(person=me).active().exists()


def test_former_board_member_cannot_reseat_themselves(board_user):
    me = board_user.person
    end_board_seat(
        BoardSeat.objects.unscoped("test").get(person=me),
        reason=SeatEndReason.RESIGNED,
        actor=board_user,
    )
    with pytest.raises(PermissionDenied):
        seat_for(me, board_user, term_start=today())
