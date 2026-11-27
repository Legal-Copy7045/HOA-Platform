"""D74 monitoring: background jobs, the incident timeline, storage growth and
the database connections check, and that each reaches System status and the
alert email without personal data."""

import datetime

import pytest
from django.core import mail
from django.core.management import call_command
from django.db import connection
from django.urls import reverse
from django.utils import timezone

from community.models import Role
from community.tests.factories import LotFactory, make_member
from core import growth, health, job_health, ops_timeline
from core.models import ErrorSignature, GrowthSnapshot, HostTask, OpsEvent

pytestmark = pytest.mark.django_db

ARG_VALUE = "tok_should_never_show_1234"


# --- Synthetic Procrastinate rows ----------------------------------------------------------


def _job(task, status, *, attempts=0, scheduled_at=None, events=()):
    """Insert a job row directly (the queue's own triggers may add a
    'deferred' event) and the given (type, at) events. The arguments hold a
    token-like value that must never be read or shown."""
    with connection.cursor() as cur:
        cur.execute(
            "INSERT INTO procrastinate_jobs (queue_name, task_name, args, status, attempts, "
            "scheduled_at) VALUES (%s, %s, %s::jsonb, %s, %s, %s) RETURNING id",
            ["maintenance", task, f'{{"token": "{ARG_VALUE}"}}', status, attempts, scheduled_at],
        )
        job_id = cur.fetchone()[0]
        cur.execute("DELETE FROM procrastinate_events WHERE job_id = %s", [job_id])
        for kind, at in events:
            cur.execute(
                "INSERT INTO procrastinate_events (job_id, type, at) VALUES (%s, %s, %s)",
                [job_id, kind, at],
            )
    return job_id


def _ago(**kw):
    return timezone.now() - datetime.timedelta(**kw)


def test_no_job_problems_on_a_quiet_queue():
    _job("core.tasks.worker_heartbeat", "succeeded", events=[("succeeded", _ago(minutes=2))])
    assert job_health.problems() == []
    assert health._jobs() == (True, "No failed, hanging or waiting jobs")


def test_failed_jobs_in_the_last_day_are_named_in_plain_words():
    _job("payments.tasks.daily_sync", "failed", events=[("failed", _ago(hours=2))])
    _job("payments.tasks.daily_sync", "failed", events=[("failed", _ago(hours=3))])
    _job("comms.tasks.weekly_digest", "failed", events=[("failed", _ago(days=2))])  # too old
    (problem,) = job_health.problems()
    assert (problem.kind, problem.task, problem.count) == ("failed", "daily_sync", 2)
    ok, detail = health._jobs()
    assert not ok
    assert detail.startswith("The daily Stripe check (refunds, payouts, reconciliation) failed 2")
    assert ARG_VALUE not in detail


def test_retried_hanging_and_waiting_jobs():
    _job("comms.tasks.send_invitation", "todo", attempts=5, events=[("deferred", _ago(minutes=1))])
    _job("core.tasks.health_alerts", "doing", events=[("started", _ago(minutes=90))])
    _job("core.tasks.growth_snapshot", "doing", events=[("started", _ago(minutes=5))])  # fine
    _job("comms.tasks.weekly_digest", "todo", events=[("deferred", _ago(minutes=40))])
    # Scheduled for later: not waiting.
    _job(
        "comms.tasks.event_reminders",
        "todo",
        scheduled_at=timezone.now() + datetime.timedelta(hours=1),
        events=[("deferred", _ago(hours=3))],
    )
    kinds = {(p.kind, p.task) for p in job_health.problems()}
    assert ("retried", "send_invitation") in kinds
    assert ("stuck", "health_alerts") in kinds
    assert ("stuck", "growth_snapshot") not in kinds
    backlog = [p for p in job_health.problems() if p.kind == "backlog"]
    assert len(backlog) == 1 and backlog[0].minutes >= 40
    assert "Weekly summary emails" in backlog[0].plain and backlog[0].count == 2


def test_unknown_task_names_still_read_plainly():
    assert job_health.what("some.app.tasks.brand_new") == "A background job (brand_new)"


