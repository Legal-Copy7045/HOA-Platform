"""Who sees and does what (CLAUDE.md "Authorization (critical)").

Cross-owner isolation, immediate revocation on ownership end, role scopes,
and the permission check on every community service.
"""

import pytest
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import PermissionDenied

from community import services
from community.models import (
    AppointedBy,
    BoardSeat,
    Lot,
    Office,
    OfficeHolding,
    Ownership,
    Person,
    Role,
    RoleAssignment,
    SeatEndReason,
    SeatType,
)
from community.services import CommunityError
from community.tests.factories import LotFactory, PersonFactory, days_ago, make_member
from core import policy
from core.policy import SYSTEM
from core.scoping import system_access

pytestmark = pytest.mark.django_db

SCOPED_MODELS = [Lot, Person, Ownership, BoardSeat, OfficeHolding, RoleAssignment]


@pytest.fixture
def world():
    """Two owners on two lots, a co-owner, a former owner, and role holders."""
    lot_a, lot_b, lot_c = LotFactory(), LotFactory(), LotFactory()
    owner_a = make_member(lots=[lot_a])
    co_owner_a = make_member(lots=[lot_a])
    owner_b = make_member(lots=[lot_b])
    former = make_member(lots=[lot_a])
    with system_access("test setup"):
        services.end_ownership(
            Ownership.objects.get(person__user=former), end_date=days_ago(1), actor=SYSTEM
        )
    return {
        "lot_a": lot_a,
        "lot_b": lot_b,
        "lot_c": lot_c,
        "owner_a": owner_a,
        "co_owner_a": co_owner_a,
        "owner_b": owner_b,
        "former": former,
        "board": make_member(board=True),
        "auditor": make_member(roles=[Role.AUDITOR]),
        "arc": make_member(roles=[Role.ARC_REVIEWER]),
        "sysadmin": make_member(roles=[Role.SYSADMIN]),
    }


def visible(model, user):
    return set(model.objects.for_user(user).values_list("pk", flat=True))


# --- Cross-owner isolation ----------------------------------------------------


def test_owner_sees_only_their_own_lot(world):
    assert visible(Lot, world["owner_a"]) == {world["lot_a"].pk}
    assert visible(Lot, world["owner_b"]) == {world["lot_b"].pk}


def test_owner_a_cannot_reach_owner_b_records_by_primary_key(world):
    owner_a = world["owner_a"]
    b_person = Person.objects.unscoped("test").get(user=world["owner_b"])
    b_ownership = Ownership.objects.unscoped("test").get(person=b_person)
    for model, pk in [(Lot, world["lot_b"].pk), (Person, b_person.pk), (Ownership, b_ownership.pk)]:
        with pytest.raises(model.DoesNotExist):
            model.objects.for_user(owner_a).get(pk=pk)


def test_owner_sees_current_co_owners_but_not_past_owners(world):
    people = Person.objects.for_user(world["owner_a"])
    users = set(people.values_list("user", flat=True))
    assert users == {world["owner_a"].pk, world["co_owner_a"].pk}
    ownerships = Ownership.objects.for_user(world["owner_a"])
    assert all(o.end_date is None for o in ownerships)
    assert ownerships.count() == 2


def test_owner_sees_no_board_or_role_records_of_others(world):
    for model in (BoardSeat, OfficeHolding, RoleAssignment):
        assert visible(model, world["owner_a"]) == set()


# --- Revocation ----------------------------------------------------------------


def test_ending_ownership_removes_access_on_the_next_query(world):
    owner_a = world["owner_a"]
    assert visible(Lot, owner_a) == {world["lot_a"].pk}
    ownership = Ownership.objects.unscoped("test").get(person__user=owner_a)
    services.end_ownership(ownership, actor=world["board"])
    own_person = {ownership.person_id}
    for model in SCOPED_MODELS:
        # A former owner keeps read-only sight of their own person record
        # (Milestone 3) and nothing about the lot.
        expected = own_person if model is Person else set()
        assert visible(model, owner_a) == expected, model.__name__
    assert not policy.can(owner_a, "lot.view_own")
    assert policy.can(owner_a, "history.view_own")


def test_former_owner_sees_nothing_of_the_lot(world):
    former = Person.objects.unscoped("test").get(user=world["former"])
    for model in SCOPED_MODELS:
        expected = {former.pk} if model is Person else set()
        assert visible(model, world["former"]) == expected, model.__name__


