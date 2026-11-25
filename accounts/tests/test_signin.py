"""Sign-in rules (decisions D5, D6): never password-only, invite-only, lockout."""

import re

import pytest
from django.core import mail
from django.urls import reverse

from accounts.tests.helpers import PASSWORD, current_totp
from audit.models import AuditEvent
from audit.services import hash_identifier
from community.tests.factories import LotFactory, enroll_totp, make_member

pytestmark = pytest.mark.django_db


@pytest.fixture
def owner():
    user = make_member(lots=[LotFactory()], email="owner1@example.com", mfa=False)
    user.set_password(PASSWORD)
    user.save()
    user.totp_secret = enroll_totp(user)
    return user


def _password_login(client, email, password=PASSWORD):
    return client.post(reverse("account_login"), {"login": email, "password": password})


def _session_user(client):
    return client.session.get("_auth_user_id")


def test_signup_and_email_change_are_closed(client):
    assert client.get("/accounts/signup/").status_code == 404
    assert client.get("/accounts/email/").status_code == 404


def test_password_alone_does_not_sign_in(client, owner):
    response = _password_login(client, owner.email)
    assert response.status_code == 302
    assert response["Location"] == reverse("mfa_authenticate")
    assert _session_user(client) is None


def test_password_then_authenticator_code_signs_in(client, owner):
    _password_login(client, owner.email)
    response = client.post(reverse("mfa_authenticate"), {"code": current_totp(owner.totp_secret)})
    assert response.status_code == 302
    assert _session_user(client) == str(owner.pk)
    event = AuditEvent.objects.filter(action="auth.login").latest("id")
    assert event.actor == owner
    assert event.metadata == {"method": "password"}


def test_wrong_authenticator_code_does_not_sign_in(client, owner):
    _password_login(client, owner.email)
    client.post(reverse("mfa_authenticate"), {"code": "000000"})
    assert _session_user(client) is None


def test_emailed_code_is_a_first_factor_only(client, owner):
    client.post(reverse("account_request_login_code"), {"email": owner.email})
    code = re.search(r"\b([A-Z0-9]{4}-[A-Z0-9]{4})\b", mail.outbox[-1].body).group(1)
    response = client.post(reverse("account_confirm_login_code"), {"code": code})
    assert response["Location"] == reverse("mfa_authenticate")
    assert _session_user(client) is None
    client.post(reverse("mfa_authenticate"), {"code": current_totp(owner.totp_secret)})
    assert _session_user(client) == str(owner.pk)


def test_allauth_emails_name_the_community_not_the_server(client, owner):
    client.post(reverse("account_request_login_code"), {"email": owner.email})
    body = mail.outbox[-1].body
    assert "Oak Creek Farms HOA" in body
    assert "testserver" not in body


def _with_enrollment_window(client):
    session = client.session
    session["mfa_enrollment_until"] = 2**40
    session.save()


def test_account_without_authenticator_can_only_enroll(client):
    user = make_member(lots=[LotFactory()], mfa=False)
    client.force_login(user)
    _with_enrollment_window(client)
    response = client.get(reverse("home"))
    assert response.status_code == 302
    assert response["Location"] == reverse("mfa_index")
    assert client.get(reverse("mfa_index")).status_code == 200
    assert client.get(reverse("mfa_activate_totp")).status_code in (200, 302)


@pytest.mark.parametrize("who", ["board", "owner", "sysadmin"])
def test_mfa_is_mandatory_for_every_kind_of_account(client, who):
    from community.models import Role

    kwargs = {
        "board": {"board": True},
        "owner": {"lots": [LotFactory()]},
        "sysadmin": {"roles": [Role.SYSADMIN]},
    }[who]
    client.force_login(make_member(mfa=False, **kwargs))
    _with_enrollment_window(client)
    assert client.get(reverse("home"))["Location"] == reverse("mfa_index")


