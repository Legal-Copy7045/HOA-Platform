"""Demo mode (D97): on the POC with a seeded demo dataset, a board member or
sysadmin may view the portal as a demo test persona and switch back. Rules
are not relaxed: the persona's own permissions apply, the real user's MFA was
checked, real logins are never targets, sensitive pages are refused, and
everything is audited with the real user behind the persona."""

from __future__ import annotations

import pytest
from django.core.checks import run_checks
from django.test import RequestFactory
from django.urls import reverse

from audit.models import AuditEvent
from community import services
from community.models import Person, Role, SeatType, SysadminNomination
from community.tests.factories import LotFactory, PersonFactory, UserFactory, days_ago, make_member
from core import demo_mode
from core.models import DemoSeed
from core.policy import SYSTEM

pytestmark = pytest.mark.django_db

SWITCH, BACK = "/demo/switch/", "/demo/switch-back/"


@pytest.fixture
def poc(settings):
    settings.SEED_DEMO_ALLOWED = True
    settings.DEPLOYMENT_STAGE = "poc"
    return settings


def _persona(key, label, *, board=False, lots=(), roles=()):
    user = UserFactory(email=f"demo-{key}@example.com")
    user.set_unusable_password()
    user.save()
    person = PersonFactory(user=user, display_name=f"{key.title()} Persona (demo)")
    if board:
        services.record_board_seat(
            person=person, seat_type=SeatType.OWNER_ELECTED, term_start=days_ago(100), actor=SYSTEM
        )
    for lot in lots:
        services.record_ownership(person=person, lot=lot, start_date=days_ago(300), actor=SYSTEM)
    for role in roles:
        services.grant_role(person=person, role=role, start_date=days_ago(10), actor=SYSTEM)
    return user


@pytest.fixture
def w(poc):
    lot, other = LotFactory(), LotFactory()
    personas = {
        "president": _persona("president", "Board president", board=True),
        "owner": _persona("owner", "Owner (single)", lots=[lot]),
        "auditor": _persona("auditor", "Auditor (read-only)", roles=[Role.AUDITOR]),
    }
    for position, (key, user) in enumerate(personas.items()):
        demo_mode.mark_persona(user, key=key, label=f"{key} label", position=position)
    DemoSeed.objects.create(dataset="oak_creek")
    return {
        "lot": lot,
        "other": other,
        "board": make_member(board=True),
        "board2": make_member(board=True),
        "sysadmin": make_member(roles=[Role.SYSADMIN]),
        "owner": make_member(lots=[other]),
        "manager": make_member(roles=[Role.MANAGER]),
        **{f"p_{k}": v for k, v in personas.items()},
    }


def _switch(client, real, persona):
    client.force_login(real)
    return client.post(SWITCH, {"persona": persona.pk})


# --- Where it exists ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("allowed", "stage", "ok"),
    [
        (True, "poc", True),
        (True, "local", False),
        (True, "production", False),
        (True, "staging", False),
        (False, "poc", False),
    ],
)
def test_configured_only_on_the_poc_with_demo_data_allowed(settings, allowed, stage, ok):
    settings.SEED_DEMO_ALLOWED = allowed
    settings.DEPLOYMENT_STAGE = stage
    assert demo_mode.configured() is ok


@pytest.mark.parametrize(
    ("allowed", "stage"), [(True, "production"), (True, "local"), (False, "poc")]
)
def test_everything_is_absent_off_the_poc(client, w, settings, allowed, stage):
    settings.SEED_DEMO_ALLOWED = allowed
    settings.DEPLOYMENT_STAGE = stage
    client.force_login(w["board"])
    assert client.post(SWITCH, {"persona": w["p_owner"].pk}).status_code == 404
    assert client.post(BACK).status_code == 404
    page = client.get(reverse("home"))
    assert b"data-demo-banner" not in page.content
    assert b"Demo \xe2\x80\x94 you are viewing as" not in page.content
    assert not AuditEvent.objects.filter(action="demo.switched").exists()


