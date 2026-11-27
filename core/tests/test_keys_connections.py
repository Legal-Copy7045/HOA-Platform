"""Keys and connections (D93): third-party service keys entered in the
browser. Sysadmins only, a fresh second factor, tested before saving,
encrypted at rest under a dedicated key-encryption key, write-only, read
through one accessor without a restart, audited by name, the board told, and
Stripe and Teller changes approved by a second person with the provider
account pinned."""

from __future__ import annotations

import datetime
import logging
import sys
import time
from unittest import mock

import psycopg
import pytest
from django.core import mail
from django.db import connection
from django.urls import reverse
from django.utils import timezone

from audit.models import AuditEvent
from community.models import Role
from community.tests.factories import LotFactory, make_member
from core import credential_checks, credentials, fresh_mfa
from core.append_only import AppendOnlyError
from core.credential_checks import TestResult as Result
from core.models import CredentialChange, CredentialDecision, CredentialValue

pytestmark = pytest.mark.django_db

SMTP2GO_KEY = "api-TESTONLYabcdefghijklmnop1234"  # fictional, never a real key
STRIPE_KEY = "rk_" + "test_TESTONLYfictional000111222"
STRIPE_KEY_2 = "rk_" + "test_TESTONLYfictional333444555"
SENDER = "Oak Creek Test <portal@example.org>"
CHECK = {"check": "keys-confirmed"}


# --- helpers ------------------------------------------------------------------------------


@pytest.fixture
def admin():
    return make_member(roles=[Role.SYSADMIN])


@pytest.fixture
def board():
    return make_member(board=True)


@pytest.fixture
def board2():
    return make_member(board=True)


def _mfa(client, user, *, kind="totp", age=0, method="mfa"):
    client.force_login(user)
    session = client.session
    session["account_authentication_methods"] = [
        {"method": method, "type": kind, "at": time.time() - age, "reauthenticated": True}
    ]
    session.save()


def _passes(account_ref=""):
    return lambda: Result(True, "ok", "Fake provider accepted it.", account_ref)


def _fails():
    return lambda: Result(False, "auth", "Fake provider refused it.")


@pytest.fixture
def fake_tests(monkeypatch):
    """No network: every provider test passes (Stripe and Teller give an account)."""
    fakes = {k: _passes() for k in credential_checks.TESTERS}
    fakes["stripe"] = _passes("acct_TESTONLYaaaa1111")
    fakes["teller"] = _passes("app_TESTONLY1")
    monkeypatch.setattr(credential_checks, "TESTERS", fakes)
    return fakes


def _save_smtp2go(client, *, extra=None, action="save"):
    data = {
        **CHECK,
        "action": action,
        "SMTP2GO_API_KEY": SMTP2GO_KEY,
        "SMTP2GO_REGION": "global",
        "DEFAULT_FROM_EMAIL": SENDER,
        "confirm": "on",
        **(extra or {}),
    }
    return client.post(reverse("keys_service", args=["smtp2go"]), data)


def _stripe(actor, key=STRIPE_KEY, account="acct_TESTONLYaaaa1111", **kw):
    test = Result(True, "ok", "ok", account)
    return credentials.save(actor, "stripe", {"STRIPE_SECRET_KEY": key}, test=test, **kw)


# --- addresses and authorization ----------------------------------------------------------


def test_urls_sit_under_system_status():
    assert reverse("keys_connections") == "/ops/status/keys/"
    assert reverse("keys_service", args=["stripe"]) == "/ops/status/keys/stripe/"
    assert reverse("keys_change", args=[3]) == "/ops/status/keys/change/3/"


ROLES = {
    "owner": lambda: make_member(lots=[LotFactory()]),
    "manager": lambda: make_member(roles=[Role.MANAGER]),
    "board": lambda: make_member(board=True),
    "auditor": lambda: make_member(roles=[Role.AUDITOR]),
    "arc": lambda: make_member(roles=[Role.ARC_REVIEWER]),
    "treasurer": lambda: make_member(board=True, lots=[LotFactory()]),
}


@pytest.mark.parametrize("who", sorted(ROLES))
def test_only_sysadmins_reach_the_pages_or_save(client, who, fake_tests):
    user = ROLES[who]()
    _mfa(client, user)  # even with a fresh second factor: the role decides first
    assert client.get(reverse("keys_connections")).status_code == 403
    for service in credentials.BY_KEY:
        url = reverse("keys_service", args=[service])
        assert client.get(url).status_code == 403
        assert client.post(url, {**CHECK, "action": "save"}).status_code == 403
    assert _save_smtp2go(client).status_code == 403
    assert not CredentialChange.objects.exists()
    with pytest.raises(Exception, match=r"system\.manage_keys"):
        credentials.save(user, "smtp2go", {"SMTP2GO_API_KEY": SMTP2GO_KEY})


