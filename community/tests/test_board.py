import pytest

from audit.models import AuditEvent
from community.models import BoardSeat, Office, OfficeHolding, SeatEndReason, SeatType, today
from community.services import (
    CommunityError,
    end_board_seat,
    record_board_seat,
    record_office,
)
from community.tests.factories import PersonFactory, UserFactory, days_ago

pytestmark = pytest.mark.django_db


@pytest.fixture
def board_user():
    user = UserFactory()
    PersonFactory(user=user)
    return user


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
    assert BoardSeat.objects.active().get() == seat
    assert seat.citation == "Bylaws §3.2"
    assert AuditEvent.objects.filter(action="seat.recorded", target_id=str(seat.pk)).exists()


def test_seat_continues_past_term_end_until_ended_but_is_flagged(board_user):
    seat = seat_for(PersonFactory(), board_user, term_end=days_ago(1))
    assert BoardSeat.objects.active().filter(pk=seat.pk).exists()
    assert list(BoardSeat.objects.past_term_end()) == [seat]


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
    assert not OfficeHolding.objects.active().exists()
    assert not BoardSeat.objects.active().exists()
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
        seat_for(me, board_user)
    other = UserFactory()
    PersonFactory(user=other)
    seat = seat_for(me, other)
    with pytest.raises(CommunityError):
        record_office(person=me, office=Office.PRESIDENT, start_date=today(), actor=board_user)
    end_board_seat(seat, reason=SeatEndReason.RESIGNED, actor=board_user)
