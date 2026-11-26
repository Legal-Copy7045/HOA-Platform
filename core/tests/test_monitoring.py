"""Milestone 4: health checks and alerts to sysadmins."""

import datetime
import re

import pytest
from django.core import mail
from django.core.management import CommandError, call_command
from django.urls import reverse
from django.utils import timezone

from community.models import Role
from community.tests.factories import make_member
from core import health
from core.models import AlertState, ErrorTally, Heartbeat

pytestmark = pytest.mark.django_db


def _result(key, ok):
    return health.Result(key, key.title(), ok, "detail")


def test_worker_heartbeat_freshness():
    assert health._worker()[0] is False  # never reported
    Heartbeat.objects.create(name="worker", at=timezone.now())
    assert health._worker()[0] is True
    Heartbeat.objects.filter(name="worker").update(at=timezone.now() - datetime.timedelta(hours=1))
    assert health._worker()[0] is False


def test_alert_on_failure_then_quiet_then_recovery():
    admin = make_member(roles=[Role.SYSADMIN])
    now = timezone.now()
    assert health.alert([_result("disk", False)], now=now) == 1
    assert mail.outbox[-1].to == [admin.email] and "PROBLEM" in mail.outbox[-1].body
    # Still failing an hour later: no repeat yet.
    assert health.alert([_result("disk", False)], now=now + datetime.timedelta(hours=1)) == 0
    # Still failing after the re-alert interval: reminded.
    later = now + datetime.timedelta(hours=health.REALERT_HOURS + 1)
    assert health.alert([_result("disk", False)], now=later) == 1
    assert health.alert([_result("disk", True)], now=later) == 1
    assert "RECOVERED" in mail.outbox[-1].body
    assert AlertState.objects.get(name="disk").failing is False


def test_alert_emails_carry_no_personal_data():
    make_member(roles=[Role.SYSADMIN])
    health.alert([_result("errors", False)])
    body = mail.outbox[-1].body
    assert "@" not in body.split("Details:")[0]


def test_server_errors_are_counted_without_detail():
    health.record_server_error()
    health.record_server_error()
    assert sum(ErrorTally.objects.values_list("count", flat=True)) == 2
    assert health._errors()[0] is True


def test_command_exits_nonzero_when_a_check_fails():
    with pytest.raises(CommandError):  # the worker has never reported in tests
        call_command("health_check")


def test_status_page_shows_checks_to_sysadmins_only(client):
    client.force_login(make_member(roles=[Role.SYSADMIN]))
    page = client.get(reverse("system_status")).content
    assert b"Health checks" in page and b"Background worker" in page
    client.force_login(make_member(lots=[]))
    assert client.get(reverse("system_status")).status_code == 403


def test_send_me_a_test_email_goes_only_to_me(client, settings):
    from audit.models import AuditEvent

    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    admin = make_member(roles=[Role.SYSADMIN])
    client.force_login(admin)
    response = client.post(
        reverse("system_status"), {"action": "test_email", "to": "someone@example.com"}
    )
    assert response.status_code == 302
    assert [m.to for m in mail.outbox] == [[admin.email]]
    assert AuditEvent.objects.filter(action="system.test_email_sent").exists()
    owner = make_member(lots=[])
    client.force_login(owner)
    assert client.post(reverse("system_status"), {"action": "test_email"}).status_code == 403


def test_update_controls_only_record_a_request(client):
    from audit.models import AuditEvent
    from core.models import UpdateStatus

    admin = make_member(roles=[Role.SYSADMIN])
    client.force_login(admin)
    page = client.get(reverse("system_status")).content
    assert b"update agent" in page  # not installed yet: says how
    UpdateStatus.objects.create(
        pk=1,
        running_commit="aaa1111",
        latest_commit="bbb2222",
        behind=2,
        changes="Fix one\nFix two",
        agent_seen_at=timezone.now(),
        checked_at=timezone.now(),
    )
    page = client.get(reverse("system_status")).content.decode()
    assert "Update available: 2 changes" in page and "Install update" in page
    client.post(reverse("system_status"), {"action": "install_update"})
    status = UpdateStatus.objects.get(pk=1)
    assert status.state == "requested" and status.requested_by == admin
    client.post(reverse("system_status"), {"action": "install_update"})  # no double request
    assert AuditEvent.objects.filter(action="system.install_update").count() == 2
    client.post(reverse("system_status"), {"action": "check_updates"})
    assert UpdateStatus.objects.get(pk=1).check_requested_at is not None
    board = make_member(board=True)
    client.force_login(board)
    assert client.post(reverse("system_status"), {"action": "install_update"}).status_code == 403