def test_signed_out_visitors_go_to_sign_in(client):
    for url in (
        reverse("keys_connections"),
        reverse("keys_service", args=["smtp2go"]),
        reverse("keys_change", args=[1]),
    ):
        response = client.get(url)
        assert response.status_code == 302 and "/accounts/login/" in response["Location"]


def test_unknown_service_is_404(client, admin):
    _mfa(client, admin)
    assert client.get("/ops/status/keys/nothing/").status_code == 404


def test_a_change_is_seen_by_sysadmins_and_the_board_only(client, admin, board, fake_tests):
    change = _stripe(admin)
    url = reverse("keys_change", args=[change.pk])
    for who in ("owner", "manager", "auditor", "arc"):
        _mfa(client, ROLES[who]())
        assert client.get(url).status_code == 403, who
        assert client.post(url, {**CHECK, "decision": "approve"}).status_code == 403, who
    for user in (admin, board):
        _mfa(client, user)
        assert client.get(url).status_code == 200
    _mfa(client, board)
    assert client.get(reverse("keys_change", args=[change.pk + 999])).status_code == 404
    assert not CredentialDecision.objects.exists()


# --- a fresh second factor ----------------------------------------------------------------


def test_without_a_second_factor_the_page_asks_for_one(client, admin):
    client.force_login(admin)
    url = reverse("keys_service", args=["smtp2go"])
    for response in (client.get(url), _save_smtp2go(client)):
        assert response.status_code == 302
        assert response["Location"].startswith(reverse("mfa_reauthenticate"))
    assert not CredentialChange.objects.exists()


@pytest.mark.parametrize(
    ("method", "kind"),
    [("password", ""), ("code", ""), ("mfa", "recovery_codes")],
)
def test_a_password_an_emailed_code_or_a_recovery_code_is_not_enough(
    client, admin, method, kind, fake_tests
):
    _mfa(client, admin, method=method, kind=kind)
    response = _save_smtp2go(client)
    assert response.status_code == 302 and "reauthenticate" in response["Location"]
    assert not CredentialChange.objects.exists()


def test_the_check_lasts_five_minutes_then_asks_again(client, admin, fake_tests):
    _mfa(client, admin, age=fresh_mfa.WINDOW_SECONDS + 5)
    response = _save_smtp2go(client)
    assert response.status_code == 302 and "reauthenticate" in response["Location"]
    assert not CredentialChange.objects.exists()
    _mfa(client, admin, age=60)
    assert _save_smtp2go(client).status_code == 302
    assert CredentialChange.objects.count() == 1


def test_a_login_with_a_passkey_must_use_it(client, admin, fake_tests):
    from allauth.mfa.models import Authenticator

    Authenticator.objects.create(user=admin, type=Authenticator.Type.WEBAUTHN, data={})
    _mfa(client, admin, kind="totp")  # the app code is no longer enough
    response = client.get(reverse("keys_service", args=["smtp2go"]))
    assert response["Location"].startswith(reverse("mfa_reauthenticate_webauthn"))
    _mfa(client, admin, kind="webauthn")
    assert client.get(reverse("keys_service", args=["smtp2go"])).status_code == 200
    page = client.get(reverse("keys_connections")).content.decode()
    assert "Add a passkey" not in page


def test_a_login_without_a_passkey_is_asked_to_add_one(client, admin):
    _mfa(client, admin)
    assert "Add a passkey" in client.get(reverse("keys_connections")).content.decode()


def test_the_last_four_are_shown_only_after_a_fresh_check(client, admin, fake_tests):
    _mfa(client, admin)
    _save_smtp2go(client)
    assert SMTP2GO_KEY[-4:] in client.get(reverse("keys_connections")).content.decode()
    _mfa(client, admin, age=fresh_mfa.WINDOW_SECONDS + 1)
    page = client.get(reverse("keys_connections")).content.decode()
    assert f"ends in {SMTP2GO_KEY[-4:]}" not in page
    assert "Confirm it's you to show them" in page


def test_board_approval_needs_the_fresh_second_factor_too(client, admin, board, fake_tests):
    change = _stripe(admin)
    client.force_login(board)
    response = client.post(
        reverse("keys_change", args=[change.pk]), {**CHECK, "decision": "approve"}
    )
    assert response.status_code == 302 and "reauthenticate" in response["Location"]
    _mfa(client, board, method="password")
    client.post(reverse("keys_change", args=[change.pk]), {**CHECK, "decision": "approve"})
    assert not CredentialDecision.objects.exists()
    _mfa(client, board)
    client.post(reverse("keys_change", args=[change.pk]), {**CHECK, "decision": "approve"})
    assert CredentialDecision.objects.filter(by=board, decision="approve").count() == 1


