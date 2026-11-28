"""Backups -> Restore from a backup (D98): sysadmins only, a fresh passkey or
authenticator check, a listed backup point, RESTORE typed exactly, audited,
the board emailed before it starts, one at a time, through the server-task
allowlist (the agent and script side is in test_restore_agent.py). Plus the
result emails every server task sends (core/task_mail.py)."""

from __future__ import annotations

import datetime
import time
from unittest import mock

import pytest
from django.core import mail
from django.urls import reverse
from django.utils import timezone

from audit.models import AuditEvent
from community.models import Role
from community.tests.factories import LotFactory, make_member
from core import backup_points, host_tasks, task_mail
from core.models import HostTask, UpdateStatus

pytestmark = pytest.mark.django_db

URL = "/ops/tasks/restore/"
OLD = "2026-11-20T0230Z"
NEW = "2026-11-26T0230Z"


@pytest.fixture
def admin():
    return make_member(roles=[Role.SYSADMIN])


@pytest.fixture
def board():
    return make_member(board=True)


def _mfa(client, user, *, kind="totp", age=0, method="mfa"):
    client.force_login(user)
    session = client.session
    session["account_authentication_methods"] = [
        {"method": method, "type": kind, "at": time.time() - age, "reauthenticated": True}
    ]
    session.save()


def _points(local=None, offsite=None, version="0.44.0-abc1234"):
    rows = (
        local
        if local is not None
        else [
            {"stamp": OLD, "size_kb": 5300, "version": version, "kept": False},
            {"stamp": NEW, "size_kb": 6100, "version": version, "kept": True},
        ]
    )
    UpdateStatus.objects.update_or_create(
        pk=1,
        defaults={
            "agent_seen_at": timezone.now(),
            "backup_points": {
                "at": timezone.now().isoformat(),
                "local": rows,
                "offsite": offsite or [],
                "offsite_at": timezone.now().isoformat(),
            },
        },
    )


def _post(client, backup=f"local:{OLD}", confirm="RESTORE"):
    return client.post(URL, {"backup": backup, "confirm": confirm})


def test_url_and_allowlist():
    assert reverse("backup_restore") == URL
    spec = host_tasks.BY_KIND["restore_backup"]
    assert spec.own_page == "backup_restore"
    assert HostTask.Kind.RESTORE_BACKUP == "restore_backup"
    assert "restore_backup" in {s.kind for s in host_tasks.offered()}


@pytest.mark.parametrize("who", ["owner", "manager", "board", "auditor", "arc"])
def test_only_sysadmins_reach_the_page_or_restore(client, who):
    user = {
        "owner": lambda: make_member(lots=[LotFactory()]),
        "manager": lambda: make_member(roles=[Role.MANAGER]),
        "board": lambda: make_member(board=True),
        "auditor": lambda: make_member(roles=[Role.AUDITOR]),
        "arc": lambda: make_member(roles=[Role.ARC_REVIEWER]),
    }[who]()
    _points()
    _mfa(client, user)
    assert client.get(URL).status_code == 403
    assert _post(client).status_code == 403
    assert not HostTask.objects.exists()


def test_signed_out_visitors_go_to_sign_in(client):
    response = client.get(URL)
    assert response.status_code == 302 and "login" in response.url


@pytest.mark.parametrize(
    ("kind", "method", "age"),
    [
        (None, "password", 0),  # a password alone
        ("code", "mfa", 0),  # an emailed code
        ("recovery_codes", "mfa", 0),
        ("totp", "mfa", 6 * 60),  # older than five minutes
    ],
)
def test_without_a_fresh_second_factor_nothing_is_asked(client, admin, kind, method, age):
    _points()
    _mfa(client, admin, kind=kind, method=method, age=age)
    response = client.get(URL)
    assert response.status_code == 302 and "reauthenticate" in response.url
    response = _post(client)
    assert response.status_code == 302 and "reauthenticate" in response.url
    assert not HostTask.objects.exists()


def test_the_service_checks_the_second_factor_too(admin, rf):
    _points()
    request = rf.post(URL)
    request.user = admin
    request.session = {}
    with pytest.raises(host_tasks.HostTaskRefused, match="passkey or authenticator"):
        host_tasks.request_task(
            admin, "restore_backup", request=request, backup=f"local:{OLD}", confirmation="RESTORE"
        )
    with pytest.raises(host_tasks.HostTaskRefused):
        host_tasks.request_task(
            admin, "restore_backup", backup=f"local:{OLD}", confirmation="RESTORE"
        )


