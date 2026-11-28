"""Review fixes of 0.45.0 for demo mode (D97) and the restore (D98).

EXP-001 / BUG-001 / SEC-001 / BUG-002 / EXP-002: while a board member views
the POC as the demo *sysadmin* persona, nothing on the operations side is
reachable: every page under /ops/ is refused, every sysadmin-only or system.*
permission answers no (deny by default, so a page added later anywhere is
covered too), the fresh second factor is never fresh, and the server-task,
restore and update services refuse themselves. SEC-002 / CLM-013: personas
never grant powers to a person record the seed did not make, nor approve the
real user's own proposal. BUG-003: a restore's row keeps its requester's id."""

from __future__ import annotations

import datetime
import re
import time

import pytest
from django.core.exceptions import PermissionDenied
from django.test import RequestFactory
from django.urls import get_resolver, resolve, reverse

from accounts import services as account_services
from audit.models import AuditEvent
from community import services
from community.models import Person, Role, RoleAssignment, SeatType, SysadminNomination
from community.tests.factories import PersonFactory, UserFactory, days_ago, make_member
from core import demo_mode, fresh_mfa, host_tasks, policy
from core.models import DemoSeed, HostTask, UpdateStatus
from core.policy import SYSTEM
from core.scoping import system_access

pytestmark = pytest.mark.django_db

SWITCH = "/demo/switch/"


@pytest.fixture
def w(settings):
    settings.SEED_DEMO_ALLOWED = True
    settings.DEPLOYMENT_STAGE = "poc"

    def persona(key, *, board=False, roles=()):
        user = UserFactory(email=f"demo-{key}@example.com")
        user.set_unusable_password()
        user.save()
        person = PersonFactory(user=user, display_name=f"{key.title()} Persona (demo)")
        if board:
            services.record_board_seat(
                person=person,
                seat_type=SeatType.OWNER_ELECTED,
                term_start=days_ago(100),
                actor=SYSTEM,
            )
        for role in roles:
            with system_access("test setup"):
                RoleAssignment.objects.create(person=person, role=role, start_date=days_ago(10))
        demo_mode.mark_persona(user, key=key, label=f"{key} label")
        return user

    sysadmin = persona("sysadmin", roles=[Role.SYSADMIN])
    president = persona("president", board=True)
    treasurer = persona("treasurer", board=True)
    seeded = PersonFactory(display_name="Seeded Neighbor (demo)", email="")
    DemoSeed.objects.create(dataset="oak_creek")
    return {
        "board": make_member(board=True),
        "board2": make_member(board=True),
        "real_sysadmin": make_member(roles=[Role.SYSADMIN]),
        "p_sysadmin": sysadmin,
        "p_president": president,
        "p_treasurer": treasurer,
        "seeded": seeded,
    }


def _fresh(client):
    """The real user used their authenticator just now (in this session)."""
    session = client.session
    session[fresh_mfa.SESSION_KEY] = [{"method": "mfa", "type": "totp", "at": time.time()}]
    session.save()


def _switch(client, real, persona):
    client.force_login(real)
    assert client.post(SWITCH, {"persona": persona.pk}).status_code == 302
    _fresh(client)


class _Switched:
    """Run service code as if inside a switched request."""

    def __init__(self, real, persona):
        self.switch = demo_mode.Switch(real.pk, persona.pk, "p", "p")

    def __enter__(self):
        self.token = demo_mode._current.set(self.switch)
        return self.switch

    def __exit__(self, *exc):
        demo_mode._current.reset(self.token)


# --- EXP-001 / BUG-001: Restore from a backup ---------------------------------------------


def test_restore_is_refused_as_the_sysadmin_persona_even_with_the_real_users_fresh_factor(
    client, w
):
    _switch(client, w["board"], w["p_sysadmin"])
    url = reverse("backup_restore")
    for response in (
        client.get(url),
        client.post(url, {"backup": "local:2026-11-20T0230Z", "confirmation": "RESTORE"}),
    ):
        assert response.status_code == 403
        assert "Choose “Back to me” first" in response.content.decode()
    assert not HostTask.objects.exists()
    event = AuditEvent.objects.filter(action="demo.page_refused").first()
    assert event.metadata["url_name"] == "backup_restore"


