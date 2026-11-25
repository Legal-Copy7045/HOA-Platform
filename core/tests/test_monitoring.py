"""Milestone 4: health checks and alerts to sysadmins."""

import datetime

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
