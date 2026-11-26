"""keep_logins export/import around a demo reseed (decision D53): the real
logins come back with the same ids, password hashes, MFA authenticators,
person and sysadmin role; demo logins never leave; audited."""

from __future__ import annotations

import copy
import io
import json

import pytest
from allauth.account.models import EmailAddress
from allauth.mfa.models import Authenticator
from django.core.management import CommandError, call_command

from accounts import keep_logins
from accounts.models import User
from audit.models import AuditEvent
from community import services
from community.models import Person, Role, RoleAssignment
from community.tests.factories import make_member
from core.models import Heartbeat, HostTask
from core.policy import SYSTEM, is_sysadmin
from core.scoping import system_access

pytestmark = pytest.mark.django_db

SHIFT = 100_000


@pytest.fixture
def world(settings):
    settings.SEED_DEMO_ALLOWED = True
    admin = make_member(roles=[Role.SYSADMIN], email="real.admin@portal-test.invalid")
    admin.set_password("a long test passphrase")
    admin.save()
    Authenticator.objects.create(
        user=admin, type="webauthn", data={"name": "Laptop", "credential": {"id": "abc"}}
    )
    Authenticator.objects.create(user=admin, type="recovery_codes", data={"seed": "s", "used": 0})
    EmailAddress.objects.create(user=admin, email=admin.email, verified=True, primary=True)
    owner = make_member(email="real.owner@portal-test.invalid")  # a non-demo login too
    demo_person = services.record_person(
        display_name="Avery Applewood (demo)", email="demo001@example.com", actor=SYSTEM
    )
    demo_user = User.objects.create_user("demo-board0@example.com")
    services.link_login(demo_person, demo_user, actor=SYSTEM)
    task = HostTask.objects.create(kind="reseed_demo", dataset="fresh_start", requested_by=admin)
    HostTask.objects.filter(pk=task.pk).update(state="running")
    HostTask.objects.create(kind="backup", requested_by=demo_user, state="succeeded")
    Heartbeat.objects.create(name="backup_local", at=task.requested_at)
    return {"admin": admin, "owner": owner, "demo": demo_user, "task": task}


def _moved(data: dict) -> dict:
    """The same export, as if into a fresh database: ids and emails that are
    free here, and the old running task gone (a real reseed empties the
    database first)."""
    HostTask.objects.filter(state="running").update(state="failed")
    data = copy.deepcopy(data)
    for user in data["users"]:
        user["id"] += SHIFT
        user["email"] = "moved." + user["email"]
        for e in user["email_addresses"]:
            e["email"] = "moved." + e["email"]
        if user["person"]:
            user["person"]["display_name"] += " (kept)"
    for task in data["tasks"]:
        task["id"] += SHIFT
        task["requested_by_id"] += SHIFT
    return data


def test_export_keeps_real_logins_only(world):
    data = keep_logins.export_data()
    emails = [u["email"] for u in data["users"]]
    assert world["admin"].email in emails and world["owner"].email in emails
    assert "demo-board0@example.com" not in emails
    admin = next(u for u in data["users"] if u["email"] == world["admin"].email)
    assert admin["id"] == world["admin"].pk
    assert {a["type"] for a in admin["authenticators"]} == {"totp", "webauthn", "recovery_codes"}
    assert admin["roles"] == [
        {"role": "sysadmin", "start_date": admin["roles"][0]["start_date"], "citation": ""}
    ]
    owner = next(u for u in data["users"] if u["email"] == world["owner"].email)
    assert owner["roles"] == []
    # Only tasks the kept logins started; the backup heartbeat.
    assert [t["id"] for t in data["tasks"]] == [world["task"].pk]
    assert [h["name"] for h in data["heartbeats"]] == ["backup_local"]


def test_ended_sysadmin_roles_are_not_carried(world):
    from community.models import today

    with system_access("t"):
        RoleAssignment.objects.filter(person__user=world["admin"]).update(end_date=today())
    admin = next(u for u in keep_logins.export_data()["users"] if u["id"] == world["admin"].pk)
    assert admin["roles"] == []


def test_round_trip_restores_login_mfa_person_role_and_task(world):
    data = _moved(keep_logins.export_data())
    logins, tasks = keep_logins.import_data(data)
    assert (logins, tasks) == (2, 1)
    admin_id = world["admin"].pk + SHIFT
    user = User.objects.get(pk=admin_id)  # same id: passkeys name their login by id
    assert user.email == "moved." + world["admin"].email
    assert user.check_password("a long test passphrase")
    old = {a.type: a.data for a in Authenticator.objects.filter(user=world["admin"]).order_by("pk")}
    new = {a.type: a.data for a in Authenticator.objects.filter(user=user)}
    assert new == old
    assert EmailAddress.objects.get(user=user).verified
    with system_access("t"):
        person = Person.objects.get(user=user)
        assert person.display_name.endswith("(kept)")
        roles = RoleAssignment.objects.filter(person=person).active()
        assert list(roles.values_list("role", flat=True)) == ["sysadmin"]
    assert is_sysadmin(user)
    task = HostTask.objects.get(pk=world["task"].pk + SHIFT)
    assert (task.kind, task.dataset, task.state, task.requested_by_id) == (
        "reseed_demo",
        "fresh_start",
        "running",
        admin_id,
    )
    # New rows continue after the kept ids.
    later = User.objects.create_user("later@portal-test.invalid")
    assert later.pk > admin_id
    event = AuditEvent.objects.get(action="system.logins_kept")
    assert event.metadata == {"count": 2, "tasks": 1}


def test_import_refuses_ids_or_emails_already_taken(world):
    data = keep_logins.export_data()
    with pytest.raises(keep_logins.KeepLoginsError, match="already exists"):
        keep_logins.import_data(data)
    with pytest.raises(keep_logins.KeepLoginsError, match="this version"):
        keep_logins.import_data({"format": 99})


def test_commands_round_trip_through_stdout_and_stdin(world, capsys, monkeypatch):
    call_command("keep_logins", "export", "--out", "-")
    text = capsys.readouterr().out
    data = json.loads(text)
    assert "demo-board0@example.com" not in text
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_moved(data))))
    out = io.StringIO()
    call_command("keep_logins", "import", "--in", "-", stdout=out)
    assert "Kept 2 real login(s) and 1 server task record(s)." in out.getvalue()


def test_commands_refuse_unless_demo_allowed(world, settings):
    settings.SEED_DEMO_ALLOWED = False
    with pytest.raises(CommandError, match="SEED_DEMO_ALLOWED"):
        call_command("keep_logins", "export", "--out", "-")
    with pytest.raises(keep_logins.KeepLoginsError):
        keep_logins.import_data({"format": 1, "users": []})