def test_clearing_forgets_failures_seen_so_far():
    _job("payments.tasks.autopay", "failed", events=[("failed", _ago(hours=1))])
    assert not health._jobs()[0]
    health.clear("jobs")
    assert health._jobs()[0]
    _job("payments.tasks.autopay", "failed", events=[("failed", timezone.now())])
    assert not health._jobs()[0]


def test_job_failures_reach_the_alert_email_with_task_names_only():
    admin = make_member(roles=[Role.SYSADMIN])
    _job("ledger.tasks.send_statement_emails", "failed", events=[("failed", _ago(minutes=5))])
    results = [r for r in health.run_checks() if r.key == "jobs"]
    assert health.alert(results) == 1
    body = mail.outbox[-1].body
    assert mail.outbox[-1].to == [admin.email]
    assert "Monthly statement emails failed 1 time" in body
    assert "Technical: task send_statement_emails, failed" in body
    assert ARG_VALUE not in body


def test_system_status_lists_job_problems(client):
    admin = make_member(roles=[Role.SYSADMIN])
    _job("comms.tasks.weekly_digest", "failed", events=[("failed", _ago(minutes=5))])
    client.force_login(admin)
    page = client.get(reverse("system_status")).content.decode()
    assert "Background job problems" in page
    assert "Weekly summary emails failed 1 time" in page
    assert ARG_VALUE not in page


# --- The incident timeline -----------------------------------------------------------------


def _result(key, ok, detail="detail"):
    return health.Result(key, key.replace("_", " ").title(), ok, detail)


def test_health_transitions_and_alert_emails_are_kept_append_only():
    make_member(roles=[Role.SYSADMIN])
    now = timezone.now()
    health.alert([_result("scan_queue", False, "3 files waiting")], now=now)
    # Still failing: no second "started failing" line.
    health.alert([_result("scan_queue", False)], now=now + datetime.timedelta(minutes=10))
    later = now + datetime.timedelta(hours=1)
    health.alert([_result("scan_queue", True, "None waiting")], now=later)
    rows = list(OpsEvent.objects.order_by("at", "id"))
    kinds = [(r.kind, r.state) for r in rows]
    assert kinds == [
        ("scans", "problem"),
        ("alerts", "info"),
        ("scans", "recovered"),
        ("alerts", "info"),
    ]
    assert rows[0].message.startswith("Scan Queue: Started failing. 3 files waiting")
    assert rows[1].count == 1
    with pytest.raises(Exception):  # noqa: B017 (append-only at the model layer)
        rows[0].delete()


def test_an_alert_that_reaches_nobody_is_a_problem_on_the_timeline():
    health.alert([_result("disk", False)])
    alert = OpsEvent.objects.get(kind="alerts")
    assert alert.state == "problem" and "reached nobody" in alert.message


def test_timeline_merges_sources_newest_first_and_filters():
    sysadmin = make_member(roles=[Role.SYSADMIN])
    now = timezone.now()
    OpsEvent.objects.create(at=now - datetime.timedelta(hours=5), kind="backups", message="B")
    OpsEvent.objects.create(at=now - datetime.timedelta(hours=1), kind="health", message="H")
    ErrorSignature.objects.create(
        hour=now.replace(minute=0, second=0, microsecond=0),
        fingerprint="f" * 40,
        exc_type="ValueError",
        route="lot_ledger",
        method="GET",
        status=500,
        count=3,
        first_seen=now - datetime.timedelta(hours=3),
        last_seen=now,
    )
    HostTask.objects.create(
        kind=HostTask.Kind.BACKUP,
        requested_by=sysadmin,
        state=HostTask.State.SUCCEEDED,
        finished_at=now - datetime.timedelta(hours=2),
    )
    rows, more = ops_timeline.entries()
    assert [r.kind for r in rows] == ["health", "tasks", "errors", "backups"]
    assert not more
    assert rows[2].title.startswith("First seen: A home's account page failed")
    assert rows[1].title == "Take a backup: done"
    assert [r.kind for r in ops_timeline.entries("errors")[0]] == ["errors"]
    assert [r.kind for r in ops_timeline.entries("backups")[0]] == ["backups"]
    assert ops_timeline.entries("nonsense")[0] == rows  # unknown filter: everything