@pytest.mark.parametrize("url_name", ["home", "mfa_index", "mfa_activate_totp"])
def test_password_alone_never_opens_an_unenrolled_account(client, url_name):
    """Review finding B2: someone who learned the password (or reset it from the
    mailbox) must not be able to sign in and enroll their own authenticator.
    Enrollment is only possible in the session that accepted the invitation."""
    client.force_login(make_member(lots=[LotFactory()], mfa=False))
    response = client.get(reverse(url_name))
    assert response.status_code == 302
    assert response["Location"] == reverse("account_setup_expired")
    assert "_auth_user_id" not in client.session  # signed out


def test_enrollment_window_expires(client, settings):
    client.force_login(make_member(lots=[LotFactory()], mfa=False))
    session = client.session
    session["mfa_enrollment_until"] = 1  # long past
    session.save()
    assert client.get(reverse("mfa_index"))["Location"] == reverse("account_setup_expired")


def test_failed_login_for_unknown_email_stores_only_a_hash(client):
    _password_login(client, "Nobody@Example.com", "wrong password here")
    event = AuditEvent.objects.get(action="auth.login_failed")
    assert event.subject_hash == hash_identifier("nobody@example.com")
    assert event.target_id == ""
    assert "nobody" not in str(event.metadata)


def test_failed_login_for_known_account_names_the_target(client, owner):
    _password_login(client, owner.email, "wrong password here")
    event = AuditEvent.objects.get(action="auth.login_failed")
    assert event.target_id == str(owner.pk)
    assert event.actor is None


def test_five_failures_lock_the_account_from_that_address(client, owner):
    for _ in range(5):
        _password_login(client, owner.email, "wrong password here")
    response = _password_login(client, owner.email)  # even the right password
    assert response.status_code == 429
    assert b"Too many sign-in attempts" in response.content
    assert _session_user(client) is None
    assert AuditEvent.objects.filter(action="auth.lockout").exists()


def test_lockout_is_per_address(client, owner):
    for _ in range(5):
        client.post(
            reverse("account_login"),
            {"login": owner.email, "password": "nope nope"},  # nosec B105
            REMOTE_ADDR="198.51.100.1",
        )
    response = client.post(
        reverse("account_login"),
        {"login": owner.email, "password": PASSWORD},
        REMOTE_ADDR="198.51.100.2",
    )
    assert response["Location"] == reverse("mfa_authenticate")


def test_logout_is_audited(client, owner):
    client.force_login(owner)
    client.post(reverse("account_logout"))
    assert AuditEvent.objects.filter(action="auth.logout", actor=owner).exists()


def test_mfa_changes_are_audited(rf, owner):
    from allauth.mfa.models import Authenticator
    from allauth.mfa.signals import authenticator_removed

    request = rf.get("/")
    request.user = owner
    authenticator = Authenticator.objects.get(user=owner)
    authenticator_removed.send(
        sender=Authenticator, request=request, user=owner, authenticator=authenticator
    )
    event = AuditEvent.objects.get(action="mfa.authenticator_removed")
    assert event.metadata == {"type": "totp"}


def test_code_from_the_previous_window_is_accepted(client, owner):
    from accounts.tests.helpers import totp_code

    _password_login(client, owner.email)
    client.post(reverse("mfa_authenticate"), {"code": totp_code(owner.totp_secret, -1)})
    assert _session_user(client) == str(owner.pk)


def test_code_from_two_windows_ago_is_refused(client, owner):
    from accounts.tests.helpers import totp_code

    _password_login(client, owner.email)
    client.post(reverse("mfa_authenticate"), {"code": totp_code(owner.totp_secret, -3)})
    assert _session_user(client) is None


def test_reauthentication_never_offers_an_emailed_code():
    """Review B6: allauth's default lets an emailed code re-authenticate."""
    from allauth.account.internal.flows.reauthentication import get_reauthentication_flows

    user = make_member(lots=[LotFactory()])
    ids = {flow["id"] for flow in get_reauthentication_flows(user)}
    assert "reauthenticate_by_code" not in ids
    assert ids & {"reauthenticate", "mfa_reauthenticate"}
