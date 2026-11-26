"""Forgot password still needs the second step, and lockouts are made rare (D58)."""

import re

import pytest
from allauth.mfa.models import Authenticator
from django.core import mail
from django.urls import reverse

from accounts.tests.helpers import PASSWORD, totp_code
from audit.models import AuditEvent
from community.tests.factories import LotFactory, enroll_totp, make_member
from core import my_items

pytestmark = pytest.mark.django_db

NEW_PASSWORD = "a brand new long passphrase 77"  # nosec B105 - test-only


def _owner(email="reset@example.com", mfa=True):
    user = make_member(lots=[LotFactory()], email=email, mfa=False)
    user.set_password(PASSWORD)
    user.save()
    secret = enroll_totp(user) if mfa else None
    return user, secret


def _reset_link(client, user, django_capture_on_commit_callbacks) -> str:
    mail.outbox = []
    with django_capture_on_commit_callbacks(execute=True):
        client.post(reverse("account_reset_password"), {"email": user.email})
    body = mail.outbox[-1].body
    path = re.search(r"(/accounts/password/reset/key/[^\s]+/)", body).group(1)
    response = client.get(path)
    assert response.status_code == 302
    return response["Location"]


# --- Forgot password: emailed link -> second step -> new password ---------------------------


def test_reset_link_asks_for_the_second_step_before_the_password(
    client, django_capture_on_commit_callbacks
):
    user, _secret = _owner()
    url = _reset_link(client, user, django_capture_on_commit_callbacks)
    page = client.get(url)
    assert page.status_code == 200
    assert b"Confirm it's you" in page.content
    assert b'name="password1"' not in page.content


def test_new_password_cannot_be_posted_without_the_second_step(
    client, django_capture_on_commit_callbacks
):
    user, _secret = _owner()
    url = _reset_link(client, user, django_capture_on_commit_callbacks)
    page = client.post(url, {"password1": NEW_PASSWORD, "password2": NEW_PASSWORD})
    assert b"Confirm it's you" in page.content
    user.refresh_from_db()
    assert user.check_password(PASSWORD)


def test_wrong_code_is_refused_and_audited(client, django_capture_on_commit_callbacks):
    user, _secret = _owner()
    url = _reset_link(client, user, django_capture_on_commit_callbacks)
    page = client.post(url, {"step": "verify", "code": "000000"})
    assert b"That code is not right" in page.content
    assert AuditEvent.objects.filter(action="password.reset_verify_failed").exists()


def test_app_code_then_new_password_never_signs_in(client, django_capture_on_commit_callbacks):
    user, secret = _owner()
    url = _reset_link(client, user, django_capture_on_commit_callbacks)
    page = client.post(url, {"step": "verify", "code": totp_code(secret)})
    assert b'name="password1"' in page.content
    mail.outbox = []
    with django_capture_on_commit_callbacks(execute=True):
        done = client.post(url, {"password1": NEW_PASSWORD, "password2": NEW_PASSWORD})
    assert done.status_code == 302
    user.refresh_from_db()
    assert user.check_password(NEW_PASSWORD)
    assert "_auth_user_id" not in client.session
    changed = [m for m in mail.outbox if "Your password was changed" in m.subject]
    assert changed and user.email in [m.to[0] for m in changed]
    assert AuditEvent.objects.filter(action="password.reset").exists()
    verified = AuditEvent.objects.get(action="password.reset_verified")
    assert verified.metadata == {"type": "totp"}
    # Signing in with the new password still asks for the app.
    response = client.post(
        reverse("account_login"), {"login": user.email, "password": NEW_PASSWORD}
    )
    assert response["Location"] == reverse("mfa_authenticate")
    assert "_auth_user_id" not in client.session


def test_a_recovery_code_works_as_the_second_step(client, django_capture_on_commit_callbacks):
    from allauth.mfa.recovery_codes.internal.auth import RecoveryCodes

    user, _secret = _owner()
    codes = RecoveryCodes.activate(user)
    code = codes.get_unused_codes()[0]
    url = _reset_link(client, user, django_capture_on_commit_callbacks)
    page = client.post(url, {"step": "verify", "code": code})
    assert b'name="password1"' in page.content
    codes.instance.refresh_from_db()
    assert code not in RecoveryCodes(codes.instance).get_unused_codes()


def test_account_without_authenticator_is_sent_to_the_board(
    client, django_capture_on_commit_callbacks
):
    user, _ = _owner("nomfa@example.com", mfa=False)
    url = _reset_link(client, user, django_capture_on_commit_callbacks)
    page = client.get(url)
    assert page.status_code == 403 and b"Ask the board" in page.content


