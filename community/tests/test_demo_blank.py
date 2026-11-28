"""The blank demo dataset (owner's request, 0.45.0, D98): nothing but the real
logins a reseed keeps, settings at their defaults; the requester can get a
board seat (no home: there is none) to open the setup wizard and the
register import. Fresh start is unchanged."""

from __future__ import annotations

import io

import pytest
from django.core.management import call_command
from django.urls import reverse

from accounts import keep_logins
from audit.models import AuditEvent
from community import demo
from community.models import BoardSeat, Lot, Ownership, Person, Role
from community.tests.factories import make_member
from core import host_tasks
from core.models import DemoSeed, HostTask
from core.scoping import system_access

pytestmark = pytest.mark.django_db


@pytest.fixture
def poc(settings):
    settings.SEED_DEMO_ALLOWED = True
    settings.DEPLOYMENT_STAGE = "poc"
    return settings


def test_blank_is_registered_and_described():
    blank = demo.get("blank")
    assert blank.label == "Blank (empty portal)" and blank.has_homes is False
    assert "no homes, no owners, no board" in blank.description
    assert demo.get("fresh_start").has_homes and "Not empty: 44 homes" in (
        demo.get("fresh_start").description
    )
    assert demo.names() == ["oak_creek", "fresh_start", "blank"]
    out = io.StringIO()
    call_command("seed_demo", list_datasets=True, stdout=out)
    assert "blank" in out.getvalue().split()


def test_seeding_blank_creates_nothing_but_the_record(poc):
    admin = make_member(roles=[Role.SYSADMIN], email="real.admin@portal-test.invalid")
    with system_access("t"):
        people = Person.objects.count()
    out = io.StringIO()
    call_command("seed_demo", dataset="blank", stdout=out)
    assert "Blank portal: no homes, owners, board or history" in out.getvalue()
    with system_access("t"):
        assert Lot.objects.count() == 0 and Ownership.objects.count() == 0
        assert BoardSeat.objects.count() == 0 and Person.objects.count() == people
    assert DemoSeed.objects.get().dataset == "blank"
    assert AuditEvent.objects.get(action="system.demo_seeded").metadata == {"dataset": "blank"}
    assert admin.is_active


def test_reseed_with_blank_keeps_the_login_and_gives_a_seat_only(poc, client):
    """The reseed's Python steps in order: the real login is kept (here it
    already is: the test database is the 'new' one), blank seeds nothing, the
    requester gets a board seat and no home, and can then open the setup
    wizard and the register import."""
    admin = make_member(roles=[Role.SYSADMIN], email="real.admin@portal-test.invalid")
    assert [u.pk for u in keep_logins.kept_users()] == [admin.pk]
    call_command("seed_demo", dataset="blank", stdout=io.StringIO())
    task = HostTask.objects.create(
        kind="reseed_demo",
        dataset="blank",
        attach_requester=True,
        requested_by=admin,
        state="running",
    )
    out = io.StringIO()
    call_command("demo_attach_requester", task=task.pk, stdout=out)
    assert "Requester attached: board seat (no homes in this dataset)." in out.getvalue()
    with system_access("t"):
        assert BoardSeat.objects.active().filter(person__user=admin).count() == 1
        assert not Ownership.objects.filter(person__user=admin).exists()
    event = AuditEvent.objects.get(action="system.demo_requester_attached")
    assert event.metadata["ownership_id"] is None and event.metadata["lot_id"] is None
    client.force_login(admin)
    assert client.get(reverse("setup_home"), follow=True).status_code == 200
    assert client.post(reverse("setup_start"), follow=True).status_code == 200
    assert client.get(reverse("register_import")).status_code == 200


def test_the_reseed_request_accepts_blank(poc, rf):
    admin = make_member(roles=[Role.SYSADMIN])
    task = host_tasks.request_task(
        admin, "reseed_demo", dataset="blank", confirmation="RESEED", attach_requester=True
    )
    assert (task.dataset, task.attach_requester) == ("blank", True)


def test_kept_task_rows_carry_the_new_columns(poc):
    """keep_logins exports and imports the restore's backup point and the
    result-email mark with the server-task history (D98)."""
    admin = make_member(roles=[Role.SYSADMIN], email="real.admin@portal-test.invalid")
    HostTask.objects.create(
        kind="restore_backup", requested_by=admin, backup="local:2026-11-20T0230Z", state="failed"
    )
    data = keep_logins.export_data()
    row = data["tasks"][0]
    assert row["backup"] == "local:2026-11-20T0230Z" and "notified_at" in row
