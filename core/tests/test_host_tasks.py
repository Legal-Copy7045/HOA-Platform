"""Server tasks (Admin -> Server tasks, decision D36): who may start and see
them, one at a time, audit, the page, and the agent's SQL against the real
table. The agent's bash side is in test_host_tasks_agent.py."""

from __future__ import annotations

import datetime
import io
import re
from pathlib import Path

import pytest
from django.core.management import call_command
from django.db import IntegrityError, connection, transaction
from django.urls import reverse
from django.utils import timezone

from audit.models import AuditEvent
from community.models import Role
from community.tests.factories import LotFactory, make_member
from core import host_tasks
from core.models import Heartbeat, HostTask, UpdateStatus

pytestmark = pytest.mark.django_db

URL = "/ops/tasks/"


@pytest.fixture
def admin():
    return make_member(roles=[Role.SYSADMIN])


def _agent_alive():
    UpdateStatus.objects.update_or_create(pk=1, defaults={"agent_seen_at": timezone.now()})


def test_url_name():
    assert reverse("server_tasks") == URL


def test_every_kind_has_wording_and_offsite_tasks_wait_for_offsite(settings):
    assert {spec.kind for spec in host_tasks.TASKS} == set(HostTask.Kind.values)
    assert all(spec.confirm.endswith((".", "?")) for spec in host_tasks.TASKS)
    settings.BACKUP_S3_BUCKET = ""
    kinds = {spec.kind for spec in host_tasks.offered()}
    assert "restore_drill_offsite" not in kinds and "offsite_lock_check" not in kinds
    assert {"backup", "restore_drill", "harden_host", "health_check"} <= kinds
    settings.BACKUP_S3_BUCKET = "backups"
    assert {spec.kind for spec in host_tasks.offered()} == set(HostTask.Kind.values)


def test_sysadmin_starts_a_backup_and_it_is_audited(client, admin):
    client.force_login(admin)
    response = client.post(URL, {"kind": "backup"})
    assert response.status_code == 302 and response["Location"] == URL
    task = HostTask.objects.get()
    assert (task.kind, task.state, task.requested_by) == ("backup", "requested", admin)
    event = AuditEvent.objects.get(action="system.task_requested")
    assert event.actor == admin and event.metadata == {"kind": "backup"}


@pytest.mark.parametrize("who", ["owner", "manager", "board", "auditor", "arc"])
def test_only_sysadmins_start_tasks(client, who):
    user = {
        "owner": lambda: make_member(lots=[LotFactory()]),
        "manager": lambda: make_member(roles=[Role.MANAGER]),
        "board": lambda: make_member(board=True),
        "auditor": lambda: make_member(roles=[Role.AUDITOR]),
        "arc": lambda: make_member(roles=[Role.ARC_REVIEWER]),
    }[who]()
    client.force_login(user)
    assert client.post(URL, {"kind": "backup"}).status_code == 403
    assert not HostTask.objects.exists()
    with pytest.raises(Exception, match=r"system\.run_task"):
        host_tasks.request_task(user, "backup")


@pytest.mark.parametrize("who", ["owner", "manager", "auditor", "arc"])
def test_only_board_and_sysadmins_see_the_page(client, who):
    user = {
        "owner": lambda: make_member(lots=[LotFactory()]),
        "manager": lambda: make_member(roles=[Role.MANAGER]),
        "auditor": lambda: make_member(roles=[Role.AUDITOR]),
        "arc": lambda: make_member(roles=[Role.ARC_REVIEWER]),
    }[who]()
    client.force_login(user)
    assert client.get(URL).status_code == 403


def test_anonymous_is_sent_to_sign_in(client):
    response = client.get(URL)
    assert response.status_code == 302 and "login" in response["Location"]