# --- test before saving -------------------------------------------------------------------


def test_save_runs_the_test_and_records_it(client, admin, fake_tests):
    _mfa(client, admin)
    assert _save_smtp2go(client).status_code == 302
    change = CredentialChange.objects.get()
    assert change.test_ok is True and change.service == "smtp2go"
    assert credentials.get("SMTP2GO_API_KEY") == SMTP2GO_KEY
    assert credentials.get("EMAIL_PROVIDER") == "smtp2go"
    tested = AuditEvent.objects.get(action="system.credential_tested")
    assert tested.metadata == {"service": "smtp2go", "ok": True, "code": "ok"}


def test_a_failed_test_refuses_the_save(client, admin, fake_tests):
    fake_tests["smtp2go"] = _fails()
    _mfa(client, admin)
    page = _save_smtp2go(client).content.decode()
    assert "Test failed" in page and "Fake provider refused it." in page
    assert not CredentialChange.objects.exists()


def test_save_anyway_is_recorded_and_told(
    client, admin, board, fake_tests, django_capture_on_commit_callbacks
):
    fake_tests["smtp2go"] = _fails()
    _mfa(client, admin)
    with django_capture_on_commit_callbacks(execute=True):
        _save_smtp2go(client, extra={"save_anyway": "on"})
    change = CredentialChange.objects.get()
    assert change.saved_anyway and change.test_ok is False and change.test_code == "auth"
    event = AuditEvent.objects.get(action="system.credential_saved")
    assert event.metadata["saved_anyway"] is True and event.metadata["test_ok"] is False
    assert any("saved anyway" in m.body for m in mail.outbox)


def test_test_only_saves_nothing(client, admin, fake_tests):
    _mfa(client, admin)
    page = _save_smtp2go(client, action="test").content.decode()
    assert "Test passed" in page
    assert not CredentialChange.objects.exists()


def test_a_format_problem_is_refused_before_any_call(client, admin, fake_tests):
    called = []
    fake_tests["smtp2go"] = lambda: called.append(1) or Result(True, "ok", "")
    _mfa(client, admin)
    _save_smtp2go(client, extra={"SMTP2GO_API_KEY": "not-a-key"})
    assert not called and not CredentialChange.objects.exists()


def test_a_full_stripe_secret_key_is_refused(client, admin, fake_tests):
    _mfa(client, admin)
    data = {
        **CHECK,
        "action": "save",
        "STRIPE_SECRET_KEY": "sk_" + "test_TESTONLY0000000000",  # nosec B105 - fictional test value
        "confirm": "on",
    }
    page = client.post(reverse("keys_service", args=["stripe"]), data).content.decode()
    assert "full secret key" in page
    assert not CredentialChange.objects.exists()


def test_the_confirmation_is_required(client, admin, fake_tests):
    _mfa(client, admin)
    _save_smtp2go(client, extra={"confirm": ""})
    assert not CredentialChange.objects.exists()


def test_saves_are_rate_limited(admin, fake_tests):
    for _ in range(credentials.SAVES_PER_HOUR):
        credentials.save(admin, "ai", {"GEMINI_API_KEY": "TESTONLY-gemini-key-000"})
    with pytest.raises(credentials.CredentialError, match="Too many"):
        credentials.save(admin, "ai", {"GEMINI_API_KEY": "TESTONLY-gemini-key-001"})
    assert AuditEvent.objects.filter(
        action="system.credential_refused", metadata__reason="rate_limited"
    ).exists()


def test_tests_are_rate_limited(admin):
    from django.core.cache import cache

    cache.delete(f"credentials:tests:{admin.pk}")
    results = [credentials.note_test(admin) for _ in range(credentials.TESTS_PER_HOUR + 1)]
    assert all(results[:-1]) and results[-1] is False


# --- write-only ---------------------------------------------------------------------------


def test_a_value_is_never_in_a_page_a_log_an_audit_row_or_an_email(
    client, admin, board, fake_tests, caplog, django_capture_on_commit_callbacks
):
    caplog.set_level(logging.DEBUG)
    _mfa(client, admin)
    with django_capture_on_commit_callbacks(execute=True):
        _save_smtp2go(client)
        _stripe(admin)
    secrets = (SMTP2GO_KEY, STRIPE_KEY)
    pages = [
        reverse("keys_connections"),
        reverse("system_status"),
        *[reverse("keys_service", args=[k]) for k in credentials.BY_KEY],
        *[reverse("keys_change", args=[c.pk]) for c in CredentialChange.objects.all()],
    ]
    for url in pages:
        body = client.get(url).content.decode()
        for secret in secrets:
            assert secret not in body, url
    _mfa(client, board)
    for key in ("server", "payments", "notices", "arc", "vendors"):
        body = client.get(reverse("setup_step", args=[key])).content.decode()
        assert all(s not in body for s in secrets), key
    body = client.get(reverse("keys_change", args=[CredentialChange.objects.first().pk]))
    assert all(s not in body.content.decode() for s in secrets)
    for record in caplog.records:
        text = record.getMessage() + repr(vars(record))
        assert all(s not in text for s in secrets)
    for event in AuditEvent.objects.all():
        assert all(s not in repr(event.metadata) for s in secrets)
    assert mail.outbox
    for message in mail.outbox:
        assert all(s not in message.body + message.subject for s in secrets)
    for value in CredentialValue.objects.all():
        assert all(s not in repr(value) + value.shown for s in secrets)