def test_the_page_lists_points_and_says_what_happens(client, admin):
    _points(offsite=[{"stamp": "2026-11-25T0230Z", "size_kb": 7000, "version": "0.43.1-def5678"}])
    _mfa(client, admin)
    page = client.get(URL).content.decode()
    assert f'value="local:{OLD}"' in page and f'value="local:{NEW}"' in page
    assert 'value="offsite:2026-11-25T0230Z"' in page
    assert "made by release 0.44.0" in page and "5.2 MB" in page and "safety backup" in page
    assert "safety backup" in page.lower() and "logins" in page and "signs in again" in page
    assert "Type RESTORE to confirm" in page


@pytest.mark.parametrize("confirm", ["", "restore", "Restore", "RESTOR", "RESTORE!", "YES"])
def test_the_word_must_be_typed_exactly(client, admin, confirm):
    _points()
    _mfa(client, admin)
    response = _post(client, confirm=confirm)
    assert response.status_code == 200
    assert not HostTask.objects.exists()


@pytest.mark.parametrize(
    "backup",
    [
        "",
        OLD,  # no where
        "local:2026-11-21T0230Z",  # well formed, not listed
        "local:../../etc/passwd",
        "local:/var/backups/hoa-portal/2026-11-20T0230Z",
        "local:2026-11-20T0230Z/../x",
        "remote:2026-11-20T0230Z",
        "offsite:2026-11-20T0230Z",  # listed locally only
        "local:2026-11-20T0230Z; rm -rf /",
    ],
)
def test_only_a_listed_backup_point_is_accepted(client, admin, backup):
    _points()
    _mfa(client, admin)
    response = _post(client, backup=backup)
    assert response.status_code == 200
    assert not HostTask.objects.exists()
    with pytest.raises(host_tasks.HostTaskRefused):
        host_tasks.check_restore(_fresh_request(admin), backup, "RESTORE")


def _fresh_request(user):
    request = mock.Mock()
    request.user = user
    request.session = {
        "account_authentication_methods": [{"method": "mfa", "type": "totp", "at": time.time()}]
    }
    return request


def test_the_database_refuses_a_path_as_a_backup_point(admin):
    from django.db import IntegrityError, transaction

    with pytest.raises(IntegrityError), transaction.atomic():
        HostTask.objects.create(
            kind="restore_backup", requested_by=admin, backup="local:../../etc/passwd"
        )
    with pytest.raises(IntegrityError), transaction.atomic():
        HostTask.objects.create(kind="backup", requested_by=admin, backup=f"local:{OLD}")


def test_a_newer_release_or_unknown_one_cannot_be_chosen(client, admin, settings):
    settings.APP_RELEASE = "0.45.0"
    _points(
        local=[
            {"stamp": OLD, "size_kb": 1, "version": "0.46.0-abc1234"},
            {"stamp": NEW, "size_kb": 1, "version": ""},
        ]
    )
    _mfa(client, admin)
    page = client.get(URL).content.decode()
    assert "newer than the installed 0.45.0" in page and "does not say which release" in page
    for stamp in (OLD, NEW):
        assert _post(client, backup=f"local:{stamp}").status_code == 200
    assert not HostTask.objects.exists()
    older = backup_points.BackupPoint("local", OLD, 1, "0.44.0-abc")
    assert older.older and not older.refusal


def test_a_restore_is_requested_audited_and_the_board_told(client, admin, board):
    _points()
    _mfa(client, admin)
    mail.outbox.clear()
    response = _post(client)
    assert response.status_code == 302
    task = HostTask.objects.get()
    assert task.kind == "restore_backup" and task.backup == f"local:{OLD}"
    assert task.requested_by == admin and task.state == "requested"
    event = AuditEvent.objects.get(action="system.task_requested")
    assert event.metadata == {"kind": "restore_backup", "backup": f"local:{OLD}"}
    to = {m.to[0] for m in mail.outbox}
    assert board.email in to and admin.email in to
    assert all("restore" in m.subject.lower() for m in mail.outbox)
    # One at a time.
    response = _post(client, backup=f"local:{NEW}")
    assert response.status_code == 200 and HostTask.objects.count() == 1


def test_previewing_as_an_owner_refuses(admin):
    from core import preview

    _points()
    token = preview._current.set(preview.Preview(user_id=admin.pk, lot_id=1, lot_number=1))
    try:
        with pytest.raises(host_tasks.HostTaskRefused, match="homeowner view"):
            host_tasks.check_restore(_fresh_request(admin), f"local:{OLD}", "RESTORE")
    finally:
        preview._current.reset(token)


def test_the_plain_start_button_redirects_to_the_page(client, admin):
    client.force_login(admin)
    response = client.post("/ops/tasks/", {"kind": "restore_backup"})
    assert response.status_code == 302 and response.url == URL
    assert not HostTask.objects.exists()