def test_board_sees_the_history_but_no_start_buttons(client, admin):
    HostTask.objects.create(
        kind="backup",
        requested_by=admin,
        state="succeeded",
        finished_at=timezone.now(),
        summary="Backup 2026-11-26T1830Z, 5.3M, copied off-site",
    )
    client.force_login(make_member(board=True))
    page = client.get(URL).content.decode()
    assert "<h1>Server tasks</h1>" in page
    assert "Backup 2026-11-26T1830Z, 5.3M, copied off-site" in page and admin.email in page
    assert 'name="kind"' not in page and "Take a backup now</button>" not in page
    assert "Sysadmins start tasks; the board sees the history." in page


def test_sysadmin_page_offers_the_tasks_with_confirm_texts(client, admin, settings):
    settings.BACKUP_S3_BUCKET = ""
    _agent_alive()
    client.force_login(admin)
    page = client.get(URL).content.decode()
    assert 'data-confirm="Take a backup now? It takes about a minute; the site stays up."' in page
    assert "uses extra memory on the server for a few minutes" in page
    assert 'value="restore_drill_offsite"' not in page  # off-site not set up
    assert "Tasks will not run right now" not in page
    assert "Portal update" in page and "Send a test email" in page


def test_one_task_at_a_time(client, admin):
    client.force_login(admin)
    client.post(URL, {"kind": "backup"})
    response = client.post(URL, {"kind": "health_check"}, follow=True)
    assert "already waiting or running" in response.content.decode()
    assert HostTask.objects.count() == 1
    assert AuditEvent.objects.filter(action="system.task_requested").count() == 1
    page = client.get(URL).content.decode()
    assert 'id="current-task"' in page and " disabled" in page
    # Once it has finished, the next one may start.
    HostTask.objects.update(state="succeeded", finished_at=timezone.now())
    client.post(URL, {"kind": "health_check"})
    assert HostTask.objects.filter(state="requested").get().kind == "health_check"


def test_the_database_also_refuses_a_second_active_task(admin):
    HostTask.objects.create(kind="backup", requested_by=admin, state="running")
    with pytest.raises(IntegrityError), transaction.atomic():
        HostTask.objects.create(kind="health_check", requested_by=admin)


def test_unknown_or_unoffered_kinds_are_refused(client, admin, settings):
    settings.BACKUP_S3_BUCKET = ""
    client.force_login(admin)
    for kind in ("", "rm -rf /", "restore_drill_offsite", "send_test_email"):
        response = client.post(URL, {"kind": kind}, follow=True)
        assert "not offered on this server" in response.content.decode()
    assert not HostTask.objects.exists()


def test_history_is_kept(admin):
    task = HostTask.objects.create(kind="backup", requested_by=admin)
    with pytest.raises(PermissionError):
        task.delete()


def test_agent_text_is_cleaned_and_escaped_on_the_page(client, admin):
    HostTask.objects.create(
        kind="backup",
        requested_by=admin,
        state="failed",
        finished_at=timezone.now(),
        summary="bad \x1b[31m<script>x</script>\x07",
        output_tail="line one\n\x1b[0m<b>two</b>",
    )
    client.force_login(admin)
    page = client.get(URL).content.decode()
    assert "\x1b" not in page and "\x07" not in page
    assert "<script>x</script>" not in page and "&lt;script&gt;" in page
    assert "&lt;b&gt;two&lt;/b&gt;" in page


def test_page_warns_when_the_agent_is_not_running(client, admin):
    client.force_login(admin)
    assert "has not reported yet" in client.get(URL).content.decode()
    UpdateStatus.objects.create(pk=1, agent_seen_at=timezone.now() - datetime.timedelta(minutes=9))
    page = client.get(URL).content.decode()
    assert "Tasks will not run right now" in page and "last seen" in page
    # A long task keeps the agent busy: no warning while it runs.
    HostTask.objects.create(kind="restore_drill", requested_by=admin, state="running")
    assert "Tasks will not run right now" not in client.get(URL).content.decode()
    HostTask.objects.update(state="succeeded")
    _agent_alive()
    assert "Tasks will not run right now" not in client.get(URL).content.decode()