def test_a_secret_input_never_comes_back_after_a_test(client, admin, fake_tests):
    fake_tests["smtp2go"] = _fails()
    _mfa(client, admin)
    page = _save_smtp2go(client).content.decode()
    assert SMTP2GO_KEY not in page
    assert SENDER.split("<")[1].rstrip(">") in page  # a plain setting is kept


def test_an_error_report_carries_no_value(rf, admin, monkeypatch, fake_tests):
    from django.contrib.messages.storage.fallback import FallbackStorage
    from django.contrib.sessions.middleware import SessionMiddleware
    from django.views.debug import ExceptionReporter

    from core.keys_views import keys_service

    def boom():
        raise RuntimeError("storage failed")

    monkeypatch.setattr(credentials, "key_id", boom)
    request = rf.post(
        reverse("keys_service", args=["smtp2go"]),
        {
            **CHECK,
            "action": "save",
            "SMTP2GO_API_KEY": SMTP2GO_KEY,
            "SMTP2GO_REGION": "global",
            "confirm": "on",
        },
    )
    SessionMiddleware(lambda r: None).process_request(request)
    request.session["account_authentication_methods"] = [
        {"method": "mfa", "type": "totp", "at": time.time()}
    ]
    request._messages = FallbackStorage(request)
    request.user = admin
    with pytest.raises(RuntimeError):
        keys_service(request, service="smtp2go")
    report = ExceptionReporter(request, *sys.exc_info(), is_email=True)
    assert SMTP2GO_KEY not in report.get_traceback_text()
    assert SMTP2GO_KEY not in report.get_traceback_html()


# --- encryption at rest -------------------------------------------------------------------


def test_the_database_holds_ciphertext_only(admin):
    credentials.save(admin, "smtp2go", {"SMTP2GO_API_KEY": SMTP2GO_KEY})
    with connection.cursor() as cursor:
        cursor.execute("SELECT ciphertext, last4, shown, key_id FROM core_credentialvalue")
        rows = cursor.fetchall()
    for ciphertext, _last4, _shown, key_id in rows:
        assert SMTP2GO_KEY.encode() not in bytes(ciphertext)
        assert bytes(ciphertext).startswith(credentials.FORMAT)
        assert key_id and len(key_id) == 16
    secret = CredentialValue.objects.get(name="SMTP2GO_API_KEY")
    assert secret.last4 == SMTP2GO_KEY[-4:] and secret.shown == ""
    assert CredentialValue.objects.get(name="EMAIL_PROVIDER").shown == "smtp2go"


def test_the_key_is_derived_from_its_own_kek_not_the_django_secret(settings):
    settings.CREDENTIALS_KEK = settings.SECRET_KEY = "x" * 40
    one = credentials._data_key()
    assert one != settings.SECRET_KEY.encode()[:32]
    settings.SECRET_KEY = "y" * 40
    assert credentials._data_key() == one  # the Django secret plays no part


def test_a_wrong_kek_fails_closed(admin, settings, caplog):
    settings.SMTP2GO_API_KEY = "api-TESTONLYserverenvvalue0000"
    credentials.save(admin, "smtp2go", {"SMTP2GO_API_KEY": SMTP2GO_KEY})
    settings.CREDENTIALS_KEK = "another-test-only-kek-of-forty-characters"
    credentials.forget()
    # Neither the saved value nor the older .env value: empty.
    assert credentials.get("SMTP2GO_API_KEY") == ""
    assert any(r.getMessage() == "credentials.unreadable" for r in caplog.records)
    info = credentials.overview(credentials.BY_KEY["smtp2go"])
    assert info["unreadable"]
    from comms import smtp2go

    assert not smtp2go.config().configured


def test_without_a_kek_nothing_is_saved_or_read(admin, settings):
    credentials.save(admin, "ai", {"GEMINI_API_KEY": "TESTONLY-gemini-key-000"})
    settings.CREDENTIALS_KEK = ""
    credentials.forget()
    assert credentials.get("GEMINI_API_KEY") == ""
    with pytest.raises(credentials.CredentialError, match="CREDENTIALS_KEK"):
        credentials.save(admin, "ai", {"GEMINI_API_KEY": "TESTONLY-gemini-key-001"})


