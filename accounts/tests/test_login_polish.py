"""Owner feedback on sign-in: a mail outage is a friendly page, people are
shown by name, no "signed in" toast, and the code page offers the passkey as
a styled button."""

import smtplib

import pytest
from django.core.mail.backends.base import BaseEmailBackend

from community.tests.factories import LotFactory, make_member
from core import health
from core.people import display_name

pytestmark = pytest.mark.django_db


class BrokenTransport(BaseEmailBackend):
    def send_messages(self, messages):
        raise smtplib.SMTPConnectError(421, "down")


def test_login_code_with_mail_down_is_a_friendly_page(client, settings):
    settings.EMAIL_BACKEND = "core.email.RedirectingBackend"
    settings.EMAIL_TRANSPORT_BACKEND = "accounts.tests.test_login_polish.BrokenTransport"
    user = make_member(lots=[LotFactory()])
    response = client.post("/accounts/login/code/", {"email": user.email})
    assert response.status_code == 503
    assert (
        b"couldn&#x27;t send the email" in response.content or b"couldn't send" in response.content
    )
    assert user.email.encode() not in response.content
    email_check = {r.key: r for r in health.run_checks()}["email"]
    assert not email_check.ok


def test_people_are_names_not_numbers(client):
    user = make_member(lots=[LotFactory()])
    name = display_name(user)
    assert name and "#" not in name and str(user.pk) != name


def test_no_signed_in_toast():
    from django.template.loader import render_to_string

    assert render_to_string("account/messages/logged_in.txt", {}).strip() == ""


def test_code_page_wording():
    from pathlib import Path

    text = Path("templates/mfa/authenticate.html").read_text()
    assert "Use my passkey instead" in text and "Use a security key" not in text
    assert 'id="mfa_webauthn_authenticate"' in text  # the passkey script still finds it