def test_the_restore_and_task_services_refuse_a_switched_request(w):
    request = RequestFactory().post("/")
    request.user = w["p_sysadmin"]
    request.session = {}
    with _Switched(w["board"], w["p_sysadmin"]):
        for kind in (HostTask.Kind.RESTORE_BACKUP, HostTask.Kind.BACKUP):
            with pytest.raises(host_tasks.HostTaskRefused, match="demo persona"):
                host_tasks.request_task(w["p_sysadmin"], kind, request=request)
        with pytest.raises(host_tasks.HostTaskRefused, match="demo persona"):
            host_tasks.check_restore(request, "local:2026-11-20T0230Z", "RESTORE")
    # The request attribute alone is enough as well.
    request.demo_switch = demo_mode.Switch(w["board"].pk, w["p_sysadmin"].pk, "p", "p")
    with pytest.raises(host_tasks.HostTaskRefused):
        host_tasks.check_restore(request, "local:2026-11-20T0230Z", "RESTORE")
    assert not HostTask.objects.exists()


def test_no_fresh_second_factor_while_switched(w):
    request = RequestFactory().get("/")
    request.user = w["p_sysadmin"]
    request.session = {
        fresh_mfa.SESSION_KEY: [{"method": "mfa", "type": "totp", "at": time.time()}]
    }
    assert fresh_mfa.is_fresh(request)  # not switched: the session's factor counts
    request.demo_switch = demo_mode.Switch(w["board"].pk, w["p_sysadmin"].pk, "p", "p")
    assert not fresh_mfa.is_fresh(request)
    view = fresh_mfa.required(lambda r: "ran")
    with pytest.raises(PermissionDenied):
        view(request)


# --- BUG-002 / EXP-002: System status actions ---------------------------------------------


@pytest.mark.parametrize(
    "action",
    [
        "install_update",
        "schedule_update",
        "cancel_schedule",
        "check_updates",
        "test_email",
        "clear_check",
        "scan_waiting",
    ],
)
def test_system_status_actions_are_refused_while_switched(client, w, action):
    UpdateStatus.get()
    before = UpdateStatus.objects.values().get(pk=1)
    _switch(client, w["board"], w["p_sysadmin"])
    response = client.post(reverse("system_status"), {"action": action})
    assert response.status_code == 403
    assert UpdateStatus.objects.values().get(pk=1) == before


def test_the_update_action_refuses_a_switched_request_itself(w):
    from core import views

    request = RequestFactory().post("/", {"action": "install_update"})
    request.user = w["p_sysadmin"]
    request.demo_switch = demo_mode.Switch(w["board"].pk, w["p_sysadmin"].pk, "p", "p")
    with pytest.raises(PermissionDenied):
        views._update_action(request, "install_update")


# --- Deny by default: the policy and every URL --------------------------------------------


def test_operations_permissions_answer_no_while_switched(w):
    persona = w["p_sysadmin"]
    refused = sorted(a for a in policy.PERMISSIONS if policy.refused_while_switched(a))
    assert {"system.run_task", "system.view_status", "system.manage_keys"} <= set(refused)
    assert "system.view_tasks" in refused and "system.approve_keys" in refused
    assert "demo.switch" not in refused  # board and sysadmin: checked on the real user
    assert policy.can(persona, "system.run_task")
    with _Switched(w["board"], persona):
        for action in refused:
            assert not policy.can(persona, action), action
        with pytest.raises(PermissionDenied):
            policy.require(persona, "system.run_task")
        assert policy.can(SYSTEM, "system.run_task")  # the worker is never switched
    assert policy.can(persona, "system.run_task")


