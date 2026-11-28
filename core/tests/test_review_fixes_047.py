"""Review fixes for 0.47.0 (regression/catalogue/fixes_047.yaml), portal side:
live mail sending and bounce polling try each checked public address in turn
(BUG-004), and a non-ASCII digit in a port field, or in a numeric id a page
reads, is a form error rather than a 500 (BUG-005). The simulation's fixes
are in regression/tests/test_simulation.py; the workflow image pin (SEC-001)
too."""

from __future__ import annotations

import time

import pytest
from django.core.mail import EmailMessage
from django.urls import reverse

from comms import bounces, sms
from community.models import Role
from community.tests.factories import make_member
from core import credential_checks
from core import email as core_email
from core.credential_checks import TestResult as Result

SMTP = "django.core.mail.backends.smtp.EmailBackend"
FIRST, SECOND, THIRD = "93.184.215.14", "93.184.215.15", "2606:2800:220:1::1"
PASSWORD = "TESTONLY-pw"  # nosec B105 - fictional test value


# --- BUG-004: every checked public address is tried, in order -------------------------------


class _Connected(Exception):
    pass


def _fake_connect(monkeypatch, answering=None):
    """Every address refuses except ``answering``, which 'connects' (raises
    _Connected so nothing further happens)."""
    tried = []

    def connect(address, port, timeout, source_address=None):
        tried.append((address, port))
        if address == answering:
            raise _Connected(address)
        raise ConnectionRefusedError("test: down")

    monkeypatch.setattr(credential_checks, "_connect", connect)
    return tried


def test_connect_any_tries_each_address_and_stops_at_the_first_that_answers(monkeypatch):
    tried = _fake_connect(monkeypatch, answering=SECOND)
    with pytest.raises(_Connected):
        credential_checks._connect_any([FIRST, SECOND, THIRD], 587, 5)
    assert tried == [(FIRST, 587), (SECOND, 587)]


def test_connect_any_raises_the_last_failure_when_none_answers(monkeypatch):
    tried = _fake_connect(monkeypatch)
    with pytest.raises(ConnectionRefusedError):
        credential_checks._connect_any([FIRST, SECOND], 993, 5)
    assert tried == [(FIRST, 993), (SECOND, 993)]
    with pytest.raises(ValueError):
        credential_checks._connect_any([], 993, 5)


def _smtp_settings(settings, monkeypatch):
    settings.EMAIL_TRANSPORT_BACKEND = SMTP
    settings.EMAIL_REDIRECT_ALL_TO = ""
    monkeypatch.setattr(
        core_email,
        "smtp_options",
        lambda: {
            "host": "smtp.example.org",
            "port": 587,
            "username": "",
            "password": "",  # nosec B105 - empty test value
            "use_tls": False,
            "use_ssl": False,
        },
    )


def _send():
    message = EmailMessage("Hi", "Body", "portal@example.com", ["owner@example.com"])
    with pytest.raises(core_email.EmailUnavailable) as caught:
        core_email.RedirectingBackend().send_messages([message])
    return caught.value.__cause__


@pytest.mark.django_db
def test_live_mail_falls_over_to_the_relays_next_address(settings, monkeypatch):
    _smtp_settings(settings, monkeypatch)
    monkeypatch.setattr(credential_checks, "resolve", lambda host: [FIRST, SECOND, THIRD])
    tried = _fake_connect(monkeypatch)
    _send()
    assert tried == [(FIRST, 587), (SECOND, 587), (THIRD, 587)]


@pytest.mark.django_db
def test_live_mail_still_refuses_when_any_address_is_private(settings, monkeypatch):
    _smtp_settings(settings, monkeypatch)
    monkeypatch.setattr(credential_checks, "resolve", lambda host: [FIRST, "10.1.2.3"])
    tried = _fake_connect(monkeypatch)
    assert isinstance(_send(), core_email.NotPublicHost)
    assert tried == []


def _cfg():
    return bounces.Config("imap.example.org", 993, "bounces", PASSWORD, "INBOX", True)


