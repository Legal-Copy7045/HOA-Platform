"""System status -> Restart the portal (D91): sysadmins only, a fresh sign-in
check, a confirmation step, audited, one at a time and at most once every 10
minutes, through the server-task allowlist (the agent side is in
test_host_tasks_agent.py)."""

from __future__ import annotations

import datetime

import pytest
from django.urls import reverse
from django.utils import timezone

from audit.models import AuditEvent
from community.models import Role
from community.tests.factories import LotFactory, make_member
from core import host_tasks
from core.models import HostTask, UpdateStatus

pytestmark = pytest.mark.django_db

URL = "/ops/status/restart/"
CONFIRM = {"confirm": host_tasks.RESTART_CONFIRM_VALUE}


@pytest.fixture
def admin():
    return make_member(roles=[Role.SYSADMIN])


def _reauth(client, user):
    client.force_login(user)
    session = client.session
    session["account_authentication_methods_user"] = str(user.pk)
    session["account_authentication_methods"] = [
        {"method": "mfa", "at": timezone.now().timestamp(), "reauthenticated": True}
    ]
    session.save()


def _agent_alive():
    UpdateStatus.objects.update_or_create(pk=1, defaults={"agent_seen_at": timezone.now()})


def test_url_name():
    assert reverse("system_restart") == URL


def test_restart_is_an_allowlisted_kind_with_its_own_page():
    spec = host_tasks.BY_KIND["restart_app"]
    assert spec.own_page == "system_restart" and spec.confirm.endswith(".")
    assert HostTask.Kind.RESTART_APP == "restart_app"
    assert "restart_app" in {s.kind for s in host_tasks.offered()}


@pytest.mark.parametrize("who", ["owner", "manager", "board", "auditor", "arc", "treasurer"])
def test_only_sysadmins_reach_the_page_or_restart(client, who):
    user = {
        "owner": lambda: make_member(lots=[LotFactory()]),
        "manager": lambda: make_member(roles=[Role.MANAGER]),
        "board": lambda: make_member(board=True),
        "auditor": lambda: make_member(roles=[Role.AUDITOR]),
        "arc": lambda: make_member(roles=[Role.ARC_REVIEWER]),
        "treasurer": lambda: make_member(board=True, lots=[LotFactory()]),
    }[who]()
    # Even with a fresh sign-in check: the role decides, before anything else.
    _reauth(client, user)
    assert client.get(URL).status_code == 403
    assert client.post(URL, CONFIRM).status_code == 403
    assert not HostTask.objects.exists()
    assert not AuditEvent.objects.filter(action="system.task_requested").exists()
    with pytest.raises(Exception, match=r"system\.run_task"):
        host_tasks.request_task(user, "restart_app", confirmation="restart")


def test_signed_out_visitors_are_sent_to_sign_in(client):
    response = client.get(URL)
    assert response.status_code == 302 and "/accounts/login/" in response["Location"]
    assert client.post(URL, CONFIRM).status_code == 302
    assert not HostTask.objects.exists()


def test_a_sysadmin_without_a_fresh_sign_in_check_is_asked_for_one(client, admin):
    client.force_login(admin)
    for response in (client.get(URL), client.post(URL, CONFIRM)):
        assert response.status_code == 302 and "reauthenticate" in response["Location"]
    assert not HostTask.objects.exists()


def test_the_confirmation_page_explains_and_does_nothing_by_itself(client, admin):
    _reauth(client, admin)
    page = client.get(URL).content.decode()
    assert "<h1>Restart the portal</h1>" in page
    assert "unavailable for up to a minute" in page
    assert f'name="confirm" value="{host_tasks.RESTART_CONFIRM_VALUE}"' in page
    assert not HostTask.objects.exists()


def test_confirmed_restart_is_requested_and_audited(client, admin):
    _agent_alive()
    _reauth(client, admin)
    response = client.post(URL, CONFIRM)
    assert response.status_code == 302 and response["Location"] == URL
    task = HostTask.objects.get()
    assert (task.kind, task.state, task.requested_by) == ("restart_app", "requested", admin)
    assert (task.dataset, task.attach_requester) == ("", False)
    event = AuditEvent.objects.get(action="system.task_requested")
    assert event.actor == admin and event.metadata == {"kind": "restart_app"}
    # The page shows progress.
    page = client.get(URL).content.decode()
    assert "Waiting for the server" in page and 'id="current-task"' in page
    assert "Restart the portal now</button>" in page and " disabled " in page


def test_finished_restart_is_audited_once_and_shown_with_its_result(client, admin):
    from django.core.management import call_command

    task = HostTask.objects.create(kind="restart_app", requested_by=admin)
    HostTask.objects.filter(pk=task.pk).update(
        state="succeeded",
        finished_at=timezone.now(),
        summary="Restarted the web server and the background worker; the site answers.",
    )
    call_command("record_host_tasks")
    call_command("record_host_tasks")
    events = AuditEvent.objects.filter(action="system.task_finished")
    assert [e.metadata for e in events] == [{"kind": "restart_app", "state": "succeeded"}]
    _reauth(client, admin)
    status = client.get(reverse("system_status")).content.decode()
    assert 'id="restart"' in status and "the site answers" in status
    assert f'href="{URL}"' in status
    assert "the site answers" in client.get(URL).content.decode()


@pytest.mark.parametrize("confirmation", ["", "yes", "RESTART", "restart "])
def test_without_the_confirmation_value_nothing_is_requested(client, admin, confirmation):
    _reauth(client, admin)
    response = client.post(URL, {"confirm": confirmation}, follow=True)
    assert "Confirm the restart on its own page first." in response.content.decode()
    assert not HostTask.objects.exists()


def test_server_tasks_start_button_goes_to_the_confirmation_page(client, admin):
    _reauth(client, admin)
    response = client.post(reverse("server_tasks"), {"kind": "restart_app"})
    assert response.status_code == 302 and response["Location"] == URL
    assert not HostTask.objects.exists()
    page = client.get(reverse("server_tasks")).content.decode()
    assert f'href="{URL}"' in page


def test_one_task_at_a_time(client, admin):
    HostTask.objects.create(kind="backup", requested_by=admin)
    _reauth(client, admin)
    response = client.post(URL, CONFIRM, follow=True)
    assert "already waiting or running" in response.content.decode()
    assert HostTask.objects.filter(kind="restart_app").count() == 0


def test_at_most_once_every_ten_minutes(client, admin):
    _reauth(client, admin)
    client.post(URL, CONFIRM)
    first = HostTask.objects.get()
    HostTask.objects.filter(pk=first.pk).update(state="succeeded", finished_at=timezone.now())
    response = client.post(URL, CONFIRM, follow=True)
    assert "Another restart can be asked for in 10 minutes." in response.content.decode()
    assert HostTask.objects.count() == 1
    # Once the cool-down has passed, the next one is accepted.
    HostTask.objects.filter(pk=first.pk).update(
        requested_at=timezone.now() - host_tasks.RESTART_COOLDOWN - datetime.timedelta(seconds=1)
    )
    client.post(URL, CONFIRM)
    assert HostTask.objects.filter(kind="restart_app").count() == 2
    assert AuditEvent.objects.filter(action="system.task_requested").count() == 2


def test_the_cool_down_counts_refused_and_failed_restarts_too(admin):
    task = HostTask.objects.create(kind="restart_app", requested_by=admin)
    HostTask.objects.filter(pk=task.pk).update(state="failed", finished_at=timezone.now())
    with pytest.raises(host_tasks.HostTaskRefused, match="minutes"):
        host_tasks.request_task(admin, "restart_app", confirmation="restart")
