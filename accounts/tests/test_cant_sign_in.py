"""Can't sign in? (decision D62): one entry point on the sign-in page that
asks what's wrong and routes to the existing flows. Nothing it shows or
does reveals whether an account exists."""

import datetime
import re
from unittest import mock

import pytest
from django.contrib.messages import get_messages
from django.urls import reverse

from accounts import setup_requests
from accounts.models import Invitation
from accounts.signin_help import PROBLEMS, RECOVERY_CODE_NOTE
from audit.actions import ACTIONS
from audit.models import AuditEvent
from community.models import Person
from community.tests.factories import LotFactory, make_member
from core.my_items import items_for
from core.scoping import system_access

pytestmark = pytest.mark.django_db

WRONG = "not the password"  # nosec B105 - test-only

HELP_PAGES = (
    "signin_help",
    "signin_help_device",
    "signin_help_setup",
    "signin_help_email",
    "signin_help_locked",
)


def _login(email, *, mfa):
    """A login whose Person record carries the same email (as after an invitation)."""
    user = make_member(lots=[LotFactory()], email=email, mfa=mfa)
    with system_access("t"):
        Person.objects.filter(user=user).update(email=email)
    return user


def _person(user):
    with system_access("t"):
        return Person.objects.get(user=user)


def _ask(client, email):
    with mock.patch("accounts.tasks.record_setup_link_request.defer") as defer:
        response = client.post(reverse("signin_help_setup"), {"email": email})
    return response, defer


def _ask_and_run(client, email, capture):
    """Post the form and run the queued job, as the worker would."""
    with (
        mock.patch(
            "accounts.tasks.record_setup_link_request.defer",
            side_effect=lambda user_id: setup_requests.record_request(user_id),
        ),
        capture(execute=True),
    ):
        return client.post(reverse("signin_help_setup"), {"email": email})


def _board_titles(board):
    return [i.title for i in items_for(board)]


# --- The sign-in page ---------------------------------------------------------------


def test_sign_in_page_has_exactly_one_help_link(client):
    page = client.get(reverse("account_login")).content.decode()
    assert page.count(f'href="{reverse("signin_help")}"') == 1
    assert "Can't sign in?" in page
    assert "Forgot your password?" not in page and "Lost your phone?" not in page
    assert f'href="{reverse("account_reset_password")}"' not in page


def test_lockout_page_links_to_cant_sign_in(client, settings):
    owner = make_member(lots=[LotFactory()], email="lock@example.com")
    for _ in range(settings.AXES_FAILURE_LIMIT):
        client.post(reverse("account_login"), {"login": owner.email, "password": WRONG})
    page = client.post(reverse("account_login"), {"login": owner.email, "password": WRONG})
    assert page.status_code == 429
    assert reverse("signin_help").encode() in page.content
    assert b"Can't sign in?" in page.content


# --- The question and where each answer goes -----------------------------------------


def test_help_page_asks_what_is_wrong_with_a_fieldset_and_labels(client):
    response = client.get(reverse("signin_help"))
    assert response.status_code == 200
    page = response.content.decode()
    assert "<h1>Can't sign in?</h1>" in page
    assert '<fieldset class="choices"' in page and "<legend>What's wrong?</legend>" in page
    assert page.count('type="radio" name="problem"') == len(PROBLEMS) == 5
    for value, label, _ in PROBLEMS:
        assert f'<label class="choice" for="problem_{value}">' in page
        assert f'id="problem_{value}"' in page
        assert label.replace("'", "&#x27;") in page
    assert 'method="get"' in page
    assert "_auth_user_id" not in client.session


@pytest.mark.parametrize(
    ("problem", "target"),
    [
        ("password", "account_reset_password"),
        ("device", "signin_help_device"),
        ("setup", "signin_help_setup"),
        ("email", "signin_help_email"),
        ("locked", "signin_help_locked"),
    ],
)
def test_each_option_routes_to_its_page(client, problem, target):
    response = client.get(reverse("signin_help"), {"problem": problem})
    assert response.status_code == 302
    assert response["Location"] == reverse(target)
    assert client.get(response["Location"]).status_code == 200


def test_an_unknown_choice_asks_again(client):
    response = client.get(reverse("signin_help"), {"problem": "../admin"})
    assert response.status_code == 200
    page = response.content.decode()
    assert "Choose one of the options." in page and 'role="alert"' in page
    assert 'aria-describedby="problem_error"' in page


def test_password_option_is_the_existing_reset(client):
    page = client.get(reverse("account_reset_password"))
    assert page.status_code == 200


# --- Lost phone, passkey or app ----------------------------------------------------


def test_device_page_asks_about_a_recovery_code_or_other_device(client):
    page = client.get(reverse("signin_help_device")).content.decode()
    assert "<legend>Do you have a recovery code, or another device with your passkey?</legend>" in (
        page
    )
    assert '<label class="choice" for="have_yes">' in page
    assert '<label class="choice" for="have_no">' in page