def test_the_board_sees_the_restore_in_the_history(client, admin, board):
    HostTask.objects.create(kind="restore_backup", requested_by=admin, backup=f"local:{OLD}")
    client.force_login(board)
    page = client.get("/ops/tasks/").content.decode()
    assert "Restore from a backup (Nov 20, 2026 02:30 UTC, on the server)" in page


def test_a_restored_row_without_its_requester_still_shows(client, admin):
    HostTask.objects.create(
        kind="restore_backup", requested_by=None, backup=f"local:{OLD}", state="succeeded"
    )
    client.force_login(admin)
    assert "a login not in this data" in client.get("/ops/tasks/").content.decode()


# --- result emails --------------------------------------------------------------------------


def _finished(user, kind="backup", state="succeeded", summary="Backup 2026-11-26T1830Z, 5.3M"):
    start = timezone.now() - datetime.timedelta(seconds=95)
    return HostTask.objects.create(
        kind=kind,
        requested_by=user,
        state=state,
        started_at=start,
        finished_at=start + datetime.timedelta(seconds=95),
        summary=summary,
        output_tail="owner jane@example.com line\nHOA_OWNER_DB_PASSWORD=[hidden]",
    )


@pytest.mark.parametrize(
    "kind",
    [
        "backup",
        "restore_drill",
        "restore_drill_offsite",
        "offsite_lock_check",
        "harden_host",
        "health_check",
        "reseed_demo",
        "restart_app",
        "restore_backup",
    ],
)
@pytest.mark.parametrize("state", ["succeeded", "failed"])
def test_every_task_emails_its_result_once(admin, kind, state):
    requester = make_member(roles=[Role.SYSADMIN])
    task = _finished(requester, kind=kind, state=state)
    mail.outbox.clear()
    host_tasks.record_finished()
    to = {m.to[0] for m in mail.outbox}
    assert {requester.email, admin.email} <= to
    message = mail.outbox[0]
    assert host_tasks.BY_KIND[kind].label in message.subject
    assert ("succeeded" if state == "succeeded" else "failed") in message.subject
    assert "Took: 1 min 35 s" in message.body and "/ops/tasks/" in message.body
    assert "Result: Backup 2026-11-26T1830Z, 5.3M" in message.body
    # No log tail, no addresses from it.
    assert "jane@example.com" not in message.body and "PASSWORD" not in message.body
    task.refresh_from_db()
    assert task.notified_at is not None
    mail.outbox.clear()
    host_tasks.record_finished()
    assert mail.outbox == []


def test_a_restore_result_also_goes_to_the_board(admin, board):
    _finished(admin, kind="restore_backup", summary="Restored the backup of 2026-11-20T0230Z")
    mail.outbox.clear()
    host_tasks.record_finished()
    assert board.email in {m.to[0] for m in mail.outbox}


def test_the_summary_in_an_email_is_cleaned_again(admin):
    _finished(admin, state="failed", summary="Failed (exit 1): mail to x@example.com " + "A" * 40)
    mail.outbox.clear()
    host_tasks.record_finished()
    body = mail.outbox[0].body
    assert "x@example.com" not in body and "A" * 40 not in body and "[address]" in body


def test_a_mail_failure_does_not_fail_the_task_or_its_audit(admin):
    task = _finished(admin, state="failed")
    with mock.patch("core.task_mail.send_mail", side_effect=OSError("smtp down")):
        assert host_tasks.record_finished() == 1
    task.refresh_from_db()
    assert task.audited_at is not None and task.notified_at is not None
    assert AuditEvent.objects.filter(action="system.task_finished").count() == 1
    with mock.patch("core.task_mail.task_finished", side_effect=RuntimeError("boom")):
        _finished(admin)
        host_tasks.record_finished()


def test_an_install_update_emails_its_result(admin):
    now = timezone.now()
    UpdateStatus.objects.update_or_create(
        pk=1,
        defaults={
            "state": "failed",
            "state_changed_at": now,
            "run_started_at": now - datetime.timedelta(seconds=40),
            "requested_by": admin,
            "message": "See /var/log/hoa-portal-update.log on the VM",
        },
    )
    mail.outbox.clear()
    host_tasks.record_finished()
    assert len(mail.outbox) == 1
    assert "Install update" in mail.outbox[0].subject and "failed" in mail.outbox[0].subject
    assert "Took: 40 seconds" in mail.outbox[0].body
    mail.outbox.clear()
    host_tasks.record_finished()
    assert mail.outbox == []


def test_restore_point_labels():
    assert backup_points.label(f"offsite:{OLD}") == "Nov 20, 2026 02:30 UTC, off-site copy"
    assert backup_points.find("local:../x") is None
    assert task_mail.duration_text(None, None) == "unknown"