def test_a_sealed_value_cannot_be_moved_to_another_setting(admin):
    change = credentials.save(admin, "smtp2go", {"SMTP2GO_API_KEY": SMTP2GO_KEY})
    blob = CredentialValue.objects.get(change=change, name="SMTP2GO_API_KEY").ciphertext
    with pytest.raises(credentials.Unreadable):
        credentials.unseal(blob, "STRIPE_SECRET_KEY", change.pk)
    with pytest.raises(credentials.Unreadable):
        credentials.unseal(blob, "SMTP2GO_API_KEY", change.pk + 1)


# --- the accessor -------------------------------------------------------------------------


def test_the_portal_value_wins_over_env_and_removing_it_brings_env_back(admin, settings):
    settings.GEMINI_API_KEY = "TESTONLY-env-gemini"
    assert credentials.get("GEMINI_API_KEY") == "TESTONLY-env-gemini"
    credentials.save(admin, "ai", {"GEMINI_API_KEY": "TESTONLY-portal-gemini"})
    assert credentials.get("GEMINI_API_KEY") == "TESTONLY-portal-gemini"
    from core import ai

    assert ai.api_key() == "TESTONLY-portal-gemini"
    credentials.clear(admin, "ai")
    assert credentials.get("GEMINI_API_KEY") == "TESTONLY-env-gemini"
    assert credentials.get("GEMINI_MODEL") == settings.GEMINI_MODEL


def test_typed_values_converted_and_derived(admin, settings):
    settings.EMAIL_USE_TLS, settings.EMAIL_USE_SSL = True, False
    assert credentials.get("EMAIL_USE_TLS") is True
    credentials.save(
        admin,
        "smtp",
        {
            "EMAIL_HOST": "smtp.example.org",
            "EMAIL_PORT": "465",
            "EMAIL_SECURITY": "ssl",
            "EMAIL_HOST_PASSWORD": "TESTONLY-smtp-password",  # nosec B105 - fictional test value
        },
    )
    assert credentials.get("EMAIL_PORT") == 465
    assert credentials.get("EMAIL_USE_SSL") is True and credentials.get("EMAIL_USE_TLS") is False
    from core.email import smtp_options

    assert smtp_options()["host"] == "smtp.example.org"


def test_only_known_settings_go_through_the_accessor():
    with pytest.raises(KeyError):
        credentials.get("DJANGO_SECRET_KEY")


def test_changes_take_effect_without_a_restart(admin, settings, monkeypatch):
    """Another process (the worker) sees a save within the cache time; this
    process at once."""
    from comms import smtp2go
    from core import email

    settings.CREDENTIALS_CACHE_SECONDS = 15
    assert email.transport_backend() == settings.EMAIL_TRANSPORT_BACKEND
    stale = credentials.state()  # what the worker read a moment ago
    credentials.save(
        admin, "smtp2go", {"SMTP2GO_API_KEY": SMTP2GO_KEY, "DEFAULT_FROM_EMAIL": SENDER}
    )
    # This process forgot its copy on the save:
    assert email.transport_backend() == "comms.smtp2go.Smtp2goBackend"
    assert smtp2go.config().key == SMTP2GO_KEY
    assert email.sender(None) == SENDER
    # The worker still has its old copy until the cache time is over ...
    real = time.monotonic
    credentials._cache[0], credentials._cache[1] = real() + 15, stale
    assert credentials.get("EMAIL_PROVIDER") == settings.EMAIL_PROVIDER
    monkeypatch.setattr(credentials.time, "monotonic", lambda: real() + 16)
    # ... then reads the database again: no restart.
    assert credentials.get("EMAIL_PROVIDER") == "smtp2go"


def test_the_saved_sender_is_used_for_default_mail(admin, settings):
    from django.core.mail import get_connection, send_mail

    credentials.save(
        admin, "smtp", {"EMAIL_HOST": "smtp.example.org", "DEFAULT_FROM_EMAIL": SENDER}
    )
    # The portal's own backend (pytest-django swaps EMAIL_BACKEND for locmem).
    connection_ = get_connection("core.email.RedirectingBackend")
    send_mail("Subject", "Body", None, ["someone@example.com"], connection=connection_)
    assert mail.outbox[-1].from_email == SENDER