def test_transfer_moves_access_to_buyer_immediately(world):
    buyer = make_member()
    buyer_person = Person.objects.unscoped("test").get(user=buyer)
    services.transfer_lot(world["lot_b"], new_owners=[buyer_person], actor=world["board"])
    assert visible(Lot, buyer) == {world["lot_b"].pk}
    assert visible(Lot, world["owner_b"]) == set()


# --- Role scopes ---------------------------------------------------------------


@pytest.mark.parametrize("who", ["board", "auditor"])
def test_board_and_auditor_see_every_lot(world, who):
    assert visible(Lot, world[who]) == set(
        Lot.objects.unscoped("test").values_list("pk", flat=True)
    )


@pytest.mark.parametrize("who", ["arc", "sysadmin"])
def test_arc_and_sysadmin_get_no_owner_data_from_their_role(world, who):
    assert visible(Lot, world[who]) == set()
    assert visible(Ownership, world[who]) == set()
    me = Person.objects.unscoped("test").get(user=world[who])
    assert visible(Person, world[who]) == {me.pk}  # only themselves


@pytest.mark.parametrize("user", [None, AnonymousUser()])
def test_anonymous_sees_nothing(user):
    LotFactory()
    assert visible(Lot, user) == set()


def test_deactivated_login_sees_nothing(world):
    owner_a = world["owner_a"]
    owner_a.is_active = False
    owner_a.save()
    assert visible(Lot, owner_a) == set()


def test_user_without_person_sees_nothing():
    from community.tests.factories import UserFactory

    LotFactory()
    assert visible(Lot, UserFactory()) == set()


# --- Permission checks in the services -----------------------------------------


def _attempts(world):
    target = PersonFactory()
    lot = world["lot_c"]
    with system_access("test setup"):
        own_b = Ownership.objects.get(person__user=world["owner_b"])
    seat = services.record_board_seat(
        person=PersonFactory(),
        seat_type=SeatType.OWNER_ELECTED,
        term_start=days_ago(5),
        actor=SYSTEM,
    )
    return {
        "record_person": lambda a: services.record_person(display_name="X", actor=a),
        "record_ownership": lambda a: services.record_ownership(
            person=target, lot=lot, start_date=days_ago(1), actor=a
        ),
        "end_ownership": lambda a: services.end_ownership(own_b, actor=a),
        "transfer_lot": lambda a: services.transfer_lot(lot, new_owners=[target], actor=a),
        "record_board_seat": lambda a: services.propose_board_seat(
            person=target, seat_type=SeatType.OWNER_ELECTED, term_start=days_ago(1), actor=a
        ),
        "end_board_seat": lambda a: services.end_board_seat(
            seat, reason=SeatEndReason.REMOVED, actor=a
        ),
        "record_office": lambda a: services.record_office(
            person=seat.person, office=Office.TREASURER, start_date=days_ago(1), actor=a
        ),
        "grant_arc": lambda a: services.grant_role(
            person=target, role=Role.ARC_REVIEWER, appointed_by=AppointedBy.BOARD, actor=a
        ),
        "grant_auditor": lambda a: services.grant_role(person=target, role=Role.AUDITOR, actor=a),
    }


SERVICE_NAMES = [
    "record_person",
    "record_ownership",
    "end_ownership",
    "transfer_lot",
    "record_board_seat",
    "end_board_seat",
    "record_office",
    "grant_arc",
    "grant_auditor",
]


@pytest.mark.parametrize("who", ["owner_a", "auditor", "arc", "sysadmin", "former"])
@pytest.mark.parametrize("service", SERVICE_NAMES)
def test_non_board_members_cannot_change_the_register(world, who, service):
    with pytest.raises(PermissionDenied):
        _attempts(world)[service](world[who])


@pytest.mark.parametrize("service", SERVICE_NAMES)
def test_board_member_can_change_the_register(world, service):
    _attempts(world)[service](world["board"])


def test_services_refuse_a_missing_actor(world):
    with pytest.raises(CommunityError):
        services.record_person(display_name="X", actor=None)


def test_sysadmin_cannot_be_granted_directly(world):
    with pytest.raises(CommunityError, match="second board member"):
        services.grant_role(person=PersonFactory(), role=Role.SYSADMIN, actor=world["board"])


def test_one_board_member_can_revoke_sysadmin(world):
    with system_access("test"):
        assignment = RoleAssignment.objects.get(person__user=world["sysadmin"])
    services.end_role(assignment, actor=world["board"])
    assert not policy.is_sysadmin(world["sysadmin"])
    assert not policy.can(world["sysadmin"], "system.view_status")