def test_agent_script_passes_values_as_psql_variables_only():
    from pathlib import Path

    text = Path("infra/poc/portal-agent.sh").read_text()
    assert ":'changes'" in text and "flock -n" in text
    assert '-c "$' not in text  # no SQL built from shell strings


def test_host_alert_goes_to_sysadmins_only_and_is_audited(settings):
    import io

    from audit.models import AuditEvent

    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    admin = make_member(roles=[Role.SYSADMIN])
    make_member(board=True)
    make_member()
    stdin = io.StringIO("1 x WARNING: Terminal shell in container\x1b[31m\x07\n" + "x" * 20000)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("sys.stdin", stdin)
        call_command("host_alert", "--kind", "falco", stdout=io.StringIO())
    assert [m.to for m in mail.outbox] == [[admin.email]]
    body = mail.outbox[0].body
    assert "Terminal shell in container" in body and "\x1b" not in body and "\x07" not in body
    assert len(body) < 8500  # capped
    event = AuditEvent.objects.get(action="system.host_alert")
    assert event.metadata == {"kind": "falco", "lines": 2, "sent": 1}


def test_host_alert_refuses_empty_input_and_unknown_kinds():
    import io

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("sys.stdin", io.StringIO(" \n\x00"))
        with pytest.raises(CommandError):
            call_command("host_alert", "--kind", "audit")
        with pytest.raises(CommandError):
            call_command("host_alert", "--kind", "anything")


def test_status_page_shows_host_security(client):
    from core.models import UpdateStatus

    client.force_login(make_member(roles=[Role.SYSADMIN]))
    status = UpdateStatus.objects.create(pk=1, agent_seen_at=timezone.now())
    assert "Not set up" in client.get(reverse("system_status")).content.decode()
    status.host_security = {
        "falco": "active",
        "auditd": "active",
        "updates": "active",
        "integrity": "active",
        "alerts": "failed",
        "livepatch": "<b>x</b>",
    }
    status.save()
    page = client.get(reverse("system_status")).content.decode()
    assert "Container monitoring (Falco): on" in page
    assert "Alert emails: <strong>failed</strong>" in page
    assert "<b>x</b>" not in page  # agent values are escaped


def test_harden_script_is_idempotent_and_keeps_secrets_off_argv():
    from pathlib import Path

    text = Path("infra/poc/harden-host.sh").read_text()
    # No mail server or other recommended extras on the host.
    installs = re.findall(r"apt-get install[^\n]*", text)
    assert installs and all("--no-install-recommends" in i for i in installs)
    assert "set -euo pipefail" in text and '"$(id -u)" -eq 0' in text
    assert 'Automatic-Reboot "false"' in text
    assert "modern_ebpf" in text and "falco-modern-bpf.service" in text
    assert "host_alert --kind falco" in text and "alert integrity" in text and "alert audit" in text
    assert "--checkpoint" in text  # audit events reported once
    assert "/opt/hoa-portal" not in text.split("APP_DIR=", 1)[1].split("\n", 1)[0]
    # The agent reports every unit the script installs.
    agent = Path("infra/poc/portal-agent.sh").read_text()
    for unit in ("falco-modern-bpf.service", "hoa-integrity-check.timer", "hoa-host-alerts.timer"):
        assert unit in text and unit in agent
    assert "PASSWORD" not in text and "SECRET" not in text