SAMPLES = ("1", "x", "2026-11", "2026", "a1b2", "file.pdf", "t" * 32)


def _path_for(name: str) -> str | None:
    """A path that resolves to ``name``, with sample values for its arguments."""
    for possibilities, pattern, _defaults, _converters in get_resolver().reverse_dict.getlist(name):
        for fmt, params in possibilities:
            values = {}
            for p in params:
                group = re.search(rf"\(\?P<{p}>((?:[^()]|\([^()]*\))*)\)", pattern)
                regex = group.group(1) if group else "[^/]+"
                first_word = re.match(r"\(?\??:?([A-Za-z_]+)", regex)
                candidates = ([first_word.group(1)] if first_word else []) + list(SAMPLES)
                values[p] = next((c for c in candidates if re.fullmatch(regex, c)), "1")
            path = "/" + fmt % values
            try:
                if resolve(path).url_name == name:
                    return path
            except Exception:  # noqa: S112 - try the next form  # nosec B112
                continue
    return None


# Not pages to walk: signing out ends the session the walk runs in.
WALK_SKIP = {"account_logout", "demo_switch_back"}


def _all_paths() -> dict[str, str]:
    out = {}
    for name in get_resolver().reverse_dict:
        if isinstance(name, str) and name not in WALK_SKIP:
            path = _path_for(name)
            if path is not None:
                out[name] = path
    return out


def test_every_operations_url_is_refused_while_switched(client, w, monkeypatch):
    """Walk every URL in the project. First as the real sysadmin (not
    switched) to find each page that checks an operations permission; then
    switched to the sysadmin persona: every /ops/ page and every page found is
    refused, and no operations permission is ever granted while switched, so
    a future page cannot slip through whatever its address or name."""
    paths = _all_paths()
    assert len(paths) > 300  # the walk really covers the project
    checked: list[tuple[str, bool]] = []
    real_can = policy.can

    watched = {w["real_sysadmin"].pk, w["p_sysadmin"].pk}

    def spy(user, action, on=None):
        result = real_can(user, action, on)
        # The real sysadmin's walk, then the persona's; the real board member's
        # own pages while switched (switching, re-authentication) are theirs.
        if getattr(user, "pk", None) in watched and policy.refused_while_switched(action):
            checked.append((action, result))
        return result

    monkeypatch.setattr(policy, "can", spy)

    client.force_login(w["real_sysadmin"])
    _fresh(client)
    needs_ops: set[str] = set()
    for name, path in paths.items():
        checked.clear()
        client.get(path)
        if any(ok for _action, ok in checked):
            needs_ops.add(name)
    assert {"system_status", "server_tasks", "backup_restore", "keys_connections"} <= needs_ops

    client.force_login(w["board"])
    assert client.post(SWITCH, {"persona": w["p_sysadmin"].pk}).status_code == 302
    _fresh(client)
    client.raise_request_exception = False
    leaks = []
    for name, path in sorted(paths.items()):
        for method in ("get", "post"):
            checked.clear()
            response = getattr(client, method)(path, {})
            if any(ok for _action, ok in checked):
                leaks.append(f"{method} {path}: an operations permission was granted")
            must_refuse = path.startswith("/ops/") or name in needs_ops
            if must_refuse and response.status_code != 403:
                leaks.append(f"{method} {path} ({name}): {response.status_code}, not 403")
        # Still switched (no page above ended the switch).
        assert demo_mode.SESSION_KEY in client.session, name
    assert not leaks, "\n".join(leaks)
    assert not HostTask.objects.exists()


def test_every_ops_path_is_refused_by_the_middleware_rule():
    factory = RequestFactory()
    for path in ["/ops/status/", "/ops/tasks/restore/", "/ops/new-page-added-later/"]:
        assert demo_mode.is_refused(factory.get(path), None) or demo_mode.is_refused(
            factory.get(path), "anything"
        )
    assert not demo_mode.is_refused(factory.get("/demo/switch-back/"), "demo_switch_back")