def test_the_bounce_poller_and_usps_read_through_the_accessor(admin):
    from comms import bounces, usps_iv

    credentials.save(
        admin,
        "bounce",
        {
            "BOUNCE_IMAP_HOST": "imap.example.org",
            "BOUNCE_IMAP_USER": "bounces",
            "BOUNCE_IMAP_PASSWORD": "TESTONLY-imap",  # nosec B105 - fictional test value
        },
    )
    cfg = bounces.config()
    assert cfg.configured and cfg.password == "TESTONLY-imap"  # nosec B105 - fictional test value
    credentials.save(
        admin,
        "usps",
        {
            "USPS_IV_ENVIRONMENT": "cat",
            "USPS_IV_USERNAME": "bcg-user",
            "USPS_IV_PASSWORD": "TESTONLY-usps",  # nosec B105 - fictional test value
        },
    )
    assert usps_iv.config().configured


# --- Stripe and Teller: a second person and a pinned account ------------------------------


def test_a_stripe_key_waits_for_another_board_member(
    admin, board, settings, django_capture_on_commit_callbacks
):
    settings.PAYMENTS_PROVIDER, settings.STRIPE_SECRET_KEY = "none", ""
    with django_capture_on_commit_callbacks(execute=True):
        change = _stripe(admin)
    assert change.approvals_needed == 1
    assert credentials.change_state(change).state == credentials.PENDING
    assert credentials.get("STRIPE_SECRET_KEY") == ""  # not in use yet
    assert any("Approval needed" in m.subject for m in mail.outbox)
    mail.outbox.clear()
    credentials.decide(board, change, "approve")
    assert credentials.change_state(change).state == credentials.ACTIVE
    assert credentials.get("STRIPE_SECRET_KEY") == STRIPE_KEY
    from payments import providers

    assert providers.config().available
    assert credentials.pin("stripe") == "acct_TESTONLYaaaa1111"
    assert any("approved" in m.subject for m in mail.outbox)
    assert AuditEvent.objects.filter(
        action="system.credential_decided", metadata__decision="approve"
    ).exists()


def test_nobody_approves_their_own_change(admin):
    both = make_member(board=True, roles=[Role.SYSADMIN])
    change = _stripe(both)
    with pytest.raises(credentials.CredentialError, match="another board member"):
        credentials.decide(both, change, "approve")
    assert not CredentialDecision.objects.exists()
    assert AuditEvent.objects.filter(action="policy.own_matter_refused").exists()
    with pytest.raises(Exception, match=r"system\.approve_keys"):
        credentials.decide(admin, _stripe(admin), "approve")  # a sysadmin alone is not the board


def test_the_page_shows_the_own_change_note(client):
    both = make_member(board=True, roles=[Role.SYSADMIN])
    change = _stripe(both)
    _mfa(client, both)
    page = client.get(reverse("keys_change", args=[change.pk])).content.decode()
    assert "another board member has to decide" in page
    assert 'value="approve"' not in page


def test_an_unapproved_change_lapses_after_48_hours(admin, board):
    change = _stripe(admin)
    later = timezone.now() + datetime.timedelta(hours=credentials.APPROVAL_HOURS, minutes=1)
    with mock.patch.object(credentials.timezone, "now", return_value=later):
        credentials.forget()
        assert credentials.change_state(change).state == credentials.EXPIRED
        with pytest.raises(credentials.CredentialError, match="not waiting"):
            credentials.decide(board, change, "approve")
        assert credentials.get("STRIPE_SECRET_KEY") != STRIPE_KEY


def test_a_refused_or_cancelled_change_never_takes_effect(admin, board, board2):
    change = _stripe(admin)
    credentials.decide(board, change, "reject")
    with pytest.raises(credentials.CredentialError):
        credentials.decide(board2, change, "approve")
    assert credentials.get("STRIPE_SECRET_KEY") != STRIPE_KEY
    other = _stripe(admin, key=STRIPE_KEY_2)
    with pytest.raises(credentials.CredentialError, match="Only the person"):
        credentials.decide(make_member(roles=[Role.SYSADMIN]), other, "cancel")
    credentials.decide(admin, other, "cancel")
    assert credentials.change_state(other).state == credentials.CANCELLED


def test_a_newer_change_supersedes_one_still_waiting(admin, board):
    first = _stripe(admin)
    _stripe(admin, key=STRIPE_KEY_2)
    assert credentials.change_state(first).state == credentials.SUPERSEDED
    with pytest.raises(credentials.CredentialError):
        credentials.decide(board, first, "approve")


def test_a_key_for_another_stripe_account_is_refused(admin, board):
    credentials.decide(board, _stripe(admin), "approve")
    with pytest.raises(credentials.CredentialError, match="different stripe account"):
        _stripe(admin, key=STRIPE_KEY_2, account="acct_TESTONLYbbbb2222")
    assert AuditEvent.objects.filter(
        action="system.credential_refused", metadata__reason="other_account"
    ).exists()
    # The same account with a new key is fine (and still needs approval).
    assert _stripe(admin, key=STRIPE_KEY_2).approvals_needed == 1


