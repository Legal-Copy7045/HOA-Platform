"""UX review (2026-11): guided sign-in setup, the login page, sign-in help and
the pages around them. The MFA rules themselves (D5, review B2) are unchanged
and still tested in test_signin.py and test_invitations.py; here we check the
pages guide people without opening anything new."""

import datetime
import re
from unittest import mock

import pytest
from django.core import mail
from django.urls import reverse

from accounts import enrollment, services
from accounts.models import VerificationMethod
from accounts.tests.helpers import PASSWORD, totp_code
from community.models import CommunityProfile
from community.tests.factories import LotFactory, PersonFactory, make_member

pytestmark = pytest.mark.django_db

SIDEBAR = b'class="sidebar"'
WRONG = "not the password"  # nosec B105 - test-only


def _contact(text="the board at board@example.org or 555-0100"):
    profile = CommunityProfile.get()
    profile.signin_help_contact = text
    profile.save()
    return text


def _invite(person, board):
    with mock.patch("accounts.tasks.send_invitation.defer"):
        invitation = services.create_invitation(person=person, actor=board)
    mail.outbox = []
    services.send_invitation_email(invitation.pk)
    return re.search(r"/invite/([^/\s]+)/", mail.outbox[-1].body).group(1)


@pytest.fixture
def new_owner(client):
    """A fresh owner who has just accepted an invitation (step 1 done)."""
    board = make_member(board=True)
    person = PersonFactory(email="setup@example.com")
    token = _invite(person, board)
    client.get(reverse("invite_landing", args=[token]))
    response = client.post(reverse("invite_accept"), {"password1": PASSWORD, "password2": PASSWORD})
    assert response["Location"] == reverse("mfa_index")
    return client


def _activate_totp(client):
    page = client.get(reverse("mfa_activate_totp"))
    secret = page.context["form"].secret
    return client.post(reverse("mfa_activate_totp"), {"code": totp_code(secret)})


# --- Step 1: invitation page --------------------------------------------------------


def test_invite_page_states_the_rule_and_helps_under_the_field(client):
    board = make_member(board=True)
    token = _invite(PersonFactory(email="step1@example.com"), board)
    client.get(reverse("invite_landing", args=[token]))
    page = client.get(reverse("invite_accept")).content.decode()
    assert "at least 12 characters" in page
    assert 'id="id_password1_help"' in page and 'aria-describedby="id_password1_help"' in page
    assert page.index('id="id_password1"') < page.index('id="id_password1_help"')
    assert 'data-show-password="id_password1 id_password2"' in page
    assert 'aria-current="step"' in page and "Choose a password" in page
    assert "passphrase" not in page.lower()


def test_invite_page_errors_sit_with_the_field(client):
    board = make_member(board=True)
    token = _invite(PersonFactory(email="short@example.com"), board)
    client.get(reverse("invite_landing", args=[token]))
    page = client.post(
        reverse("invite_accept"), {"password1": "short", "password2": "short"}
    ).content.decode()
    assert 'id="id_password1_errors" role="alert"' in page
    assert 'aria-invalid="true"' in page


# --- Step 2: add a passkey or app ---------------------------------------------------


def test_step_two_recommends_a_passkey_and_hides_the_sidebar(new_owner):
    page = new_owner.get(reverse("mfa_index")).content.decode()
    assert "Use this phone's fingerprint or face (passkey)" in page
    assert "Use an authenticator app instead" in page
    assert "What's this?" in page
    assert reverse("mfa_add_webauthn") in page and reverse("mfa_activate_totp") in page
    assert 'aria-current="step">Add a passkey or app' in page
    assert 'Choose a password<span class="sr-only"> (done)' in page
    assert SIDEBAR.decode() not in page
    assert "Two-Factor Authentication" not in page
    assert 'action="/accounts/logout/"' in page  # a way out without the sidebar


def test_step_two_pages_carry_the_step_bar(new_owner):
    for name in ("mfa_activate_totp", "mfa_add_webauthn"):
        page = new_owner.get(reverse(name)).content
        assert b'class="steps setup-steps"' in page, name
        assert SIDEBAR not in page, name


def test_passkey_page_keeps_the_script_hooks_and_defaults_to_sign_in(new_owner):
    page = new_owner.get(reverse("mfa_add_webauthn")).content.decode()
    assert 'id="mfa_webauthn_add"' in page
    assert 'data-allauth-onload="allauth.webauthn.forms.addForm"' in page
    assert re.search(r'id="id_passwordless" checked', page)
    assert '<label for="id_name">' in page