def test_timeline_pages():
    now = timezone.now()
    for i in range(ops_timeline.PER_PAGE + 3):
        at = now - datetime.timedelta(minutes=i)
        OpsEvent.objects.create(at=at, kind="health", message=f"#{i}")
    first, more = ops_timeline.entries(page=1)
    second, more2 = ops_timeline.entries(page=2)
    assert len(first) == ops_timeline.PER_PAGE and more
    assert [e.title for e in second] == ["#25", "#26", "#27"] and not more2
    # Older than the window: not shown.
    OpsEvent.objects.create(at=now - datetime.timedelta(days=100), kind="health", message="old")
    assert "old" not in [e.title for e in ops_timeline.entries(page=2)[0]]


def test_timeline_page_is_for_sysadmins_only(client):
    sysadmin = make_member(roles=[Role.SYSADMIN])
    board = make_member(board=True)
    owner = make_member(lots=[LotFactory(street_address="9 Private Way")])
    OpsEvent.objects.create(kind="updates", message="Update installed: Running abc")
    for user, code in ((owner, 403), (board, 403)):
        client.force_login(user)
        assert client.get(reverse("ops_timeline")).status_code == code
    client.force_login(sysadmin)
    page = client.get(reverse("ops_timeline") + "?kind=updates&page=1").content.decode()
    assert "Incident timeline" in page and "Update installed: Running abc" in page
    assert "9 Private Way" not in page and owner.email not in page
    status = client.get(reverse("system_status")).content.decode()
    assert "Update installed: Running abc" in status and reverse("ops_timeline") in status


def test_backup_heartbeats_and_host_alerts_go_on_the_timeline(monkeypatch):
    import io

    make_member(roles=[Role.SYSADMIN])
    call_command("backup_heartbeat", kind="restore_drill")
    assert OpsEvent.objects.get(kind="backups").message.startswith(
        "Restore drill passed: the signed backup verified"
    )
    monkeypatch.setattr("sys.stdin", io.StringIO("process opened /etc/shadow\n"))
    call_command("host_alert", kind="falco", stdout=io.StringIO())
    host = OpsEvent.objects.get(kind="host")
    assert host.state == "problem" and "Container behavior (Falco)" in host.message
    assert "/etc/shadow" not in host.message  # the alert text stays in the email


def test_record_never_breaks_the_caller(monkeypatch):
    def boom(**kwargs):
        raise RuntimeError("no table")

    monkeypatch.setattr(OpsEvent.objects, "create", boom)
    assert ops_timeline.record("health", "x") is None


def test_stripe_trouble_is_tallied_per_hour():
    ops_timeline.bump("stripe", "network")
    ops_timeline.bump("stripe", "http_503")
    assert ops_timeline.tally("stripe", hours=3) == (2, "http_503")
    assert ops_timeline.tally("other", hours=3) == (0, "")


# --- Growth --------------------------------------------------------------------------------


D0 = datetime.date(2026, 10, 1)


def _points(values):
    return [(D0 + datetime.timedelta(days=i), v) for i, v in enumerate(values)]


def test_slope_needs_a_week_and_rounds_in_whole_bytes():
    assert growth.slope_per_day(_points([1, 2, 3])) is None
    assert growth.slope_per_day(_points([100 + 10 * i for i in range(10)])) == 10
    # 1.5 a day rounds half away from zero.
    assert growth.slope_per_day(_points([0, 1, 3, 4, 6, 7, 9, 10])) in (1, 2)
    assert growth.slope_per_day(_points([50 - 3 * i for i in range(8)])) == -3
    same_day = [(D0, 1)] * 8
    assert growth.slope_per_day(same_day) is None