def test_a_switch_made_on_the_poc_ends_when_the_gate_closes(client, w, settings):
    _switch(client, w["board"], w["p_owner"])
    settings.DEPLOYMENT_STAGE = "production"
    assert client.get(reverse("people")).status_code == 200  # the real board member again
    assert demo_mode.SESSION_KEY not in client.session


def test_absent_without_a_seeded_dataset(client, poc):
    board = make_member(board=True)
    client.force_login(board)
    assert client.post(SWITCH, {"persona": board.pk}).status_code == 404
    assert b"data-demo-banner" not in client.get(reverse("home")).content


@pytest.mark.parametrize("dataset", ["fresh_start", "blank"])
def test_unavailable_when_the_dataset_has_no_personas(client, poc, dataset):
    """A dataset without test personas (fresh_start, a blank one): no banner,
    and a switch is refused with a plain explanation."""
    DemoSeed.objects.create(dataset=dataset)
    board = make_member(board=True)
    client.force_login(board)
    assert b"data-demo-banner" not in client.get(reverse("home")).content
    response = client.post(SWITCH, {"persona": board.pk})
    assert response.status_code == 404
    assert "has no test personas" in response.content.decode()
    assert demo_mode.SESSION_KEY not in client.session
    assert not demo_mode.available()


def test_the_system_check_refuses_a_gate_open_off_the_poc(settings, monkeypatch):
    settings.DEPLOYMENT_STAGE = "production"
    settings.SEED_DEMO_ALLOWED = False
    assert not [m for m in run_checks() if m.id == "hoa.E003"]
    monkeypatch.setattr(demo_mode, "configured", lambda: True)  # a widened gate
    assert [m.id for m in run_checks() if m.id == "hoa.E003"] == ["hoa.E003"]


def test_the_system_check_wants_the_switch_after_the_mfa_check(settings):
    order = [m for m in settings.MIDDLEWARE if m != "core.demo_mode.DemoSwitchMiddleware"]
    index = order.index("accounts.middleware.RequireMFAMiddleware")
    settings.MIDDLEWARE = [*order[:index], "core.demo_mode.DemoSwitchMiddleware", *order[index:]]
    assert [m.id for m in run_checks() if m.id == "hoa.E004"] == ["hoa.E004"]


def test_the_real_settings_pass_the_checks():
    assert not [m for m in run_checks() if m.id in ("hoa.E003", "hoa.E004")]


# --- Banner ------------------------------------------------------------------------------


def test_banner_on_every_page_with_the_picker(client, w):
    client.force_login(w["board"])
    for name in ("home", "people", "my_items"):
        page = client.get(reverse(name))
        assert b"data-demo-banner" in page.content, name
    page = client.get(reverse("home")).content.decode()
    assert "Demo — you are viewing as: yourself (your own login)" in page
    for key in ("president", "owner", "auditor"):
        assert f'value="{w[f"p_{key}"].pk}"' in page
        assert f"{key} label" in page
    # Real logins are never in the picker.
    for real in ("board", "board2", "sysadmin", "owner"):
        assert f'value="{w[real].pk}"' not in page


def test_banner_names_the_persona_and_offers_back_to_me(client, w):
    _switch(client, w["board"], w["p_owner"])
    page = client.get(reverse("home")).content.decode()
    assert "Demo — you are viewing as: Owner Persona (demo) (owner label)" in page
    assert "Back to me" in page


def test_users_who_may_not_switch_see_the_banner_without_a_picker(client, w):
    client.force_login(w["owner"])
    page = client.get(reverse("home")).content.decode()
    assert "data-demo-banner" in page
    assert 'name="persona"' not in page


# --- Who may switch ----------------------------------------------------------------------


@pytest.mark.parametrize("who", ["board", "sysadmin"])
def test_board_and_sysadmin_switch_at_once_without_sign_out(client, w, who):
    response = _switch(client, w[who], w["p_owner"])
    assert response.status_code == 302
    session_user = client.session["_auth_user_id"]
    assert str(session_user) == str(w[who].pk)  # still the real login underneath
    # Now the owner persona: owner pages, no board pages.
    assert client.get(reverse("my_lot")).status_code == 200
    assert client.get(reverse("board_home")).status_code == 403
    event = AuditEvent.objects.get(action="demo.switched")
    assert event.actor_id == w[who].pk
    assert event.target_id == str(w["p_owner"].pk)
    assert event.metadata["persona"] == "owner"