def test_arc_appointment_must_record_the_appointer(world):
    with pytest.raises(CommunityError, match="appointed"):
        services.grant_role(person=PersonFactory(), role=Role.ARC_REVIEWER, actor=world["board"])


def test_no_self_granted_roles(world):
    me = Person.objects.unscoped("test").get(user=world["board"])
    with pytest.raises(CommunityError):
        services.grant_role(person=me, role=Role.AUDITOR, actor=world["board"])


# --- Policy table and coverage ---------------------------------------------------


MEMBER_READS = {
    "announcement.view",
    "calendar.view",
    "document.view_owner",
    "meeting.view",
    "resolution.view",
    "vote.view",
    "reservation.view",
}


@pytest.mark.parametrize(
    ("who", "allowed"),
    [
        (
            "owner_a",
            MEMBER_READS
            | {
                "lot.view_own",
                "directory.view",
                "records_request.submit",
                "vote.cast",
                "arc.submit",
                "ledger.view_own",
                "resale.request",
                "request.submit",
                "request.view_own",
                "account.request_change",
                "budget.view_owner",
                "sms.opt_in",
                "reservation.request",
                "reserves.view_summary",
            },
        ),
        (
            "auditor",
            MEMBER_READS
            | {
                "lot.view_all",
                "audit.view_sysadmin_activity",
                "document.view_board",
                "arc.view_all",
                "violation.view_all",
                "finance.view",
                "reports.view",
                "committee.view",
                "committee.view_all",
                "roster.view",
                "budget.view_owner",
                "reserves.view",
                "reserves.view_summary",
            },
        ),
        ("arc", MEMBER_READS | {"arc.view_all", "arc.intake", "arc.decide"}),
        ("sysadmin", {"system.view_status", "invite.resend", "invite.revoke", "branding.manage"}),
        ("former", {"history.view_own", "request.view_own"}),
    ],
)
def test_permission_matrix(world, who, allowed):
    granted = {action for action in policy.PERMISSIONS if policy.can(world[who], action)}
    assert granted == allowed


def test_manager_does_data_entry_but_never_approves_or_votes():
    manager = make_member(roles=[Role.MANAGER])
    for action in (
        "invoice.enter",
        "workorder.manage",
        "violation.manage",
        "arc.intake",
        "document.manage",
        "lot.view_all",
        "ledger.manage",
    ):
        assert policy.can(manager, action), action
    for action in (
        "invoice.approve",
        "vote.cast",
        "arc.decide",
        "violation.decide",
        "budget.manage",
        "person.manage",
        "role.sysadmin.approve",
        "reserves.manage",
    ):
        assert not policy.can(manager, action), action


def test_board_member_can_do_every_board_action(world):
    board_actions = {a for a, groups in policy.PERMISSIONS.items() if policy.BOARD in groups}
    assert all(policy.can(world["board"], a) for a in board_actions)


def test_unknown_action_is_an_error(world):
    with pytest.raises(KeyError):
        policy.can(world["board"], "lot.delete_everything")


def test_coverage_warns_when_fewer_than_two_people_can_act(world):
    thin = policy.thinly_covered_actions()
    # One board member, one auditor, one sysadmin in this world.
    assert thin["ownership.manage"] == 1
    assert thin["system.view_status"] == 1
    assert "lot.view_own" not in thin  # owner-only actions are exempt
    make_member(board=True, mfa=False)  # without MFA they do not count (D4)
    assert policy.thinly_covered_actions()["ownership.manage"] == 1
    make_member(board=True)
    assert "ownership.manage" not in policy.thinly_covered_actions()


def test_whole_board_is_told_when_a_seat_is_recorded(django_capture_on_commit_callbacks):
    """Review A3 mitigation: a single member could otherwise quietly create a
    second board login for themselves."""
    from unittest import mock

    from community.models import SeatType
    from community.tests.factories import PersonFactory, days_ago

    board = make_member(board=True)
    with (
        mock.patch("community.tasks.notify_board.defer") as defer,
        django_capture_on_commit_callbacks(execute=True),
    ):
        services.record_board_seat(
            person=PersonFactory(),
            seat_type=SeatType.OWNER_ELECTED,
            term_start=days_ago(1),
            actor=board,
        )
    defer.assert_called_once_with(kind="board_seat_recorded")