def test_backups_card_shows_local_offsite_and_lock(client, admin, settings):
    settings.BACKUP_S3_BUCKET = "backups"
    now = timezone.now()
    for name in ("backup_local", "backup_offsite", "offsite_lock"):
        Heartbeat.objects.create(name=name, at=now)
    client.force_login(admin)
    page = client.get(URL).content.decode()
    card = page.split('id="backups"', 1)[1].split("</section>", 1)[0]
    assert "Last backup on the server" in card and "Off-site lock verified" in card
    assert "None yet" not in card and "Not verified" not in card
    assert "Take a backup now" in card


def test_system_status_has_take_a_backup_now_on_the_backups_row(client, admin):
    client.force_login(admin)
    page = client.get(reverse("system_status")).content.decode()
    row = page.split('<div class="title">Backups</div>', 1)[1].split("</li>", 1)[0]
    assert f'action="{URL}"' in row and 'value="backup"' in row and "Take a backup now" in row
    assert 'name="next" value="system_status"' in row
    response = client.post(URL, {"kind": "backup", "next": "system_status"})
    assert response["Location"] == reverse("system_status")
    assert HostTask.objects.get().kind == "backup"
    # Any other "next" goes back to Server tasks: never a URL from the form.
    HostTask.objects.update(state="succeeded")
    response = client.post(URL, {"kind": "health_check", "next": "https://evil.example/"})
    assert response["Location"] == URL


def test_finished_tasks_are_audited_once_with_kind_and_state(admin):
    task = HostTask.objects.create(
        kind="backup", requested_by=admin, state="succeeded", finished_at=timezone.now()
    )
    HostTask.objects.create(kind="health_check", requested_by=admin, state="running")
    out = io.StringIO()
    call_command("record_host_tasks", stdout=out)
    call_command("record_host_tasks", stdout=out)
    events = AuditEvent.objects.filter(action="system.task_finished")
    assert [e.metadata for e in events] == [{"kind": "backup", "state": "succeeded"}]
    task.refresh_from_db()
    assert task.audited_at is not None


# --- The agent's SQL, run against the real table -------------------------------------------

AGENT = Path("infra/poc/host-tasks.sh").read_text()


def _sql(start: str) -> str:
    """The heredoc SQL in host-tasks.sh that starts with ``start``."""
    match = re.search(re.escape(start) + r".*?;\n", AGENT, re.S)
    assert match, start
    return match.group(0)


def _bind(sql: str) -> tuple[str, dict]:
    """psql's :'name' variables as driver parameters, for the test only."""
    names = re.findall(r":'(\w+)'", sql)
    return re.sub(r":'(\w+)'", r"%(\1)s", sql), names


def test_agent_claims_the_oldest_request_and_writes_back_through_variables(admin):
    older = HostTask.objects.create(
        kind="backup", requested_by=admin, requested_at=timezone.now() - datetime.timedelta(1)
    )
    claim = _sql("UPDATE core_hosttask SET state = 'running'")
    with connection.cursor() as cur:
        cur.execute(claim)
        assert cur.fetchall() == [(older.pk, "backup")]
        cur.execute(claim)
        assert cur.fetchall() == []  # nothing else waiting
    finish, names = _bind(_sql("UPDATE core_hosttask\n   SET state = :'state'"))
    assert set(names) == {"state", "summary", "tail", "id"}
    with connection.cursor() as cur:
        cur.execute(
            finish,
            {"state": "succeeded", "summary": "Backup x', 1M", "tail": "a\nb", "id": str(older.pk)},
        )
    older.refresh_from_db()
    assert (older.state, older.summary, older.output_tail) == ("succeeded", "Backup x', 1M", "a\nb")
    assert older.started_at and older.finished_at


def test_agent_marks_a_cut_off_task_failed(admin):
    task = HostTask.objects.create(kind="harden_host", requested_by=admin, state="running")
    with connection.cursor() as cur:
        cur.execute(_sql("UPDATE core_hosttask\n   SET state = 'failed'"))
    task.refresh_from_db()
    assert task.state == "failed" and task.summary.startswith("Interrupted")
