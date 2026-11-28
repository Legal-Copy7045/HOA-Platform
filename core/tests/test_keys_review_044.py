"""Keys and connections: the review fixes of 0.44.0 (D93, "Review fixes").

Email (SMTP2Go, SMTP) and Gemini changes wait for a board member who did not
make them; every board notice about a key change goes through the settings in
effect before it, and recent changes show on System status and in the
board's My items (EXP-001 / SEC-004). A saved secret is never sent to a new
host, port or user (SEC-001), and the connection tests reach public mail
hosts only (EXP-004). A change can be refused or cancelled until it takes
effect (BUG-001). Removals: Stripe and Teller need the board and a server
fallback for the pinned account (BUG-002 / SEC-002); a removal clears only
its own service's values (BUG-005); the vendor key is never removed while
documents depend on it, and every earlier key stays a fallback (BUG-003 /
EXP-003). The pin is checked when a key takes effect (BUG-006). The server
scripts check their own .env values and can clear the portal's (EXP-006).
Error reports hide CREDENTIALS_KEK (EXP-007)."""

from __future__ import annotations

import contextlib
import datetime
import io
import time
from unittest import mock

import pytest
from allauth.mfa.models import Authenticator
from django.core import mail
from django.core.management import CommandError, call_command
from django.urls import reverse
from django.utils import timezone

from audit.models import AuditEvent
from community.models import Role
from community.tests.factories import make_member
from core import credential_checks, credentials, my_items
from core.credential_checks import TestResult as Result
from core.models import CredentialChange, CredentialDecision

pytestmark = pytest.mark.django_db

SMTP2GO_KEY = "api-TESTONLYabcdefghijklmnop1234"  # fictional, never a real key
STRIPE_KEY = "rk_" + "test_TESTONLYfictional000111222"
STRIPE_KEY_2 = "rk_" + "test_TESTONLYfictional333444555"
SENDER = "Oak Creek Test <portal@example.org>"
PASSWORD = "TESTONLY-smtp-password"  # nosec B105 - fictional test value
CHECK = {"check": "keys-confirmed"}
ACCT_A, ACCT_B = "acct_TESTONLYaaaa1111", "acct_TESTONLYbbbb2222"


def _with_mfa(user):
    Authenticator.objects.get_or_create(
        user=user, type=Authenticator.Type.TOTP, defaults={"data": {}}
    )
    return user


@pytest.fixture
def admin():
    return make_member(roles=[Role.SYSADMIN])


@pytest.fixture
def board():
    return _with_mfa(make_member(board=True))


@pytest.fixture
def board2():
    return _with_mfa(make_member(board=True))


def _login(client, user):
    client.force_login(user)
    session = client.session
    session["account_authentication_methods"] = [
        {"method": "mfa", "type": "totp", "at": time.time(), "reauthenticated": True}
    ]
    session.save()


def _stripe(actor, key=STRIPE_KEY, account=ACCT_A, **kw):
    return credentials.save(
        actor, "stripe", {"STRIPE_SECRET_KEY": key}, test=Result(True, "ok", "ok", account), **kw
    )


def _smtp2go(actor, **extra):
    values = {"SMTP2GO_API_KEY": SMTP2GO_KEY, "SMTP2GO_REGION": "global", **extra}
    return credentials.save(actor, "smtp2go", values, test=Result(True, "ok", "ok"))


@pytest.fixture
def relay(monkeypatch):
    """A fake SMTP2Go: records what would go through the new relay."""
    from comms import smtp2go

    calls = []

    def fake(url, data, headers, timeout):
        calls.append(url)
        return 200, b'{"data": {"succeeded": 1, "failed": 0, "email_id": "1abc-000001-zz"}}'

    monkeypatch.setattr(smtp2go, "transport", fake)
    return calls


# --- EXP-001 / SEC-004: email changes wait for the board; notices via the old relay -----


def test_an_email_change_waits_for_another_board_member(
    admin, board, relay, django_capture_on_commit_callbacks
):
    with django_capture_on_commit_callbacks(execute=True):
        change = _smtp2go(admin)
    assert change.approvals_needed == 1 and change.expires_at is not None
    assert credentials.change_state(change).state == credentials.PENDING
    assert credentials.get("EMAIL_PROVIDER") != "smtp2go"  # not in use yet
    assert any("Approval needed" in m.subject for m in mail.outbox)
    assert not relay  # the request went through the settings in use
    mail.outbox.clear()
    with django_capture_on_commit_callbacks(execute=True):
        credentials.decide(board, change, "approve")
    assert credentials.get("EMAIL_PROVIDER") == "smtp2go"
    # The notice that it took effect still went through the relay before it.
    assert any("approved" in m.subject for m in mail.outbox) and not relay