def test_authenticator_app_page_has_a_labelled_code_field(new_owner):
    page = new_owner.get(reverse("mfa_activate_totp")).content.decode()
    assert '<label for="id_code">' in page
    assert 'autocomplete="one-time-code"' in page
    assert "Can't scan it?" in page


def test_wrong_app_code_shows_an_error_and_stays_on_step_two(new_owner):
    page = new_owner.post(reverse("mfa_activate_totp"), {"code": "000000"})
    assert page.status_code == 200
    assert b'id="id_code_errors" role="alert"' in page.content
    assert b'class="steps setup-steps"' in page.content


# --- Step 3: recovery codes, then done ----------------------------------------------


def test_full_setup_ends_with_recovery_codes_and_brings_the_sidebar_back(new_owner):
    response = _activate_totp(new_owner)
    assert response["Location"] == reverse("mfa_view_recovery_codes")
    page = new_owner.get(reverse("mfa_view_recovery_codes")).content.decode()
    assert 'aria-current="step">Save recovery codes' in page
    assert "data-print" in page and ">Print<" in page
    assert "I've saved them" in page
    assert len(re.findall(r"<li><code>", page)) == 10
    assert SIDEBAR.decode() not in page

    response = new_owner.post(reverse("account_setup_done"))
    assert response["Location"] == reverse("home")
    assert enrollment.SESSION_KEY not in new_owner.session
    home = new_owner.get(reverse("home"))
    assert home.status_code == 200 and SIDEBAR in home.content
    # Afterwards the same page is the ordinary one, with the sidebar.
    page = new_owner.get(reverse("mfa_view_recovery_codes")).content
    assert b"I've saved them" not in page and SIDEBAR in page


def test_wandering_off_after_step_two_shows_the_sidebar(new_owner):
    _activate_totp(new_owner)
    assert SIDEBAR in new_owner.get(reverse("home")).content


def test_setup_done_needs_a_passkey_or_app_first(new_owner):
    response = new_owner.post(reverse("account_setup_done"))
    assert response["Location"] == reverse("mfa_index")
    assert new_owner.session.get(enrollment.SESSION_KEY)  # window untouched


def test_setup_done_is_post_only_and_needs_sign_in(client):
    assert client.post(reverse("account_setup_done"))["Location"].startswith(
        reverse("account_login")
    )
    client.force_login(make_member(lots=[LotFactory()]))
    assert client.get(reverse("account_setup_done")).status_code == 405


def test_enrolled_member_sees_the_security_page_not_the_setup(client):
    client.force_login(make_member(lots=[LotFactory()]))
    page = client.get(reverse("mfa_index")).content.decode()
    assert "<h1>Sign-in and security</h1>" in page
    assert "setup-steps" not in page
    assert SIDEBAR.decode() in page
    assert reverse("account_change_password") in page


def test_one_label_for_the_security_page(client):
    client.force_login(make_member(lots=[LotFactory()]))
    for name in ("home", "my_profile"):
        page = client.get(reverse(name)).content.decode()
        assert "Sign-in and security" in page, name
        assert "Passkeys and security" not in page, name
        assert ">Security<" not in page, name


# --- Assisted reset: step 1 is the code ---------------------------------------------


def test_reset_code_page_is_step_one_and_names_who_to_contact(client):
    from accounts import mfa_reset

    contact = _contact()
    first, second = make_member(board=True), make_member(board=True)
    owner = make_member(lots=[LotFactory()])
    reset = mfa_reset.start_reset(
        subject=owner, actor=first, verification_method=VerificationMethod.IN_PERSON
    )
    code = mfa_reset.approve_reset(reset, actor=second)
    owner.refresh_from_db()  # approval bumped session_generation
    client.force_login(owner)
    page = client.get(reverse("enroll_with_code")).content.decode()
    assert 'aria-current="step">Enter your code' in page
    assert contact in page
    assert SIDEBAR.decode() not in page
    response = client.post(reverse("enroll_with_code"), {"code": code})
    assert response["Location"] == reverse("mfa_index")
    page = client.get(reverse("mfa_index")).content.decode()
    assert 'Enter your code<span class="sr-only"> (done)' in page
    assert "Use this phone's fingerprint or face (passkey)" in page


# --- Login page and sign-in help ----------------------------------------------------


