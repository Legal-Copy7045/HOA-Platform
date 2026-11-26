"""demo_attach_requester (decision D53): after a reseed, the requester gets
a board seat and a demo home in the new data; POC only, only when asked,
audited with ids only."""

from __future__ import annotations

import io

import pytest
from django.core.management import CommandError, call_command

from audit.models import AuditEvent
from community.models import BoardSeat, Ownership, Role
from community.tests.factories import make_member
from core import policy
from core.models import HostTask
from core.scoping import system_access

pytestmark = pytest.mark.django_db


@pytest.fixture
def poc(settings):
    settings.SEED_DEMO_ALLOWED = True
    settings.DEPLOYMENT_STAGE = "poc"
    return settings


def _seed(dataset):
    call_command("seed_demo", dataset=dataset, stdout=io.StringIO())


def _task(user, attach=True):
    return HostTask.objects.create(
        kind="reseed_demo",
        dataset="fresh_start",
        attach_requester=attach,
        requested_by=user,
        state="running",
    )


@pytest.mark.parametrize("dataset", ["fresh_start", "oak_creek"])
def test_requester_becomes_board_member_and_joint_owner_of_a_demo_home(poc, dataset):
    _seed(dataset)
    admin = make_member(roles=[Role.SYSADMIN], email="real.admin@portal-test.invalid")
    task = _task(admin)
    out = io.StringIO()
    call_command("demo_attach_requester", task=task.pk, stdout=out)
    assert "Requester attached: board seat, joint owner of demo home 1." in out.getvalue()
    with system_access("t"):
        seat = BoardSeat.objects.active().get(person__user=admin)
        assert seat.seat_type == "declarant_appointed" and seat.ended_on is None
        mine = Ownership.objects.active().get(person__user=admin)
        assert mine.lot.number == 1 and mine.ownership_type == "joint"
        assert Ownership.objects.active().filter(lot=mine.lot).count() == 2
    roles = policy.roles_for(admin)
    assert roles.has(policy.BOARD) and roles.has(policy.OWNER) and roles.has(policy.SYSADMIN)
    event = AuditEvent.objects.get(action="system.demo_requester_attached")
    assert set(event.metadata) == {"task_id", "person_id", "seat_id", "ownership_id", "lot_id"}
    assert event.metadata["task_id"] == task.pk and event.metadata["seat_id"] == seat.pk
    # Running it again changes nothing (already a member and an owner).
    call_command("demo_attach_requester", task=task.pk, stdout=io.StringIO())
    with system_access("t"):
        assert BoardSeat.objects.active().filter(person__user=admin).count() == 1
        assert Ownership.objects.active().filter(person__user=admin).count() == 1


def test_skips_homes_that_already_have_two_owners(poc):
    _seed("fresh_start")
    first = make_member(roles=[Role.SYSADMIN], email="first@portal-test.invalid")
    call_command("demo_attach_requester", task=_task(first).pk, stdout=io.StringIO())
    HostTask.objects.filter(state="running").update(state="succeeded")
    second = make_member(roles=[Role.SYSADMIN], email="second@portal-test.invalid")
    out = io.StringIO()
    call_command("demo_attach_requester", task=_task(second).pk, stdout=out)
    assert "demo home 2." in out.getvalue()


def test_flag_off_attaches_nothing(poc):
    _seed("fresh_start")
    admin = make_member(roles=[Role.SYSADMIN])
    task = _task(admin, attach=False)
    with pytest.raises(CommandError, match="asked for this"):
        call_command("demo_attach_requester", task=task.pk)
    with system_access("t"):
        assert not BoardSeat.objects.filter(person__user=admin).exists()
        assert not Ownership.objects.filter(person__user=admin).exists()
    assert not AuditEvent.objects.filter(action="system.demo_requester_attached").exists()


@pytest.mark.parametrize(
    ("allowed", "stage"), [(True, "production"), (True, "local"), (False, "poc")]
)
def test_refused_outside_the_poc(poc, allowed, stage):
    _seed("fresh_start")
    admin = make_member(roles=[Role.SYSADMIN])
    task = _task(admin)
    poc.SEED_DEMO_ALLOWED = allowed
    poc.DEPLOYMENT_STAGE = stage
    with pytest.raises(CommandError, match="only on the POC"):
        call_command("demo_attach_requester", task=task.pk)
    with system_access("t"):
        assert not BoardSeat.objects.filter(person__user=admin).exists()


def test_a_demo_login_is_never_attached(poc):
    from accounts.models import User

    _seed("fresh_start")
    demo_user = User.objects.get(email="fresh-board0@example.com")
    with pytest.raises(CommandError, match="non-demo"):
        call_command("demo_attach_requester", task=_task(demo_user).pk)