def test_nobody_approves_their_own_email_change(board):
    both = _with_mfa(make_member(board=True, roles=[Role.SYSADMIN]))
    change = _smtp2go(both)
    assert change.approvals_needed == 1  # ``board`` could approve it
    with pytest.raises(credentials.CredentialError, match="another board member"):
        credentials.decide(both, change, "approve")


def test_the_first_officer_sets_up_email_alone_and_the_board_is_told_through_the_old_relay(
    admin, relay, django_capture_on_commit_callbacks
):
    """Nobody else could approve (no other board member with a second factor):
    in use at once, as the install needs; its notice went the old way."""
    seated = make_member(board=True)  # seated, not signed up with a second factor yet
    Authenticator.objects.filter(user=seated).delete()
    with django_capture_on_commit_callbacks(execute=True):
        change = _smtp2go(admin)
    assert change.approvals_needed == 0
    assert credentials.get("EMAIL_PROVIDER") == "smtp2go"
    notice = [m for m in mail.outbox if "Key changed" in m.subject]
    assert notice and "no other board member" in notice[0].body
    assert not relay  # not through the relay just saved


def test_gemini_changes_wait_for_the_board_too(admin, board):
    change = credentials.save(admin, "ai", {"GEMINI_API_KEY": "TESTONLY-gemini-key-000"})
    assert change.approvals_needed == 1
    assert credentials.get("GEMINI_API_KEY") != "TESTONLY-gemini-key-000"


def test_open_changes_show_on_system_status_and_in_the_boards_my_items(
    client, admin, board, board2
):
    change = _smtp2go(admin)
    _login(client, admin)
    page = client.get(reverse("system_status")).content.decode()
    assert reverse("keys_change", args=[change.pk]) in page
    assert "Waiting for board approval" in page
    items = [i for i in my_items.items_for(board) if i.area == "Keys and connections"]
    assert [i.url for i in items] == [reverse("keys_change", args=[change.pk])]
    credentials.decide(board2, change, "reject")
    assert not [i for i in my_items.items_for(board) if i.area == "Keys and connections"]
    # Never for people who are not on the board.
    assert not credentials.board_items(admin)
    assert not credentials.board_items(make_member(roles=[Role.MANAGER]))


# --- SEC-001: a saved secret is never sent to a new endpoint ------------------------------


def test_a_new_smtp_host_with_the_saved_password_left_blank_is_refused_before_connecting(
    admin, monkeypatch
):
    credentials.save(
        admin,
        "smtp",
        {"EMAIL_HOST": "smtp.example.org", "EMAIL_HOST_USER": "u", "EMAIL_HOST_PASSWORD": PASSWORD},
    )
    opened = []
    monkeypatch.setattr(
        "django.core.mail.backends.smtp.EmailBackend", lambda **kw: opened.append(kw)
    )
    monkeypatch.setattr(credential_checks, "resolve", lambda host: ["93.184.215.14"])
    for typed in (
        {"EMAIL_HOST": "smtp.attacker.example"},
        {"EMAIL_PORT": "2525"},
        {"EMAIL_HOST_USER": "x"},
    ):
        result = credential_checks.run("smtp", typed)
        assert result.code == "format" and "Password" in result.words, typed
        with pytest.raises(credentials.CredentialError, match="type the Password"):
            credentials.save(admin, "smtp", typed)
    assert not opened
    # The same host again (any case) keeps the saved password.
    assert credentials.secrets_to_retype("smtp", {"EMAIL_HOST": "SMTP.example.org"}) == []
    # Typed again, the new host is fine.
    assert (
        credential_checks.run(
            "smtp", {"EMAIL_HOST": "smtp.other.example", "EMAIL_HOST_PASSWORD": PASSWORD}
        ).code
        != "format"
    )