@pytest.mark.parametrize("who", ["owner", "manager"])
def test_others_may_not_switch(client, w, who):
    assert _switch(client, w[who], w["p_owner"]).status_code == 403
    assert demo_mode.SESSION_KEY not in client.session
    assert AuditEvent.objects.get(action="demo.switch_refused").metadata == {
        "reason": "not_allowed"
    }


def test_a_persona_signed_in_for_real_may_not_switch(client, w):
    # Even a board persona (signed in through a setup link) cannot switch.
    persona = w["p_president"]
    from community.tests.factories import enroll_totp

    enroll_totp(persona)
    assert _switch(client, persona, w["p_owner"]).status_code == 403
    assert AuditEvent.objects.get(action="demo.switch_refused").metadata["reason"] == "persona"


@pytest.mark.parametrize("target", ["board2", "sysadmin", "owner", "board"])
def test_real_logins_are_never_targets(client, w, target):
    assert _switch(client, w["board"], w[target]).status_code == 404
    assert demo_mode.SESSION_KEY not in client.session
    assert client.get(reverse("people")).status_code == 200


def test_a_real_login_named_like_a_persona_is_not_a_target(client, w):
    real = make_member(email="demo-lookalike@example.com")
    Person.objects.unscoped("t").filter(user=real).update(display_name="Look Alike (demo)")
    assert _switch(client, w["board"], real).status_code == 404


def test_the_seed_never_marks_a_login_with_a_way_in(w):
    with pytest.raises(demo_mode.DemoModeError):
        demo_mode.mark_persona(w["board2"], key="x", label="x")  # has an authenticator
    user = UserFactory()
    user.set_password("a long test password 123")
    user.save()
    PersonFactory(user=user, display_name="Pass Word (demo)")
    with pytest.raises(demo_mode.DemoModeError):
        demo_mode.mark_persona(user, key="y", label="y")
    bare = UserFactory()
    bare.set_unusable_password()
    bare.save()
    PersonFactory(user=bare, display_name="Not A Demo Name")
    with pytest.raises(demo_mode.DemoModeError):
        demo_mode.mark_persona(bare, key="z", label="z")


def test_mark_persona_refuses_without_demo_data_allowed(settings):
    settings.SEED_DEMO_ALLOWED = False
    user = UserFactory()
    with pytest.raises(demo_mode.DemoModeError):
        demo_mode.mark_persona(user, key="k", label="k")


def test_a_persona_someone_has_set_up_is_no_longer_a_target(client, w):
    from community.tests.factories import enroll_totp

    enroll_totp(w["p_owner"])
    assert _switch(client, w["board"], w["p_owner"]).status_code == 404


def test_switching_needs_a_post_with_csrf(w):
    from django.test import Client

    client = Client(enforce_csrf_checks=True)
    client.force_login(w["board"])
    assert client.get(SWITCH).status_code == 405
    assert client.post(SWITCH, {"persona": w["p_owner"].pk}).status_code == 403
    assert demo_mode.SESSION_KEY not in client.session
    assert client.post(BACK).status_code == 403


def test_signed_out_visitors_go_to_sign_in(client, w):
    for url in (SWITCH, BACK):
        response = client.post(url, {"persona": w["p_owner"].pk})
        assert response.status_code == 302
        assert response["Location"].startswith(reverse("account_login"))


# --- While switched ----------------------------------------------------------------------


def test_a_persona_cannot_escalate(client, w):
    """The owner persona has only owner powers; switching again needs the
    real user's right, and a persona is never the actor of a switch."""
    _switch(client, w["board"], w["p_owner"])
    assert client.get(reverse("lot_detail", args=[w["other"].pk])).status_code in (403, 404)
    assert client.get(reverse("board_home")).status_code == 403
    # Switching on from here is the real board member's act, to a persona only.
    assert client.post(SWITCH, {"persona": w["board2"].pk}).status_code == 404
    assert client.post(SWITCH, {"persona": w["p_auditor"].pk}).status_code == 302
    event = AuditEvent.objects.filter(action="demo.switched").first()
    assert event.actor_id == w["board"].pk
    assert event.metadata["from_persona_user_id"] == w["p_owner"].pk


