"""Review fix of 0.45.0 (BUG-004 / EXP-003): the SMTP and IMAP tests on Keys
and connections resolve the host once, check that every address is public,
and connect to that checked address (the host name kept for TLS), so a second
lookup (DNS rebinding) cannot turn the connection to an internal address."""

from __future__ import annotations

import pytest

from core import credential_checks

pytestmark = pytest.mark.django_db

PASSWORD = "TESTONLY-pw"  # nosec B105 - fictional test value
PUBLIC, PRIVATE = "93.184.215.14", "10.1.2.3"


@pytest.fixture
def lookups(monkeypatch):
    """The first lookup answers a public address, any later one a private one."""
    asked = []

    def resolve(host):
        asked.append(host)
        return [PUBLIC] if len(asked) == 1 else [PRIVATE]

    monkeypatch.setattr(credential_checks, "resolve", resolve)
    connected = []

    def connect(address, port, timeout, source_address=None):
        connected.append((address, port))
        raise OSError("test: no network")

    monkeypatch.setattr(credential_checks, "_connect", connect)
    return asked, connected


def test_public_addresses_needs_every_address_public(monkeypatch):
    monkeypatch.setattr(credential_checks, "resolve", lambda host: [PUBLIC, PRIVATE])
    assert credential_checks.public_addresses("mail.example.org") == []
    assert not credential_checks.public_host("mail.example.org")
    monkeypatch.setattr(credential_checks, "resolve", lambda host: [PUBLIC])
    assert credential_checks.public_addresses("mail.example.org") == [PUBLIC]


@pytest.mark.parametrize("security", ["tls", "ssl"])
def test_the_smtp_test_connects_to_the_checked_address(lookups, security):
    asked, connected = lookups
    result = credential_checks.run(
        "smtp",
        {
            "EMAIL_HOST": "smtp.example.org",
            "EMAIL_PORT": "587" if security == "tls" else "465",
            "EMAIL_SECURITY": security,
            "EMAIL_HOST_USER": "u",
            "EMAIL_HOST_PASSWORD": PASSWORD,
        },
    )
    assert result.code == "connect"
    assert asked == ["smtp.example.org"]  # one lookup only
    assert connected == [(PUBLIC, 587 if security == "tls" else 465)]


def test_the_imap_test_connects_to_the_checked_address(lookups):
    asked, connected = lookups
    result = credential_checks.run(
        "bounce",
        {
            "BOUNCE_IMAP_HOST": "imap.example.org",
            "BOUNCE_IMAP_USER": "bounces",
            "BOUNCE_IMAP_PASSWORD": PASSWORD,
        },
    )
    assert result.code == "connect"
    assert asked == ["imap.example.org"]
    assert connected == [(PUBLIC, 993)]


def test_the_pinned_classes_keep_the_host_name_for_tls():
    import imaplib
    import smtplib

    assert issubclass(credential_checks.pinned_smtp(PUBLIC, ssl=True), smtplib.SMTP_SSL)
    assert issubclass(credential_checks.pinned_smtp(PUBLIC, ssl=False), smtplib.SMTP)
    assert issubclass(credential_checks.pinned_imap(PUBLIC), imaplib.IMAP4_SSL)