def test_a_new_bounce_mailbox_host_needs_its_password_again(admin):
    credentials.save(
        admin,
        "bounce",
        {
            "BOUNCE_IMAP_HOST": "imap.example.org",
            "BOUNCE_IMAP_USER": "bounces",
            "BOUNCE_IMAP_PASSWORD": "TESTONLY-imap",  # nosec B105 - fictional test value
        },
    )
    with mock.patch("comms.bounces.check_login") as login:
        result = credential_checks.run("bounce", {"BOUNCE_IMAP_HOST": "imap.attacker.example"})
    assert result.code == "format" and "Password or app password" in result.words
    login.assert_not_called()


def test_a_new_smtp2go_region_needs_the_key_again(admin, board):
    credentials.decide(board, _smtp2go(admin), "approve")
    result = credential_checks.run("smtp2go", {"SMTP2GO_REGION": "eu"})
    assert result.code == "format" and "SMTP2Go API key" in result.words


# --- EXP-004: public mail hosts and the usual ports only ----------------------------------


@pytest.mark.parametrize(
    "host",
    ["127.0.0.1", "10.0.0.5", "169.254.169.254", "host.docker.internal", "db.local", "localhost"],
)
def test_internal_hosts_are_refused(host):
    result = credential_checks.run(
        "smtp", {"EMAIL_HOST": host, "EMAIL_HOST_PASSWORD": PASSWORD, "EMAIL_PORT": "587"}
    )
    assert result.code == "format"


def test_only_mail_ports_are_accepted():
    for port, ok in (("587", True), ("465", True), ("5432", False), ("80", False)):
        problems = credential_checks.validate("smtp", {"EMAIL_PORT": port}, merged={})
        assert (not problems) is ok, port
    assert credential_checks.validate("bounce", {"BOUNCE_IMAP_PORT": "143"}, merged={})


def test_a_name_that_resolves_to_a_private_address_is_not_contacted(monkeypatch):
    opened = []
    monkeypatch.setattr(
        "django.core.mail.backends.smtp.EmailBackend", lambda **kw: opened.append(kw)
    )
    monkeypatch.setattr(credential_checks, "resolve", lambda host: ["10.1.2.3"])
    result = credential_checks.run(
        "smtp", {"EMAIL_HOST": "mail.example.org", "EMAIL_HOST_PASSWORD": PASSWORD}
    )
    assert result.code == "not_public" and not opened


# --- BUG-001: refuse or cancel until it takes effect --------------------------------------


def test_a_waiting_pin_move_can_be_refused_by_another_board_member(admin, board, board2):
    board3 = _with_mfa(make_member(board=True))
    credentials.decide(board, _stripe(admin), "approve")
    move = _stripe(admin, key=STRIPE_KEY_2, account=ACCT_B, pin_change=True)
    credentials.decide(board, move, "approve")
    credentials.decide(board2, move, "approve")
    assert credentials.change_state(move).state == credentials.WAITING
    with pytest.raises(credentials.CredentialError, match="already decided"):
        credentials.decide(board, move, "reject")  # one decision per person
    credentials.decide(board3, move, "reject")
    assert credentials.change_state(move).state == credentials.REFUSED
    later = timezone.now() + datetime.timedelta(hours=73)
    with mock.patch.object(credentials.timezone, "now", return_value=later):
        credentials.forget()
        assert credentials.pin("stripe") == ACCT_A
        assert credentials.get("STRIPE_SECRET_KEY") == STRIPE_KEY
    assert AuditEvent.objects.filter(
        action="system.credential_decided", metadata__decision="reject"
    ).exists()


def test_a_waiting_pin_move_can_be_cancelled_by_its_requester(client, admin, board, board2):
    credentials.decide(board, _stripe(admin), "approve")
    move = _stripe(admin, key=STRIPE_KEY_2, account=ACCT_B, pin_change=True)
    credentials.decide(board, move, "approve")
    credentials.decide(board2, move, "approve")
    board3 = _with_mfa(make_member(board=True))
    _login(client, board3)
    page = client.get(reverse("keys_change", args=[move.pk])).content.decode()
    assert 'value="reject"' in page and 'value="approve"' not in page
    _login(client, admin)
    page = client.get(reverse("keys_change", args=[move.pk])).content.decode()
    assert 'value="cancel"' in page
    client.post(reverse("keys_change", args=[move.pk]), {**CHECK, "decision": "cancel"})
    assert credentials.change_state(move).state == credentials.CANCELLED
    with pytest.raises(credentials.CredentialError, match="not waiting"):
        credentials.decide(board3, move, "approve")