def test_yes_goes_back_to_sign_in_with_the_recovery_code_note(client):
    response = client.get(reverse("signin_help_device"), {"have": "yes"})
    assert response.status_code == 302 and response["Location"] == reverse("account_login")
    assert [str(m) for m in get_messages(response.wsgi_request)] == [RECOVERY_CODE_NOTE]
    page = client.get(reverse("account_login")).content.decode()
    assert "type one of your recovery codes instead" in page
    assert "Sign in with a passkey" in page


def test_no_starts_the_existing_account_recovery(client):
    response = client.get(reverse("signin_help_device"), {"have": "no"})
    assert response.status_code == 302 and response["Location"] == reverse("recovery_request")
    page = client.get(response["Location"]).content.decode()
    assert "Recover your sign-in" in page and "two board members" in page


def test_device_page_asks_again_on_an_unknown_answer(client):
    response = client.get(reverse("signin_help_device"), {"have": "maybe"})
    assert response.status_code == 200 and b"Choose one of the options." in response.content


# --- Setup link expired or never finished ---------------------------------------------


def test_setup_page_explains_and_offers_ask_the_board(client):
    page = client.get(reverse("signin_help_setup")).content.decode()
    assert "new setup link" in page and "Ask the board" in page
    assert '<label for="id_email">Your email address</label>' in page


def test_setup_request_answers_every_address_the_same_way(
    client, django_capture_on_commit_callbacks
):
    enrolled = _login("enrolled@example.com", mfa=True)
    never = _login("never@example.com", mfa=False)
    pages = []
    with mock.patch("accounts.tasks.record_setup_link_request.defer") as defer:
        for email in ("nobody@example.com", enrolled.email, never.email):
            with django_capture_on_commit_callbacks(execute=True):
                pages.append(client.post(reverse("signin_help_setup"), {"email": email}))
    assert {p.status_code for p in pages} == {200}
    assert pages[0].content == pages[1].content == pages[2].content
    assert b"If that address belongs to an account" in pages[0].content
    for p in pages:
        assert b"@example.com" not in p.content
    # One job each, carrying only a login id (0 for unknown).
    assert [c.kwargs for c in defer.call_args_list] == [
        {"user_id": 0},
        {"user_id": enrolled.pk},
        {"user_id": never.pk},
    ]
    assert "_auth_user_id" not in client.session


def test_board_item_only_for_a_login_that_never_enrolled(
    client, django_capture_on_commit_callbacks, mailoutbox
):
    board = make_member(board=True)
    enrolled = _login("enrolled@example.com", mfa=True)
    never = _login("never@example.com", mfa=False)
    for email in ("nobody@example.com", enrolled.email):
        _ask_and_run(client, email, django_capture_on_commit_callbacks)
    assert not AuditEvent.objects.filter(action=setup_requests.ACTION).exists()
    assert not [t for t in _board_titles(board) if "asked for a new setup link" in t]
    _ask_and_run(client, never.email, django_capture_on_commit_callbacks)
    person = _person(never)
    assert f"{person.display_name} asked for a new setup link" in _board_titles(board)
    # Nobody is emailed: the page already told the person what happens next.
    assert mailoutbox == []
    item = next(i for i in items_for(board) if "asked for a new setup link" in i.title)
    assert item.url == reverse("person_detail", args=[person.pk])
    # The owner never sees it.
    assert not [t for t in _board_titles(enrolled) if "setup link" in t]


def test_audit_event_carries_ids_only_and_only_when_it_matched(
    client, django_capture_on_commit_callbacks
):
    never = _login("never@example.com", mfa=False)
    _ask_and_run(client, never.email, django_capture_on_commit_callbacks)
    events = list(AuditEvent.objects.filter(action=setup_requests.ACTION))
    assert len(events) == 1
    event = events[0]
    person = _person(never)
    assert event.metadata == {"person_id": person.pk, "user_id": never.pk}
    assert set(event.metadata) <= ACTIONS[setup_requests.ACTION].metadata_fields
    assert event.target_id == str(person.pk) and event.subject_hash == ""
    # Asking again while it is open records nothing more.
    _ask_and_run(client, never.email, django_capture_on_commit_callbacks)
    assert AuditEvent.objects.filter(action=setup_requests.ACTION).count() == 1
    assert setup_requests.record_request(0) == "unknown"
    assert setup_requests.record_request(never.pk) == "already_open"


def test_item_goes_away_once_a_new_setup_link_is_sent(client, django_capture_on_commit_callbacks):
    from accounts import services

    board = make_member(board=True)
    never = _login("never@example.com", mfa=False)
    _ask_and_run(client, never.email, django_capture_on_commit_callbacks)
    person = _person(never)
    title = f"{person.display_name} asked for a new setup link"
    assert title in _board_titles(board)
    with mock.patch("accounts.tasks.send_invitation.defer"):
        services.create_invitation(person=person, actor=board)
    assert title not in _board_titles(board)
    assert Invitation.objects.unscoped("t").filter(person=person).exists()