def test_days_until_full():
    rising = _points([1000 + 100 * i for i in range(14)])
    assert growth.days_until_full(rising, free_bytes=5000) == 50
    assert growth.days_until_full(rising, free_bytes=0) == 0
    flat = _points([1000] * 14)
    assert growth.days_until_full(flat, free_bytes=5000) is None
    assert growth.days_until_full(rising[:3], free_bytes=5000) is None


def _snap(day, used, total=1000 * 1024**2):
    return GrowthSnapshot.objects.create(
        taken_on=day,
        database_bytes=10 * 1024**2,
        largest_tables=[["audit_auditevent", 1024]],
        connections=3,
        max_connections=100,
        disk_total_bytes=total,
        disk_free_bytes=total - used,
        disk_source="agent",
    )


def test_growth_check_uses_the_boards_warning_rule():
    from community import services as community_services
    from core.policy import SYSTEM

    today = timezone.localdate()
    assert health._storage_growth() is None  # nothing recorded yet
    for i in range(10):
        _snap(today - datetime.timedelta(days=10 - i), used=500 * 1024**2 + i * 10 * 1024**2)
    # 10 MB a day, about 410 MB free: about 41 days.
    ok, detail = health._storage_growth()
    assert not ok and "full in about 4" in detail and "warning at 60" in detail
    community_services.set_rule(
        key="monitoring.disk_full_warn_days", value="30", citation="Board policy", actor=SYSTEM
    )
    ok, detail = health._storage_growth()
    assert ok and "full in about 4" in detail


def test_growth_check_waits_for_a_week_of_figures():
    _snap(timezone.localdate() - datetime.timedelta(days=1), used=1)
    ok, detail = health._storage_growth()
    assert ok and detail.startswith("Measuring: 1 day(s)")


def test_snapshot_records_once_a_day_with_sizes_and_names_only():
    snap = growth.snapshot()
    assert snap is not None and snap.database_bytes > 0
    assert snap.max_connections >= snap.connections >= 1
    assert all(isinstance(name, str) and size >= 0 for name, size in snap.largest_tables)
    assert snap.disk_total_bytes and snap.disk_source == "container"
    assert snap.files_bytes == 0 and snap.files_count == 0
    assert growth.snapshot() is None  # once a day
    with pytest.raises(Exception):  # noqa: B017 (append-only)
        GrowthSnapshot.objects.filter(pk=snap.pk).delete()


def test_snapshot_prefers_the_agents_disk_report():
    from core.models import UpdateStatus

    status = UpdateStatus.get()
    status.agent_seen_at = timezone.now()
    status.host_resources = {"mem_total_kb": 1, "disk_total_kb": 1000, "disk_avail_kb": 250}
    status.save()
    assert growth.disk() == (1000 * 1024, 250 * 1024, "agent")


def test_connections_check_warns_above_80_percent(monkeypatch):
    monkeypatch.setattr(growth, "connections", lambda: (81, 100))
    ok, detail = health._connections()
    assert not ok and detail.startswith("81 of 100 in use (81%)")
    monkeypatch.setattr(growth, "connections", lambda: (80, 100))
    assert health._connections() == (True, "80 of 100 in use (80%)")


def test_connections_are_read_from_postgres():
    used, maximum = growth.connections()
    assert 1 <= used <= maximum


def test_sparkline_points():
    assert growth.sparkline([5]) == ""
    points = growth.sparkline([0, 10], width=100, height=40).split()
    assert points == ["0.0,38.0", "100.0,2.0"]


def test_system_status_shows_the_trend(client):
    today = timezone.localdate()
    for i in range(8):
        _snap(today - datetime.timedelta(days=8 - i), used=100 * 1024**2 + i * 1024**2)
    client.force_login(make_member(roles=[Role.SYSADMIN]))
    page = client.get(reverse("system_status")).content.decode()
    assert "Storage and database growth" in page and "<polyline" in page
    assert "audit_auditevent" in page


def test_the_new_checks_are_registered():
    keys = [k for k, _label, _fn in health.CHECKS]
    for key in ("jobs", "payment_failures", "bounce_mailbox", "connections", "storage_growth"):
        assert key in keys
    assert "jobs" in health.CLEARABLE