# --- BUG-002 / SEC-002: removing Stripe or Teller keys ------------------------------------


def test_removing_the_stripe_key_waits_for_the_board(admin, board, board2, settings):
    settings.STRIPE_SECRET_KEY = ""  # nosec B105 - no key on the server
    credentials.decide(board, _stripe(admin), "approve")
    removal = credentials.clear(admin, "stripe")
    assert removal.approvals_needed == 1
    assert credentials.get("STRIPE_SECRET_KEY") == STRIPE_KEY  # still in use
    with pytest.raises(Exception, match=r"system\.approve_keys"):
        credentials.decide(admin, removal, "approve")  # a sysadmin alone is not the board
    credentials.decide(board2, removal, "approve")
    assert credentials.get("STRIPE_SECRET_KEY") == ""


def test_removing_the_stripe_key_is_refused_when_the_servers_key_is_for_another_account(
    admin, board, settings, monkeypatch
):
    credentials.decide(board, _stripe(admin), "approve")
    settings.STRIPE_SECRET_KEY = STRIPE_KEY_2
    settings.PAYMENTS_PROVIDER = "stripe"
    seen = {}

    def fake_test():
        seen["key"] = credentials.get("STRIPE_SECRET_KEY")
        return Result(True, "ok", "ok", ACCT_B)

    monkeypatch.setitem(credential_checks.TESTERS, "stripe", fake_test)
    with pytest.raises(credentials.CredentialError, match="different stripe account"):
        credentials.clear(admin, "stripe")
    assert seen["key"] == STRIPE_KEY_2  # the server's own key was tested
    assert not CredentialChange.objects.filter(kind="clear").exists()
    assert AuditEvent.objects.filter(
        action="system.credential_refused", metadata__reason="server_other_account"
    ).exists()
    monkeypatch.setitem(credential_checks.TESTERS, "stripe", lambda: Result(True, "ok", "", ACCT_A))
    assert credentials.clear(admin, "stripe").approvals_needed == 1


# --- BUG-005: a removal clears only its own service's values ------------------------------


def test_removing_the_smtp_values_keeps_the_smtp2go_sender_and_provider(admin, settings):
    settings.EMAIL_PROVIDER = "smtp"
    credentials.save(
        admin, "smtp", {"EMAIL_HOST": "smtp.example.org", "EMAIL_HOST_PASSWORD": PASSWORD}
    )
    _smtp2go(admin, DEFAULT_FROM_EMAIL=SENDER)
    assert credentials.get("EMAIL_PROVIDER") == "smtp2go"
    removal = credentials.clear(admin, "smtp")
    assert set(removal.values.values_list("name", flat=True)) == {
        "EMAIL_HOST",
        "EMAIL_HOST_PASSWORD",
    }
    assert credentials.get("EMAIL_PROVIDER") == "smtp2go"
    assert credentials.get("DEFAULT_FROM_EMAIL") == SENDER
    assert credentials.get("EMAIL_HOST") == settings.EMAIL_HOST
    with pytest.raises(credentials.CredentialError, match="Nothing is saved"):
        credentials.clear(admin, "smtp")


# --- BUG-006: the pin is checked when a key takes effect ----------------------------------


def test_a_key_for_the_old_account_approved_after_the_pin_moved_is_never_used(admin, board, board2):
    credentials.decide(board, _stripe(admin), "approve")
    move = _stripe(admin, key=STRIPE_KEY_2, account=ACCT_B, pin_change=True)
    credentials.decide(board, move, "approve")
    credentials.decide(board2, move, "approve")
    t0 = timezone.now()
    with mock.patch.object(
        credentials.timezone, "now", return_value=t0 + datetime.timedelta(hours=30)
    ):
        credentials.forget()
        old = _stripe(admin, key="rk_" + "test_TESTONLYfictional666777888")  # for A, A still pinned
    later = t0 + datetime.timedelta(hours=73)
    with mock.patch.object(credentials.timezone, "now", return_value=later):
        credentials.forget()
        assert credentials.pin("stripe") == ACCT_B
        with pytest.raises(credentials.CredentialError, match="pinned stripe account has moved"):
            credentials.decide(board, old, "approve")
    # Even if it had been approved: approve a copy directly, then read.
    CredentialDecision.objects.create(change=old, decision="approve", by=board2, at=later)
    with mock.patch.object(credentials.timezone, "now", return_value=later):
        credentials.forget()
        assert credentials.change_state(old).state == credentials.OTHER_ACCOUNT
        assert credentials.get("STRIPE_SECRET_KEY") == STRIPE_KEY_2


