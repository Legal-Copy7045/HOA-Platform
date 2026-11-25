"""Granting sysadmin needs two different board members; revoking needs one (D4)."""

from datetime import timedelta
from unittest import mock

import pytest
from django.core import mail
from django.core.exceptions import PermissionDenied
from django.utils import timezone

from audit.models import AuditEvent
from community import services
from community.models import Person, Role, RoleAssignment, SysadminNomination
from community.services import CommunityError
from community.tasks import notify_board
from community.tests.factories import LotFactory, PersonFactory, make_member
from core import policy

pytestmark = pytest.mark.django_db


@pytest.fixture
def board_a():
    return make_member(board=True)


@pytest.fixture
def board_b():
    return make_member(board=True)


def person_of(user):
    return Person.objects.unscoped("test").get(user=user)


def nominate(nominee_user, actor):
    with mock.patch("community.tasks.notify_board.defer"):
        return services.nominate_sysadmin(person=person_of(nominee_user), actor=actor)


def test_two_different_board_members_grant_sysadmin(
    board_a, board_b, django_capture_on_commit_callbacks
):
    nominee = make_member()
    with (
        mock.patch("community.tasks.notify_board.defer") as notify,
        django_capture_on_commit_callbacks(execute=True),
    ):
        nomination = services.nominate_sysadmin(person=person_of(nominee), actor=board_a)
        assert not policy.is_sysadmin(nominee)
        services.approve_sysadmin(nomination, actor=board_b)
    assert policy.is_sysadmin(nominee)
    assert [c.kwargs["kind"] for c in notify.call_args_list] == [
        "sysadmin_nominated",
        "sysadmin_granted",
    ]
    actions = set(AuditEvent.objects.values_list("action", flat=True))
    assert {"role.sysadmin_nominated", "role.sysadmin_approved", "role.granted"} <= actions


def test_nominator_cannot_approve(board_a):
    nomination = nominate(make_member(), board_a)
    with pytest.raises(CommunityError, match="different board member"):
        services.approve_sysadmin(nomination, actor=board_a)


def test_nominee_cannot_approve_their_own_nomination(board_a, board_b):
    nomination = nominate(board_b, board_a)
    with pytest.raises(CommunityError, match="own nomination"):
        services.approve_sysadmin(nomination, actor=board_b)


def test_nobody_nominates_themselves(board_a):
    with pytest.raises(CommunityError, match="yourself"):
        nominate(board_a, board_a)


@pytest.mark.parametrize("roles", [[Role.SYSADMIN], [Role.AUDITOR]])
def test_non_board_members_can_neither_nominate_nor_approve(board_a, roles):
    outsider = make_member(roles=roles)
    with pytest.raises(PermissionDenied):
        nominate(make_member(), outsider)
    nomination = nominate(make_member(), board_a)
    with pytest.raises(PermissionDenied):
        services.approve_sysadmin(nomination, actor=outsider)


def test_owner_cannot_nominate():
    with pytest.raises(PermissionDenied):
        nominate(make_member(), make_member(lots=[LotFactory()]))


def test_expired_nomination_cannot_be_approved(board_a, board_b):
    nomination = nominate(make_member(), board_a)
    SysadminNomination.objects.unscoped("test").filter(pk=nomination.pk).update(
        expires_at=timezone.now() - timedelta(minutes=1)
    )
    with pytest.raises(CommunityError, match="no longer open"):
        services.approve_sysadmin(nomination, actor=board_b)


def test_declined_nomination_is_closed(board_a, board_b):
    nomination = nominate(make_member(), board_a)
    services.decline_sysadmin(nomination, actor=board_b)
    with pytest.raises(CommunityError):
        services.approve_sysadmin(nomination, actor=board_b)


def test_no_duplicate_nominations(board_a):
    nominee = make_member()
    nominate(nominee, board_a)
    with pytest.raises(CommunityError, match="already an open"):
        nominate(nominee, board_a)
    with pytest.raises(CommunityError, match="already a sysadmin"):
        nominate(make_member(roles=[Role.SYSADMIN]), board_a)


def test_one_board_member_revokes_and_the_board_is_told(
    board_a, django_capture_on_commit_callbacks
):
    admin = make_member(roles=[Role.SYSADMIN])
    assignment = RoleAssignment.objects.unscoped("test").get(person__user=admin)
    with (
        mock.patch("community.tasks.notify_board.defer") as notify,
        django_capture_on_commit_callbacks(execute=True),
    ):
        services.end_role(assignment, actor=board_a)
    assert not policy.is_sysadmin(admin)
    notify.assert_called_once_with(kind="sysadmin_revoked")


def test_board_notice_goes_to_current_board_only_and_names_nobody(board_a, board_b):
    make_member(lots=[LotFactory()])  # an owner: not notified
    former = make_member(board=True)
    from community.models import BoardSeat, SeatEndReason

    services.end_board_seat(
        BoardSeat.objects.unscoped("t").get(person__user=former),
        reason=SeatEndReason.RESIGNED,
        actor=board_a,
    )
    mail.outbox = []
    assert notify_board("sysadmin_nominated") == 2
    assert sorted(m.to[0] for m in mail.outbox) == sorted([board_a.email, board_b.email])
    nominee_name = PersonFactory().display_name
    assert all(nominee_name not in m.body for m in mail.outbox)