def test_the_real_user_losing_the_right_ends_the_switch(client, w):
    _switch(client, w["board"], w["p_owner"])
    from community.models import BoardSeat

    seat = BoardSeat.objects.unscoped("t").get(person__user=w["board"])
    services.end_board_seat(seat, reason="resigned", actor=SYSTEM)
    client.get(reverse("home"))
    assert demo_mode.SESSION_KEY not in client.session


def test_a_tampered_session_is_ignored(client, w):
    client.force_login(w["board"])
    session = client.session
    session[demo_mode.SESSION_KEY] = {"persona": w["board2"].pk, "real": w["board"].pk}
    session.save()
    assert client.get(reverse("people")).status_code == 200
    assert demo_mode.SESSION_KEY not in client.session
    session = client.session
    session[demo_mode.SESSION_KEY] = {"persona": w["p_owner"].pk, "real": w["board2"].pk}
    session.save()
    assert client.get(reverse("people")).status_code == 200  # not the owner persona
    assert demo_mode.SESSION_KEY not in client.session


def test_back_to_me(client, w):
    _switch(client, w["board"], w["p_owner"])
    response = client.post(BACK)
    assert response.status_code == 302
    assert client.get(reverse("people")).status_code == 200
    event = AuditEvent.objects.get(action="demo.switched_back")
    assert event.actor_id == w["board"].pk
    assert event.metadata == {"persona_user_id": w["p_owner"].pk}
    assert "demo_switch_by" not in event.metadata


@pytest.mark.parametrize(
    ("name", "kwargs"),
    [
        ("keys_connections", {}),
        ("keys_service", {"service": "smtp2go"}),
        ("system_restart", {}),
        ("server_tasks", {}),
        ("demo_data", {}),
        ("mfa_index", {}),
        ("account_change_password", {}),
        ("enroll_with_code", {}),
        ("owner_preview_start", {}),
        ("bank_connect", {}),
    ],
)
def test_sensitive_pages_are_refused_while_switched(client, w, name, kwargs):
    # Even as the sysadmin persona? We use the real sysadmin as the switcher.
    _switch(client, w["sysadmin"], w["p_president"])
    url = reverse(name, kwargs=kwargs)
    for response in (client.get(url), client.post(url, {})):
        assert response.status_code == 403
        assert "Choose “Back to me” first" in response.content.decode()
    event = AuditEvent.objects.filter(action="demo.page_refused").first()
    assert event.metadata["url_name"] == name
    assert event.metadata["demo_switch_by"] == w["sysadmin"].pk


def test_keys_and_connections_works_again_after_back_to_me(client, w):
    _switch(client, w["sysadmin"], w["p_owner"])
    assert client.get(reverse("keys_connections")).status_code == 403
    client.post(BACK)
    assert client.get(reverse("keys_connections")).status_code == 200


def test_actions_are_the_persona_s_and_record_the_real_user(client, w):
    demo_person = PersonFactory(display_name="No Login (demo)")
    _switch(client, w["board"], w["p_president"])
    client.post(reverse("sysadmin_nominate", args=[demo_person.pk]))
    nomination = SysadminNomination.objects.unscoped("t").get(person=demo_person)
    assert nomination.nominated_by_id == w["p_president"].pk
    event = (
        AuditEvent.objects.filter(actor=w["p_president"]).exclude(action="demo.switched").first()
    )
    assert event is not None
    assert event.metadata["demo_switch_by"] == w["board"].pk
    # Outside a switch nothing is added.
    client.post(BACK)
    services.nominate_sysadmin(person=PersonFactory(), actor=w["board2"])
    latest = AuditEvent.objects.filter(actor=w["board2"]).first()
    assert "demo_switch_by" not in latest.metadata


def test_callers_cannot_set_the_attribution_field():
    from audit.services import AuditError, record_event

    with pytest.raises(AuditError):
        record_event("demo.switched_back", metadata={"demo_switch_by": 1})