def test_item_expires_and_skips_logins_that_enrolled_since(
    client, django_capture_on_commit_callbacks
):
    from community.tests.factories import enroll_totp

    never = _login("never@example.com", mfa=False)
    _ask_and_run(client, never.email, django_capture_on_commit_callbacks)
    assert setup_requests.open_requests()
    enroll_totp(never)
    assert setup_requests.open_requests() == []
    later = _login("later@example.com", mfa=False)
    _ask_and_run(client, later.email, django_capture_on_commit_callbacks)
    assert [p.pk for p, _ in setup_requests.open_requests()] == [_person(later).pk]
    with mock.patch(
        "accounts.setup_requests.timezone.now",
        return_value=datetime.datetime.now(datetime.UTC)
        + datetime.timedelta(days=setup_requests.OPEN_DAYS + 1),
    ):
        assert setup_requests.open_requests() == []


def test_staff_login_that_never_enrolled_is_handled_like_d59(
    client, django_capture_on_commit_callbacks
):
    """The board may send any never-enrolled login a new setup link (D59), so
    the request is recorded for them too; the page is the same either way."""
    staff = make_member(board=True, email="director@example.com", mfa=False)
    with system_access("t"):
        Person.objects.filter(user=staff).update(email=staff.email)
    page = _ask_and_run(client, staff.email, django_capture_on_commit_callbacks)
    assert page.status_code == 200
    assert AuditEvent.objects.filter(action=setup_requests.ACTION).count() == 1


def test_setup_request_is_rate_limited_per_address(client):
    codes = [_ask(client, "x@example.com")[0].status_code for _ in range(4)]
    assert codes[:3] == [200, 200, 200] and codes[3] == 429


def test_setup_request_rate_limit_is_the_same_for_every_address(client):
    never = _login("never@example.com", mfa=False)
    unknown = [_ask(client, "nobody@example.com")[0].status_code for _ in range(4)]
    known = [_ask(client, never.email)[0].status_code for _ in range(4)]
    assert unknown == known == [200, 200, 200, 429]


def test_an_invalid_address_shows_the_form_again(client):
    response, defer = _ask(client, "not an email")
    assert response.status_code == 200
    assert b'aria-invalid="true"' in response.content
    defer.assert_not_called()


def test_signed_in_people_are_sent_home_from_the_setup_form(client):
    client.force_login(make_member(lots=[LotFactory()]))
    assert client.get(reverse("signin_help_setup"))["Location"] == reverse("home")


def test_expired_setup_page_offers_ask_the_board(client):
    page = client.get(reverse("account_setup_expired")).content.decode()
    assert reverse("signin_help_setup") in page


# --- No email any more --------------------------------------------------------------


def test_no_email_page_is_board_assisted_only(client):
    from community.models import CommunityProfile

    profile = CommunityProfile.get()
    profile.signin_help_contact = "the board at board@oakcreek.test"
    profile.save()
    response = client.get(reverse("signin_help_email"))
    page = response.content.decode()
    assert response.status_code == 200
    assert "can't be used to prove who you are" in page
    assert "Two board members" in page and "never by email" in page
    assert "the board at board@oakcreek.test" in page
    # Nothing here changes anything: no form at all.
    assert "<form" not in page


# --- Locked out ---------------------------------------------------------------------


def test_locked_page_states_the_configured_waits(client, settings):
    settings.AXES_COOLOFF_TIME = 2.0
    settings.AXES_FAILURE_LIMIT = 7
    page = client.get(reverse("signin_help_locked")).content.decode()
    assert "After 7 wrong passwords in a row" in page
    assert "<strong>2 hours</strong>" in page
    # allauth's limits, from ACCOUNT_RATE_LIMITS["login_failed"] (10/m/ip,20/h/key).
    assert "10 failed sign-ins from one phone or computer within 1 minute" in page
    assert "20 failed sign-ins for one email address within 1 hour" in page
    assert f"after {settings.ACCOUNT_LOGIN_BY_CODE_MAX_ATTEMPTS} wrong tries" in page
    assert f'href="{reverse("account_login")}">Try again</a>' in page


def test_locked_page_follows_the_default_setting(client):
    page = client.get(reverse("signin_help_locked")).content.decode()
    assert "<strong>30 minutes</strong>" in page


# --- No enumeration, no data, old links ----------------------------------------------


@pytest.mark.parametrize("name", HELP_PAGES)
def test_help_pages_say_nothing_about_accounts(client, name):
    _login("never@example.com", mfa=False)
    response = client.get(reverse(name))
    assert response.status_code == 200
    body = response.content.decode()
    assert "@example.com" not in body
    assert not re.search(r"Test Person \d+", body)
    assert "_auth_user_id" not in client.session


def test_old_direct_urls_still_work(client):
    for name in ("signin_help", "account_reset_password", "recovery_request"):
        assert client.get(reverse(name)).status_code == 200, name
    assert reverse("signin_help") == "/sign-in-help/"
    assert reverse("recovery_request") == "/sign-in-help/recover/"