# --- BUG-003 / EXP-003: the vendor document key -------------------------------------------


def test_the_vendor_key_is_not_removed_while_documents_depend_on_it(admin, settings, monkeypatch):
    settings.VENDOR_DOCUMENT_KEY, settings.VENDOR_DOCUMENT_KEY_FALLBACKS = "", []
    credentials.make_vendor_key(admin)
    monkeypatch.setattr("operations.vendor_onboarding.stored_document_count", lambda: 2)
    with pytest.raises(credentials.CredentialError, match="2 vendor document"):
        credentials.clear(admin, "vendor_key")
    assert AuditEvent.objects.filter(
        action="system.credential_refused", metadata__reason="documents_held"
    ).exists()
    assert credentials.get("VENDOR_DOCUMENT_KEY")


def test_a_removed_vendor_key_stays_a_fallback_when_a_new_one_is_made(admin, settings):
    from operations import vendor_crypto

    settings.VENDOR_DOCUMENT_KEY, settings.VENDOR_DOCUMENT_KEY_FALLBACKS = "", []
    credentials.make_vendor_key(admin)
    blob = vendor_crypto.encrypt(b"fictional W-9", "file-1")
    credentials.clear(admin, "vendor_key")  # no documents stored in this test
    assert not vendor_crypto.configured()
    credentials.make_vendor_key(admin)
    assert vendor_crypto.key_index(blob, "file-1") >= 1
    assert vendor_crypto.decrypt(blob, "file-1") == b"fictional W-9"


# --- EXP-006: the server scripts and the portal's values ----------------------------------


def test_the_server_check_tests_the_env_value_and_says_the_portal_wins(admin, board, settings):
    credentials.decide(board, _stripe(admin), "approve")
    settings.STRIPE_SECRET_KEY, settings.PAYMENTS_PROVIDER = STRIPE_KEY_2, "stripe"
    seen = []
    with mock.patch(
        "payments.stripe_api.get",
        side_effect=lambda *a, **k: (
            seen.append(credentials.get("STRIPE_SECRET_KEY")) or {"livemode": False, "data": []}
        ),
    ):
        err = io.StringIO()
        with contextlib.suppress(CommandError):
            call_command("check_stripe", "--server", stdout=io.StringIO(), stderr=err)
    assert seen and set(seen) == {STRIPE_KEY_2}
    assert "win over .env" in err.getvalue() and "clear_portal_keys stripe" in err.getvalue()
    assert STRIPE_KEY not in err.getvalue() and STRIPE_KEY_2 not in err.getvalue()


def test_the_server_operator_clears_the_portals_values(admin, settings):
    settings.EMAIL_PROVIDER = "smtp"
    credentials.save(
        admin, "smtp", {"EMAIL_HOST": "smtp.example.org", "EMAIL_HOST_PASSWORD": PASSWORD}
    )
    out = io.StringIO()
    call_command("clear_portal_keys", "smtp", stdout=out)
    assert "applies now" in out.getvalue()
    assert credentials.get("EMAIL_HOST") == settings.EMAIL_HOST
    event = AuditEvent.objects.filter(action="system.credential_cleared").latest("id")
    assert event.metadata["from_server"] is True and event.actor_id is None
    with pytest.raises(CommandError, match="Nothing is saved"):
        call_command("clear_portal_keys", "smtp")
    with pytest.raises(CommandError, match="Unknown service"):
        call_command("clear_portal_keys", "nothing")


# --- EXP-007: error reports hide the key-encryption key -----------------------------------


def test_error_reports_hide_the_credentials_kek(settings, rf):
    from django.views.debug import get_default_exception_reporter_filter

    get_default_exception_reporter_filter.cache_clear()
    try:
        safe = get_default_exception_reporter_filter().get_safe_settings()
    finally:
        get_default_exception_reporter_filter.cache_clear()
    assert safe["CREDENTIALS_KEK"] != settings.CREDENTIALS_KEK
    assert "*" in safe["CREDENTIALS_KEK"]