@pytest.mark.parametrize("target", ["board", "board2"])
def test_a_persona_never_changes_a_real_login(client, w, target):
    """One real person must not use two board personas to give a real login
    (their own or another's) powers or reset its sign-in."""
    real_person = Person.objects.unscoped("t").get(user=w[target])
    _switch(client, w["board"], w["p_president"])
    client.post(reverse("sysadmin_nominate", args=[real_person.pk]))
    assert not SysadminNomination.objects.unscoped("t").filter(person=real_person).exists()


def test_service_guards_refuse_real_logins_while_switched(w):
    from accounts import mfa_reset
    from accounts.models import VerificationMethod

    switch = demo_mode.Switch(w["board"].pk, w["p_president"].pk, "president", "x")
    token = demo_mode._current.set(switch)
    try:
        real_person = Person.objects.unscoped("t").get(user=w["board2"])
        with pytest.raises(services.CommunityError):
            services.grant_role(person=real_person, role=Role.AUDITOR, actor=w["p_president"])
        with pytest.raises(mfa_reset.MfaResetError):
            mfa_reset.start_reset(
                subject=w["board2"],
                verification_method=VerificationMethod.values[0],
                actor=w["p_president"],
            )
        # A demo person is fine.
        mfa_reset.start_reset(
            subject=w["p_owner"],
            verification_method=VerificationMethod.values[0],
            actor=w["p_president"],
        )
    finally:
        demo_mode._current.reset(token)
    # Without a switch the guard does nothing.
    demo_mode.refuse_real_target(user_id=w["board2"].pk)


def test_reauthentication_while_switched_is_the_real_user_s(client, w):
    """Votes (D24) ask for a recent password or second factor: while switched
    it is the real user's own; the persona never has one."""
    from allauth.account.adapter import get_adapter
    from allauth.account.internal.flows.login import record_authentication
    from allauth.core import context

    request = RequestFactory().get("/")
    from django.contrib.sessions.backends.db import SessionStore

    request.session = SessionStore()
    request.user = w["p_owner"]
    request.demo_real_user = w["board"]
    request.demo_switch = demo_mode.Switch(w["board"].pk, w["p_owner"].pk, "owner", "x")
    with context.request_context(request):
        assert get_adapter().is_reauthentication_required(app="") is True
        record_authentication(request, w["board"], method="password", reauthenticated=True)
        assert get_adapter().is_reauthentication_required(app="") is False
    assert request.user == w["p_owner"]


def test_a_reauthentication_prompt_while_switched_goes_to_the_real_user(w):
    from allauth.core.exceptions import ReauthenticationRequired

    request = RequestFactory().get("/")
    request.user = w["p_owner"]
    request.demo_real_user = w["board"]
    middleware = demo_mode.DemoSwitchMiddleware(lambda r: None)
    assert middleware.process_exception(request, ReauthenticationRequired()) is None
    assert request.user == w["board"]


def test_the_persona_has_no_mfa_but_the_real_user_s_was_checked(client, w):
    """The real user without a second factor never gets past the MFA check,
    so a stored switch does nothing for them."""
    from allauth.mfa.models import Authenticator

    _switch(client, w["board"], w["p_owner"])
    Authenticator.objects.filter(user=w["board"]).delete()
    response = client.get(reverse("my_lot"))
    assert response.status_code == 302
    assert response["Location"] == reverse("account_setup_expired")  # signed out
    assert "_auth_user_id" not in client.session


def test_the_persona_pages_render_with_the_sidebar(client, w):
    _switch(client, w["board"], w["p_owner"])
    page = client.get(reverse("home")).content.decode()
    assert 'id="sidebar"' in page  # not the sign-in setup steps


def test_cross_role_views_while_switched(client, w):
    _switch(client, w["board"], w["p_auditor"])
    assert client.get(reverse("board_home")).status_code in (200, 403)
    assert client.get(reverse("my_lot")).status_code in (302, 403, 404)
    client.post(SWITCH, {"persona": w["p_president"].pk})
    assert client.get(reverse("people")).status_code == 200
    client.post(SWITCH, {"persona": w["p_owner"].pk})
    assert client.get(reverse("board_home")).status_code == 403