# --- SEC-002 / CLM-013: only demo data, never one's own proposal ---------------------------


def test_a_person_added_after_the_seed_gets_no_powers_from_personas(w):
    """A real board member records "Alex" (no login yet, their own alias
    address), even named like demo data, and tries to have personas give
    them sysadmin or a board seat, or invite them."""
    for name in ("Alex Example", "Alex Example (demo)"):
        alex = PersonFactory(display_name=name)
        with _Switched(w["board"], w["p_president"]):
            with pytest.raises(services.CommunityError, match="not part of the demo data"):
                services.nominate_sysadmin(person=alex, actor=w["p_president"])
            with pytest.raises(services.CommunityError):
                services.grant_role(person=alex, role=Role.AUDITOR, actor=w["p_president"])
            with pytest.raises(services.CommunityError):
                services.record_board_seat(
                    person=alex,
                    seat_type=SeatType.OWNER_ELECTED,
                    term_start=days_ago(1),
                    actor=w["p_president"],
                )
            with pytest.raises(account_services.InvitationError):
                account_services.create_invitation(
                    person=alex, actor=w["p_president"], send_email=False
                )
        assert not SysadminNomination.objects.unscoped("t").filter(person=alex).exists()


def test_a_persona_never_approves_the_real_users_own_nomination(w):
    alex = PersonFactory(display_name="Alex Example")
    nomination = services.nominate_sysadmin(person=alex, actor=w["board"])
    with _Switched(w["board"], w["p_treasurer"]), pytest.raises(services.CommunityError):
        services.approve_sysadmin(nomination, actor=w["p_treasurer"])
    # Another real board member, not switched, approves as usual.
    services.approve_sysadmin(nomination, actor=w["board2"])


def test_a_persona_never_approves_the_real_users_own_seat_proposal(w):
    from community.models import BoardSeatNomination

    person = w["seeded"]
    with _Switched(w["board"], w["p_president"]):
        nomination = BoardSeatNomination.objects.unscoped("t").create(
            person=person,
            seat_type=SeatType.OWNER_ELECTED,
            term_start=days_ago(1),
            nominated_by=w["board"],
        )
        with pytest.raises(services.CommunityError, match="you made this proposal"):
            services.decide_seat_nomination(nomination, approve=True, actor=w["p_president"])


def test_demo_people_the_seed_made_still_work_while_switched(w):
    with _Switched(w["board"], w["p_president"]):
        nomination = services.nominate_sysadmin(person=w["seeded"], actor=w["p_president"])
        services.approve_sysadmin(nomination, actor=w["p_treasurer"])
    assert Person.objects.unscoped("t").get(pk=w["seeded"].pk)


# --- BUG-003: the restore's requester survives in the restored data -------------------------


def test_the_requesters_id_is_kept_and_audited_without_the_login(client, w):
    requester = w["real_sysadmin"]
    task = HostTask.objects.create(
        kind=HostTask.Kind.RESTORE_BACKUP,
        requested_by=None,  # the login is not in the restored data
        requested_by_user_id=987654,
        backup="local:2026-11-20T0230Z",
        state=HostTask.State.SUCCEEDED,
        finished_at=datetime.datetime.now(datetime.UTC),
    )
    host_tasks.record_finished()
    event = AuditEvent.objects.get(action="system.task_finished")
    assert event.metadata["requested_by_user_id"] == 987654
    client.force_login(requester)
    page = client.get(reverse("server_tasks")).content.decode()
    assert "a login not in this data (login #987654)" in page
    assert task.pk


def test_a_request_records_the_requesters_id(w, settings, monkeypatch):
    monkeypatch.setattr(host_tasks, "offered", lambda: host_tasks.TASKS)
    task = host_tasks.request_task(w["real_sysadmin"], HostTask.Kind.BACKUP)
    assert task.requested_by_user_id == w["real_sysadmin"].pk