def test_moving_the_pin_needs_two_board_members_and_72_hours(
    admin, board, board2, django_capture_on_commit_callbacks
):
    credentials.decide(board, _stripe(admin), "approve")
    with django_capture_on_commit_callbacks(execute=True):
        move = _stripe(admin, key=STRIPE_KEY_2, account="acct_TESTONLYbbbb2222", pin_change=True)
    assert move.approvals_needed == 2 and move.wait_hours == 72
    assert any("pinned stripe account" in m.subject for m in mail.outbox)
    credentials.decide(board, move, "approve")
    assert credentials.change_state(move).state == credentials.PENDING
    with pytest.raises(credentials.CredentialError):
        credentials.decide(board, move, "approve")  # one person counts once
    credentials.decide(board2, move, "approve")
    assert credentials.change_state(move).state == credentials.WAITING
    assert credentials.pin("stripe") == "acct_TESTONLYaaaa1111"
    assert credentials.get("STRIPE_SECRET_KEY") == STRIPE_KEY
    assert any("takes effect" in m.body for m in mail.outbox)
    later = timezone.now() + datetime.timedelta(hours=73)
    with mock.patch.object(credentials.timezone, "now", return_value=later):
        credentials.forget()
        assert credentials.change_state(move).state == credentials.ACTIVE
        assert credentials.pin("stripe") == "acct_TESTONLYbbbb2222"
        assert credentials.get("STRIPE_SECRET_KEY") == STRIPE_KEY_2


def test_the_requester_does_not_count_towards_a_pin_move(board):
    both = make_member(board=True, roles=[Role.SYSADMIN])
    credentials.decide(board, _stripe(both), "approve")
    move = _stripe(both, key=STRIPE_KEY_2, account="acct_TESTONLYbbbb2222", pin_change=True)
    with pytest.raises(credentials.CredentialError):
        credentials.decide(both, move, "approve")


def test_teller_pins_its_application(admin, board):
    test = Result(True, "ok", "ok", "app_TESTONLY1")
    values = {"TELLER_APPLICATION_ID": "app_TESTONLY1", "TELLER_ENVIRONMENT": "sandbox"}
    change = credentials.save(admin, "teller", values, test=test)
    assert change.approvals_needed == 1
    credentials.decide(board, change, "approve")
    from ledger.bankfeeds import teller

    assert teller.application_id() == "app_TESTONLY1" and teller.configured()
    other = Result(True, "ok", "ok", "app_TESTONLY2")
    with pytest.raises(credentials.CredentialError, match="different teller application"):
        credentials.save(admin, "teller", {"TELLER_APPLICATION_ID": "app_TESTONLY2"}, test=other)


# --- the vendor document key --------------------------------------------------------------


def test_the_vendor_key_is_made_and_rotated_in_the_portal(
    admin, settings, django_capture_on_commit_callbacks
):
    from operations import vendor_crypto

    settings.VENDOR_DOCUMENT_KEY, settings.VENDOR_DOCUMENT_KEY_FALLBACKS = "", []
    credentials.make_vendor_key(admin)
    assert vendor_crypto.configured()
    blob = vendor_crypto.encrypt(b"fictional W-9", "file-1")
    with pytest.raises(credentials.CredentialError):
        credentials.make_vendor_key(admin)  # already set: rotate instead
    with (
        mock.patch("core.tasks.reencrypt_vendor_documents.defer") as defer,
        django_capture_on_commit_callbacks(execute=True),
    ):
        credentials.make_vendor_key(admin, rotate=True)
    defer.assert_called_once()
    assert vendor_crypto.key_index(blob, "file-1") == 1  # the old key is a fallback
    assert vendor_crypto.decrypt(blob, "file-1") == b"fictional W-9"
    assert AuditEvent.objects.filter(action="system.vendor_key_made", metadata__rotated=True)


def test_vendor_key_page_makes_it_with_a_confirmation(client, admin, settings):
    settings.VENDOR_DOCUMENT_KEY = ""
    _mfa(client, admin)
    url = reverse("keys_service", args=["vendor_key"])
    client.post(url, {**CHECK, "action": "make"})
    assert not CredentialChange.objects.exists()
    client.post(url, {**CHECK, "action": "make", "confirm": "on"})
    assert CredentialChange.objects.filter(service="vendor_key").count() == 1
    assert credentials.get("VENDOR_DOCUMENT_KEY") not in client.get(url).content.decode()


# --- append-only --------------------------------------------------------------------------


def test_changes_values_and_decisions_are_append_only(admin, board, app_role_connection):
    change = _stripe(admin)
    credentials.decide(board, change, "approve")
    value = CredentialValue.objects.first()
    with pytest.raises(AppendOnlyError):
        change.save()
    with pytest.raises(AppendOnlyError):
        value.delete()
    with pytest.raises(AppendOnlyError):
        CredentialDecision.objects.all().delete()
    for table in ("core_credentialchange", "core_credentialvalue", "core_credentialdecision"):
        with app_role_connection.cursor() as cursor, pytest.raises(psycopg.Error):
            cursor.execute(f"DELETE FROM {table}")  # nosec B608 - fixed table names