def test_login_page_has_a_clear_forgot_password_link(client):
    page = client.get(reverse("account_login")).content.decode()
    assert "Forgot your password?" in page and reverse("account_reset_password") in page


# --- Backup sign-in method and recovery codes on My items ---------------------------------


def _titles(user):
    return [i.title for i in my_items.items_for(user)]


def test_one_strong_method_asks_for_a_backup():
    user = make_member(lots=[LotFactory()])
    assert "Add a backup sign-in method" in _titles(user)


def test_two_strong_methods_need_no_backup():
    user = make_member(lots=[LotFactory()])
    Authenticator.objects.create(user=user, type=Authenticator.Type.WEBAUTHN, data={})
    assert "Add a backup sign-in method" not in _titles(user)
    two_keys = make_member(lots=[LotFactory()], mfa=False)
    for _ in range(2):
        Authenticator.objects.create(user=two_keys, type=Authenticator.Type.WEBAUTHN, data={})
    assert "Add a backup sign-in method" not in _titles(two_keys)


def test_no_backup_item_before_setup():
    user = make_member(lots=[LotFactory()], mfa=False)
    assert "Add a backup sign-in method" not in _titles(user)


def test_two_or_fewer_recovery_codes_left_is_a_reminder():
    from allauth.mfa.recovery_codes.internal.auth import RecoveryCodes

    user = make_member(lots=[LotFactory()])
    codes = RecoveryCodes.activate(user)
    assert "Make new recovery codes" not in _titles(user)
    for code in codes.get_unused_codes()[:-2]:
        assert codes.validate_code(code)
    assert "Make new recovery codes" in _titles(user)


def test_backup_item_shows_for_board_members_too():
    board = make_member(board=True)
    assert "Add a backup sign-in method" in _titles(board)


# --- Recovery codes: shown once, printable page and text file ------------------------------


@pytest.fixture
def codes_client(client):
    """A new owner who just set up an app in the invitation's session."""
    from unittest import mock

    from accounts import services
    from community.tests.factories import PersonFactory

    board = make_member(board=True)
    with mock.patch("accounts.tasks.send_invitation.defer"):
        invitation = services.create_invitation(
            person=PersonFactory(email="codes@example.com"), actor=board
        )
    mail.outbox = []
    services.send_invitation_email(invitation.pk)
    token = re.search(r"/invite/([^/\s]+)/", mail.outbox[-1].body).group(1)
    client.get(reverse("invite_landing", args=[token]))
    client.post(reverse("invite_accept"), {"password1": PASSWORD, "password2": PASSWORD})
    page = client.get(reverse("mfa_activate_totp"))
    client.post(reverse("mfa_activate_totp"), {"code": totp_code(page.context["form"].secret)})
    return client


def test_codes_are_shown_once_then_printed_or_downloaded_from_that_showing(codes_client):
    page = codes_client.get(reverse("mfa_view_recovery_codes")).content.decode()
    assert len(re.findall(r"<li><code>", page)) == 10
    assert "only now" in page
    assert reverse("mfa_print_recovery_codes") in page
    assert reverse("mfa_download_recovery_codes") in page
    download = codes_client.get(reverse("mfa_download_recovery_codes"))
    assert download.status_code == 200 and download["Content-Type"].startswith("text/plain")
    assert "attachment" in download["Content-Disposition"]
    printable = codes_client.get(reverse("mfa_print_recovery_codes")).content.decode()
    assert len(re.findall(r"<li><code>", printable)) == 10
    hows = list(
        AuditEvent.objects.filter(action="mfa.recovery_codes_viewed").values_list(
            "metadata__how", flat=True
        )
    )
    assert sorted(set(hows)) == ["download", "page", "print"]
    for event in AuditEvent.objects.filter(action="mfa.recovery_codes_viewed"):
        assert set(event.metadata) == {"how"}
    # Another session (or this one after the grant ends) sees them hidden.
    session = codes_client.session
    session.pop("recovery_codes_shown")
    session.save()
    hidden = codes_client.get(reverse("mfa_view_recovery_codes")).content.decode()
    assert "<li><code>" not in hidden
    assert "shown only once" in hidden
    assert codes_client.get(reverse("mfa_download_recovery_codes")).status_code == 404
    assert codes_client.get(reverse("mfa_print_recovery_codes")).status_code == 404


def test_download_and_print_need_sign_in(client):
    for name in ("mfa_download_recovery_codes", "mfa_print_recovery_codes"):
        assert client.get(reverse(name))["Location"].startswith(reverse("account_login"))