def test_login_page_has_labels_and_the_passkey_first(client):
    page = client.get(reverse("account_login")).content.decode()
    assert '<label for="id_login">Email</label>' in page
    assert '<label for="id_password">Password</label>' in page
    assert page.index('id="passkey_login"') < page.index('id="id_login"')
    assert "Sign in with a passkey" in page
    assert "You'll still need your authenticator app or passkey." in page
    # D62: one "Can't sign in?" link instead of the two separate ones.
    assert reverse("signin_help") in page and "Can't sign in?" in page
    assert "placeholder=" not in page


def test_wrong_password_error_is_announced(client):
    owner = make_member(lots=[LotFactory()], email="pw@example.com")
    page = client.post(
        reverse("account_login"), {"login": owner.email, "password": WRONG}
    ).content.decode()
    assert 'class="alert danger" role="alert"' in page


def test_signin_help_is_open_and_explains_the_two_board_member_reset(client):
    page = client.get(reverse("signin_help"))
    assert page.status_code == 200
    assert "_auth_user_id" not in client.session
    # D62: the board-assisted reset is explained on "I can't get into my email".
    text = client.get(reverse("signin_help_email")).content.decode()
    assert "Two board members" in text
    assert "Contact a board member" in text
    assert "recovery codes" in client.get(reverse("signin_help_device")).content.decode()


def test_signin_help_names_the_contact_from_community_settings(client):
    contact = _contact()
    for name in ("signin_help_setup", "signin_help_email"):
        assert contact in client.get(reverse(name)).content.decode(), name


def test_board_sets_the_signin_contact_on_community_settings(client):
    board = make_member(board=True)
    client.force_login(board)
    assert b"signin_help_contact" in client.get(reverse("community_settings")).content
    profile = CommunityProfile.get()
    client.post(
        reverse("community_profile_save"),
        {
            "legal_name": profile.legal_name,
            "fiscal_year_start_month": 1,
            "signin_help_contact": "  the  board  at board@example.org ",
        },
    )
    assert CommunityProfile.get().signin_help_contact == "the board at board@example.org"


def test_owner_cannot_set_the_signin_contact(client):
    client.force_login(make_member(lots=[LotFactory()]))
    response = client.post(
        reverse("community_profile_save"),
        {"legal_name": "x", "fiscal_year_start_month": 1, "signin_help_contact": "me"},
    )
    assert response.status_code == 403
    assert CommunityProfile.get().signin_help_contact == ""


def test_expired_setup_page_names_who_to_contact(client):
    contact = _contact()
    page = client.get(reverse("account_setup_expired")).content.decode()
    assert contact in page and "1 hour" in page


def test_second_step_page_mentions_recovery_codes(client):
    from community.tests.factories import enroll_totp

    owner = make_member(lots=[LotFactory()], email="second@example.com", mfa=False)
    owner.set_password(PASSWORD)
    owner.save()
    enroll_totp(owner)
    client.post(reverse("account_login"), {"login": owner.email, "password": PASSWORD})
    page = client.get(reverse("mfa_authenticate")).content.decode()
    assert "recovery codes" in page and reverse("signin_help") in page
    assert '<label for="id_code">' in page


# --- Lockout ------------------------------------------------------------------------


def test_lockout_wait_comes_from_the_setting(client, settings):
    from accounts.templatetags.signin import duration_words, lockout_wait

    assert duration_words(datetime.timedelta(minutes=30)) == "30 minutes"
    assert duration_words(datetime.timedelta(hours=2)) == "2 hours"
    assert duration_words(datetime.timedelta(minutes=90)) == "1 hour 30 minutes"
    settings.AXES_COOLOFF_TIME = 2.0
    assert lockout_wait() == "2 hours"
    owner = make_member(lots=[LotFactory()], email="locked@example.com")
    for _ in range(5):
        client.post(reverse("account_login"), {"login": owner.email, "password": WRONG})
    page = client.post(reverse("account_login"), {"login": owner.email, "password": PASSWORD})
    assert page.status_code == 429
    assert b"try again in 2 hours" in page.content


def test_help_page_links_signed_in_people_home_not_to_login(client):
    from community.tests.factories import LotFactory, make_member

    client.force_login(make_member(lots=[LotFactory()]))
    page = client.get(reverse("signin_help")).content.decode()
    assert "Back to the portal" in page and 'href="/accounts/login/"' not in page