# --- the provider tests themselves (no network) -------------------------------------------


def test_the_stripe_test_reads_the_account(monkeypatch):
    from payments import stripe_api

    answers = {
        "/v1/account": (200, b'{"id": "acct_TESTONLYcccc3333"}'),
        "/v1/balance": (200, b'{"livemode": false}'),
    }

    def fake(method, url, data, headers):
        assert headers["Authorization"] == f"Bearer {STRIPE_KEY}"
        return answers[url.split("api.stripe.com", 1)[1].split("?")[0]]

    monkeypatch.setattr(stripe_api, "transport", fake)
    result = credential_checks.run("stripe", {"STRIPE_SECRET_KEY": STRIPE_KEY})
    assert result.ok and result.account_ref == "acct_TESTONLYcccc3333"
    answers["/v1/account"] = (401, b"{}")
    assert credential_checks.run("stripe", {"STRIPE_SECRET_KEY": STRIPE_KEY}).code == "auth"


def test_the_smtp2go_test_uses_the_typed_key(monkeypatch):
    from comms import smtp2go

    seen = {}

    def fake(url, data, headers, timeout):
        seen["key"] = headers[smtp2go.KEY_HEADER]
        return 200, b'{"data": {}}'

    monkeypatch.setattr(smtp2go, "transport", fake)
    result = credential_checks.run(
        "smtp2go", {"SMTP2GO_API_KEY": SMTP2GO_KEY, "SMTP2GO_REGION": "eu"}
    )
    assert result.ok and seen["key"] == SMTP2GO_KEY
    assert credentials.get("SMTP2GO_API_KEY") != SMTP2GO_KEY  # nothing kept


def test_the_teller_test_needs_the_certificate_accepted(monkeypatch):
    statuses = iter([401, 403])
    monkeypatch.setattr(credential_checks, "teller_transport", lambda url, ctx: next(statuses))
    values = {"TELLER_APPLICATION_ID": "app_TESTONLY1", "TELLER_ENVIRONMENT": "sandbox"}
    first = credential_checks.run("teller", values)
    assert first.ok and first.account_ref == "app_TESTONLY1"
    assert credential_checks.run("teller", values).code == "certificate"


def test_the_smtp_test_signs_in_without_sending(monkeypatch):
    opened = {}

    class FakeBackend:
        def __init__(self, **kw):
            opened.update(kw)

        def open(self):
            opened["open"] = True

        def close(self):
            opened["closed"] = True

    monkeypatch.setattr("django.core.mail.backends.smtp.EmailBackend", FakeBackend)
    result = credential_checks.run(
        "smtp",
        {
            "EMAIL_HOST": "smtp.example.org",
            "EMAIL_PORT": "587",
            "EMAIL_SECURITY": "tls",
            "EMAIL_HOST_PASSWORD": "TESTONLY-pw",  # nosec B105 - fictional test value
            "EMAIL_HOST_USER": "u",
        },
    )
    assert result.ok and opened["open"] and opened["closed"] and opened["use_tls"] is True
    assert opened["password"] == "TESTONLY-pw" and not mail.outbox


def test_a_crashing_test_never_shows_its_message(monkeypatch):
    def crash():
        raise ValueError(SMTP2GO_KEY)

    monkeypatch.setitem(credential_checks.TESTERS, "smtp2go", crash)
    result = credential_checks.run("smtp2go", {"SMTP2GO_API_KEY": SMTP2GO_KEY})
    assert not result.ok and SMTP2GO_KEY not in result.words


# --- the page tells what stays on the server ----------------------------------------------


def test_the_page_lists_server_only_settings_and_what_to_do(client, admin, settings):
    settings.BANK_FEED_TOKEN_KEY = ""  # nosec B105 - fictional test value
    _mfa(client, admin)
    page = client.get(reverse("keys_connections")).content.decode()
    assert "Set on the server at install" in page
    for label in ("Django&#x27;s secret key", "Cloudflare tunnel token", "Off-site backup bucket"):
        assert label in page
    assert "Created on the server automatically by start-portal.sh" in page


def test_the_page_warns_when_the_kek_is_missing(client, admin, settings):
    settings.CREDENTIALS_KEK = ""
    _mfa(client, admin)
    assert (
        "Nothing can be saved here yet" in client.get(reverse("keys_connections")).content.decode()
    )


def test_system_status_links_to_the_page(client, admin):
    client.force_login(admin)
    assert reverse("keys_connections") in client.get(reverse("system_status")).content.decode()