def test_bounce_polling_falls_over_to_the_mailboxs_next_address(monkeypatch):
    monkeypatch.setattr(credential_checks, "resolve", lambda host: [FIRST, SECOND])
    tried = _fake_connect(monkeypatch)
    with pytest.raises(bounces.MailboxError) as caught, bounces.Mailbox(_cfg()):
        pass
    assert caught.value.code == "connect"
    assert tried == [(FIRST, 993), (SECOND, 993)]


def test_bounce_polling_still_refuses_when_any_address_is_private(monkeypatch):
    monkeypatch.setattr(credential_checks, "resolve", lambda host: [FIRST, "192.168.1.9"])
    tried = _fake_connect(monkeypatch)
    with pytest.raises(bounces.MailboxError) as caught, bounces.Mailbox(_cfg()):
        pass
    assert caught.value.code == "not_public" and tried == []


@pytest.mark.django_db
def test_the_keys_tests_try_every_checked_address_too(monkeypatch):
    monkeypatch.setattr(credential_checks, "resolve", lambda host: [FIRST, SECOND])
    tried = _fake_connect(monkeypatch)
    result = credential_checks.run(
        "bounce",
        {
            "BOUNCE_IMAP_HOST": "imap.example.org",
            "BOUNCE_IMAP_USER": "bounces",
            "BOUNCE_IMAP_PASSWORD": PASSWORD,
        },
    )
    assert result.code == "connect"
    assert tried == [(FIRST, 993), (SECOND, 993)]


# --- BUG-005: a non-ASCII digit is a form error, never a 500 ---------------------------------

# Superscripts, Arabic-Indic and full-width digits: str.isdigit() is true,
# int() of the first two refuses them.
ODD_DIGITS = [
    chr(0xB2),
    "5" + chr(0xB9) + "87",
    "".join(map(chr, (0x665, 0x668, 0x667))),
    "".join(map(chr, (0xFF15, 0xFF18, 0xFF17))),
]


@pytest.mark.parametrize("value", ODD_DIGITS)
@pytest.mark.parametrize(
    ("service", "field"), [("smtp", "EMAIL_PORT"), ("bounce", "BOUNCE_IMAP_PORT")]
)
def test_a_port_with_a_non_ascii_digit_is_a_problem_not_a_crash(service, field, value):
    assert value.isdigit()  # what fooled the old check
    problems = credential_checks.validate(service, {field: value}, merged={})
    assert problems == ["Port must be a port number."]


@pytest.mark.parametrize("value", ["587", "465", "2525"])
def test_ascii_ports_still_pass(value):
    assert credential_checks.validate("smtp", {"EMAIL_PORT": value}, merged={}) == []


@pytest.mark.django_db
def test_keys_and_connections_answers_a_form_error_for_a_superscript_port(client, monkeypatch):
    admin = make_member(roles=[Role.SYSADMIN])
    called = []
    monkeypatch.setattr(
        credential_checks,
        "TESTERS",
        {
            k: (lambda: called.append(1) or Result(True, "ok", "ok"))
            for k in credential_checks.TESTERS
        },
    )
    client.force_login(admin)
    session = client.session
    session["account_authentication_methods"] = [
        {"method": "mfa", "type": "totp", "at": time.time(), "reauthenticated": True}
    ]
    session.save()
    response = client.post(
        reverse("keys_service", args=["smtp"]),
        {
            "check": "keys-confirmed",
            "action": "test",
            "EMAIL_HOST": "smtp.example.org",
            "EMAIL_PORT": chr(0xB2),
            "EMAIL_SECURITY": "tls",
            "EMAIL_HOST_USER": "u",
            "EMAIL_HOST_PASSWORD": PASSWORD,
        },
    )
    assert response.status_code == 200
    assert "must be a port number" in response.content.decode()
    assert not called


@pytest.mark.django_db
def test_record_payments_ignores_a_non_ascii_digit_in_recorded(client):
    manager = make_member(roles=[Role.MANAGER])
    client.force_login(manager)
    response = client.get(reverse("record_payments") + "?recorded=" + chr(0xB2) + "," + chr(0x661))
    assert response.status_code == 200


def test_an_urgent_notice_link_with_a_non_ascii_digit_is_refused():
    with pytest.raises(sms.SmsError):
        sms.link_path("announcement:" + chr(0xB2))
