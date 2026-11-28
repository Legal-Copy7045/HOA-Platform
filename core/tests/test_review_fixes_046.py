"""Review fixes for 0.46.0 (regression/catalogue/fixes_046.yaml): the mail
server port the outbound firewall blocks is refused (BUG-001), and live mail
sending and bounce polling connect only to a public address checked at
connect time, outside local development (EXP-002, BUG-004). The ledger's
newest-first pages (BUG-002) are in test_layout_046.py, the ended payment
plan (BUG-003) in test_stages.py and the firewall gaps (EXP-001, EXP-003) in
test_egress_firewall.py."""

from __future__ import annotations

import imaplib

import pytest
from django.core.mail import EmailMessage

from comms import bounces
from core import credential_checks
from core import email as core_email

SMTP = "django.core.mail.backends.smtp.EmailBackend"


# --- BUG-001: port 25 is refused with the firewall's reason -------------------------------


def test_port_25_is_refused_with_a_clear_reason():
    problems = credential_checks.validate("smtp", {"EMAIL_PORT": "25"}, merged={})
    assert len(problems) == 1
    assert "25 is blocked by the server's outbound firewall" in problems[0]
    assert "587" in problems[0] and "465" in problems[0] and "2525" in problems[0]
    for port in ("587", "465", "2525"):
        assert not credential_checks.validate("smtp", {"EMAIL_PORT": port}, merged={}), port
    assert 25 not in credential_checks.PORTS["EMAIL_PORT"]


def test_a_saved_port_25_fails_its_test_at_once_without_connecting(monkeypatch):
    opened = []
    monkeypatch.setattr(SMTP, lambda **kw: opened.append(kw))
    monkeypatch.setattr(
        core_email,
        "smtp_options",
        lambda: {"host": "smtp.example.org", "port": 25, "username": "", "password": ""},
    )
    result = credential_checks._smtp()
    assert result.code == "port_blocked" and "outbound firewall" in result.words
    assert not opened


# --- EXP-002: live connections pinned to a checked public address -------------------------


@pytest.mark.parametrize(
    ("host", "debug", "pinned"),
    [
        ("smtp.example.org", False, True),
        ("smtp.example.org", True, False),  # local development
        ("mailpit", False, False),  # Mailpit, from .env
        ("10.0.0.5", False, False),
        ("relay.lan", False, False),
        ("", False, False),
    ],
)
def test_which_hosts_are_pinned(settings, host, debug, pinned):
    settings.DEBUG = debug
    assert core_email.pin_wanted(host) is pinned


def _smtp_settings(settings, monkeypatch, host):
    settings.EMAIL_TRANSPORT_BACKEND = SMTP
    settings.EMAIL_REDIRECT_ALL_TO = ""
    monkeypatch.setattr(
        core_email,
        "smtp_options",
        lambda: {
            "host": host,
            "port": 587,
            "username": "",
            "password": "",
            "use_tls": False,
            "use_ssl": False,
        },
    )
    connected = []

    def fake_connect(address, port, timeout, source_address=None):
        connected.append((address, port))
        raise ConnectionRefusedError("test")

    monkeypatch.setattr(credential_checks, "_connect", fake_connect)
    return connected


def _send():
    message = EmailMessage("Hi", "Body", "portal@example.com", ["owner@example.com"])
    with pytest.raises(core_email.EmailUnavailable) as caught:
        core_email.RedirectingBackend().send_messages([message])
    return caught.value.__cause__


@pytest.mark.django_db
def test_live_mail_to_a_host_now_pointing_at_a_private_address_is_never_sent(settings, monkeypatch):
    connected = _smtp_settings(settings, monkeypatch, "smtp.example.org")
    monkeypatch.setattr(credential_checks, "resolve", lambda host: ["10.1.2.3"])
    cause = _send()
    assert isinstance(cause, core_email.NotPublicHost)
    assert core_email.failure_code(cause) == "not_public"
    assert connected == []


@pytest.mark.django_db
def test_live_mail_connects_to_the_public_address_it_checked(settings, monkeypatch):
    connected = _smtp_settings(settings, monkeypatch, "smtp.example.org")
    answers = iter([["93.184.215.14"], ["10.1.2.3"]])  # a second lookup would rebind
    monkeypatch.setattr(credential_checks, "resolve", lambda host: next(answers))
    _send()
    assert connected == [("93.184.215.14", 587)]


def test_mailpit_is_used_as_before(settings, monkeypatch):
    _smtp_settings(settings, monkeypatch, "mailpit")
    backend = core_email.RedirectingBackend()
    assert type(backend._inner).__name__ == "EmailBackend"
    assert backend._inner.host == "mailpit"


def _cfg(host):
    return bounces.Config(host, 993, "bounces", "pw", "INBOX", True)


def test_bounce_polling_refuses_a_mailbox_host_now_private(monkeypatch):
    monkeypatch.setattr(credential_checks, "resolve", lambda host: ["192.168.1.9"])
    with pytest.raises(bounces.MailboxError) as caught, bounces.Mailbox(_cfg("imap.example.org")):
        pass
    assert caught.value.code == "not_public"
    assert "not_public" in bounces.FAILURE_WORDS


def test_bounce_polling_connects_to_the_checked_public_address(monkeypatch):
    connected = []

    def fake_connect(address, port, timeout, source_address=None):
        connected.append((address, port))
        raise ConnectionRefusedError("test")

    monkeypatch.setattr(credential_checks, "_connect", fake_connect)
    monkeypatch.setattr(credential_checks, "resolve", lambda host: ["93.184.215.14"])
    with pytest.raises(bounces.MailboxError) as caught, bounces.Mailbox(_cfg("imap.example.org")):
        pass
    assert caught.value.code == "connect"
    assert connected == [("93.184.215.14", 993)]


def test_bounce_polling_in_local_development_is_not_pinned(settings, monkeypatch):
    monkeypatch.setattr(credential_checks, "resolve", lambda host: ["93.184.215.14"])
    settings.DEBUG = True
    assert bounces.Mailbox(_cfg("imap.example.org"))._real_factory() is imaplib.IMAP4_SSL
    settings.DEBUG = False
    assert bounces.Mailbox(_cfg("imap.example.org"))._real_factory() is not imaplib.IMAP4_SSL
