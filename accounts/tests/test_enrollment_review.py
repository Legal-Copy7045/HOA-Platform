"""Invitation and enrollment review (2026-11-26, decision D59).

Every login ends up with a passkey or authenticator app, and nothing serves
portal data to a login without one. A login that signed in but never added
one can be sent a new setup link by the board; anyone who ever had one needs
the two-board-member reset.
"""

import datetime
import re
from unittest import mock

import pytest
from django.core import mail
from django.test import Client
from django.urls import get_resolver, resolve, reverse
from django.utils import timezone

from accounts import enrollment, services
from accounts.middleware import ALLOWED_URL_NAMES, EXPIRED_ALLOWED_URL_NAMES
from accounts.models import AccountRecovery, Invitation, MfaReset, User, VerificationMethod
from accounts.tests.helpers import PASSWORD, totp_code
from audit.models import AuditEvent
from audit.services import record_event
from community.models import Person, Role
from community.tests.factories import LotFactory, PersonFactory, enroll_totp, make_member
from core.my_items import items_for
from core.scoping import system_access

pytestmark = pytest.mark.django_db

NEW_PASSWORD = "another long passphrase 77"  # nosec B105 - test-only

# Reviewed 2026-11-26: the pages a login without a passkey or app may open
# while its setup window is open. None of them shows portal data: they act
# only on the signed-in login's own sign-in (allauth) or say up/down.
REVIEWED_SETUP_PAGES = {
    "account_logout",
    "account_reauthenticate",
    "account_login",
    "account_change_password",
    "healthz",
    "enroll_with_code",
}
# Reachable once the window has closed (the login is signed out first).
REVIEWED_EXPIRED_PAGES = {"account_logout", "healthz", "account_setup_expired"}


def _invite(person, board):
    with mock.patch("accounts.tasks.send_invitation.defer"):
        invitation = services.create_invitation(person=person, actor=board)
    mail.outbox = []
    services.send_invitation_email(invitation.pk)
    return invitation, re.search(r"/invite/([^/\s]+)/", mail.outbox[-1].body).group(1)


def _accept(client, token, password=PASSWORD):
    client.get(reverse("invite_landing", args=[token]))
    return client.post(reverse("invite_accept"), {"password1": password, "password2": password})


@pytest.fixture
def board():
    return make_member(board=True)


@pytest.fixture
def stuck(board):
    """An owner who accepted the invitation and closed the browser before
    adding a passkey or app."""
    person = PersonFactory(email="stuck@example.com")
    from community import services as community
    from core.policy import SYSTEM

    community.record_ownership(
        person=person, lot=LotFactory(), start_date=datetime.date(2020, 1, 1), actor=SYSTEM
    )
    _, token = _invite(person, board)
    browser = Client()
    assert _accept(browser, token)["Location"] == reverse("mfa_index")
    with system_access("t"):
        person.refresh_from_db()
    return person


# --- The guarantee: every URL --------------------------------------------------------

# allauth names whose path we answer with an unnamed 404 view (core/urls.py).
UNBUILDABLE = {"account_email", "account_signup"}
SAMPLES = ("1", "approve", "x", "2026-11", "2026", "a1b2", "file.pdf", "t" * 32)


def _path_for(name: str) -> str | None:
    """A path that resolves to ``name``, with sample values for its arguments."""
    for possibilities, pattern, _defaults, _converters in get_resolver().reverse_dict.getlist(name):
        for fmt, params in possibilities:
            values = {}
            for p in params:
                group = re.search(rf"\(\?P<{p}>((?:[^()]|\([^()]*\))*)\)", pattern)
                regex = group.group(1) if group else "[^/]+"
                first_word = re.match(r"\(?\??:?([A-Za-z_]+)", regex)
                candidates = ([first_word.group(1)] if first_word else []) + list(SAMPLES)
                values[p] = next((c for c in candidates if re.fullmatch(regex, c)), "1")
            path = "/" + fmt % values
            try:
                if resolve(path).url_name == name:
                    return path
            except Exception:  # noqa: S112 - try the next form  # nosec B112
                continue
    return None


def _all_names() -> list[str]:
    return sorted(n for n in get_resolver().reverse_dict if isinstance(n, str))


def test_the_pages_open_without_an_authenticator_are_the_reviewed_ones():
    """Adding a page here is a security decision: review it first (D59)."""
    assert ALLOWED_URL_NAMES == REVIEWED_SETUP_PAGES
    assert EXPIRED_ALLOWED_URL_NAMES == REVIEWED_EXPIRED_PAGES
    allauth_mfa = {n for n in _all_names() if n.startswith("mfa_")}
    # Every "mfa_" page is allauth's own (or our subclass of it), about the
    # signed-in login's authenticators only. `accounts.recovery_codes` (D58)
    # shows, prints or downloads the signed-in login's own recovery codes.
    for name in allauth_mfa:
        assert resolve(_path_for(name)).func.__module__.startswith(
            ("allauth.mfa", "accounts.mfa_views", "accounts.recovery_codes")
        ), name


def test_every_url_needs_a_passkey_or_app(client):
    """Signed in with a password but no passkey or app, inside the setup
    window: every page except the reviewed setup pages sends them to step 2."""
    user = make_member(lots=[LotFactory()], mfa=False)
    client.force_login(user)
    session = client.session
    session[enrollment.SESSION_KEY] = 2**40
    session.save()
    unbuilt, leaked = [], []
    for name in _all_names():
        if name.startswith("mfa_") or name in REVIEWED_SETUP_PAGES:
            continue
        path = _path_for(name)
        if path is None:
            unbuilt.append(name)
            continue
        response = client.get(path)
        if response.status_code != 302 or response["Location"] != reverse("mfa_index"):
            leaked.append((name, response.status_code))
    assert leaked == []
    # Paths that cannot be built here are still covered: an unknown path is
    # refused the same way (below), whatever it would have resolved to.
    assert set(unbuilt) <= UNBUILDABLE, unbuilt
    assert client.get("/no-such-page/")["Location"] == reverse("mfa_index")


def test_every_url_signs_out_a_login_with_no_window():
    user = make_member(lots=[LotFactory()], mfa=False)
    browser = Client()
    for name in _all_names():
        if name in REVIEWED_EXPIRED_PAGES:
            continue
        path = _path_for(name)
        if path is None:
            continue
        browser.force_login(user)
        response = browser.get(path)
        assert response.status_code == 302, name
        assert response["Location"] == reverse("account_setup_expired"), name
        assert "_auth_user_id" not in browser.session, name


def test_signing_out_without_a_window_is_audited(client):
    user = make_member(lots=[LotFactory()], mfa=False)
    client.force_login(user)
    client.get(reverse("home"))
    event = AuditEvent.objects.filter(action="auth.setup_window_closed").latest("id")
    assert event.actor == user and event.metadata == {"reason": "none"}

    client.force_login(user)
    session = client.session
    session[enrollment.SESSION_KEY] = 1  # long past
    session.save()
    client.get(reverse("home"))
    event = AuditEvent.objects.filter(action="auth.setup_window_closed").latest("id")
    assert event.metadata == {"reason": "expired"}


def test_emailed_sign_in_code_never_opens_a_login_without_an_authenticator(client):
    user = make_member(lots=[LotFactory()], email="nomfa@example.com", mfa=False)
    user.set_password(PASSWORD)
    user.save()
    client.post(reverse("account_request_login_code"), {"email": user.email})
    code = re.search(r"\b([A-Z0-9]{4}-[A-Z0-9]{4})\b", mail.outbox[-1].body).group(1)
    client.post(reverse("account_confirm_login_code"), {"code": code})
    response = client.get(reverse("home"))
    assert response["Location"] == reverse("account_setup_expired")
    assert "_auth_user_id" not in client.session


def test_password_sign_in_never_opens_a_login_without_an_authenticator(client):
    user = make_member(lots=[LotFactory()], email="nomfa2@example.com", mfa=False)
    user.set_password(PASSWORD)
    user.save()
    for name in ("home", "my_items", "notice_choice", "mfa_index"):
        client.post(reverse("account_login"), {"login": user.email, "password": PASSWORD})
        assert client.get(reverse(name))["Location"] == reverse("account_setup_expired")


def test_notice_step_and_dashboard_need_the_authenticator_first(client, board):
    person = PersonFactory(email="order@example.com")
    _, token = _invite(person, board)
    _accept(client, token)
    for name in ("home", "my_items", "notice_choice", "account_setup_done"):
        response = client.get(reverse(name)) if name != "account_setup_done" else None
        if response is not None:
            assert response["Location"] == reverse("mfa_index"), name
    assert client.post(reverse("account_setup_done"))["Location"] == reverse("mfa_index")


def test_totp_is_offered_on_the_passkey_page_during_setup(client, board):
    person = PersonFactory(email="nopasskey@example.com")
    _, token = _invite(person, board)
    _accept(client, token)
    page = client.get(reverse("mfa_add_webauthn")).content.decode()
    assert reverse("mfa_activate_totp") in page and "authenticator app instead" in page
    secret = client.get(reverse("mfa_activate_totp")).context["form"].secret
    response = client.post(reverse("mfa_activate_totp"), {"code": totp_code(secret)})
    assert response["Location"] == reverse("mfa_view_recovery_codes")
    assert client.get(reverse("home")).status_code == 200


# --- The emailed link ------------------------------------------------------------


def test_link_is_dead_once_the_persons_email_changes(client, board):
    person = PersonFactory(email="before@example.com")
    _, token = _invite(person, board)
    with system_access("t"):
        Person.objects.filter(pk=person.pk).update(email="after@example.com")
    assert client.get(reverse("invite_landing", args=[token])).status_code == 404
    # Also a browser that opened it before the change.
    other = Client()
    with system_access("t"):
        Person.objects.filter(pk=person.pk).update(email="before@example.com")
    other.get(reverse("invite_landing", args=[token]))
    with system_access("t"):
        Person.objects.filter(pk=person.pk).update(email="after@example.com")
    assert other.get(reverse("invite_accept")).status_code == 404


def test_link_works_once_even_in_two_browsers(board):
    person = PersonFactory(email="twice@example.com")
    _, token = _invite(person, board)
    first, second = Client(), Client()
    first.get(reverse("invite_landing", args=[token]))
    second.get(reverse("invite_landing", args=[token]))
    assert _accept(first, token)["Location"] == reverse("mfa_index")
    response = second.post(reverse("invite_accept"), {"password1": PASSWORD, "password2": PASSWORD})
    assert response.status_code == 404
    assert Client().get(reverse("invite_landing", args=[token])).status_code == 404


def test_accepting_cycles_the_session_key(client, board):
    person = PersonFactory(email="fixation@example.com")
    _, token = _invite(person, board)
    client.get(reverse("account_login"))
    before = client.session.session_key
    client.get(reverse("invite_landing", args=[token]))
    landed = client.session.session_key
    client.post(reverse("invite_accept"), {"password1": PASSWORD, "password2": PASSWORD})
    assert len({before, landed, client.session.session_key}) == 3


# --- Stuck without an authenticator: a new setup link ---------------------------------


def test_closing_the_browser_midway_leaves_the_login_stuck(stuck):
    browser = Client()
    browser.post(reverse("account_login"), {"login": stuck.email, "password": PASSWORD})
    response = browser.get(reverse("home"))
    assert response["Location"] == reverse("account_setup_expired")
    page = browser.get(reverse("account_setup_expired")).content.decode()
    assert "Your setup link expired" in page
    assert "new setup link" in page and "sign-in reset" in page


def test_board_sends_a_new_setup_link_and_it_finishes_the_setup(stuck, board):
    old_session = Client()
    old_session.force_login(stuck.user)
    invitation, token = _invite(stuck, board)
    created = AuditEvent.objects.filter(action="invite.created").latest("id")
    assert created.metadata == {"person_id": stuck.pk, "setup_again": True}
    assert invitation.email == stuck.email  # only the address on record
    browser = Client()
    assert _accept(browser, token, NEW_PASSWORD)["Location"] == reverse("mfa_index")
    user = User.objects.get(pk=stuck.user_id)
    assert user.check_password(NEW_PASSWORD)
    assert browser.session.get(enrollment.SESSION_KEY)
    accepted = AuditEvent.objects.filter(action="invite.accepted").latest("id")
    assert accepted.metadata == {"person_id": stuck.pk, "setup_again": True}
    # Any other session of the login ended.
    assert old_session.get(reverse("mfa_index"))["Location"].startswith(reverse("account_login"))
    # Same Person, same login: nothing new granted.
    assert User.objects.filter(email=stuck.email).count() == 1
    secret = browser.get(reverse("mfa_activate_totp")).context["form"].secret
    browser.post(reverse("mfa_activate_totp"), {"code": totp_code(secret)})
    assert browser.get(reverse("home")).status_code == 200


def test_the_old_link_dies_when_a_new_setup_link_is_sent(stuck, board):
    _, first = _invite(stuck, board)
    _invite(stuck, board)
    assert Client().get(reverse("invite_landing", args=[first])).status_code == 404


@pytest.mark.parametrize(
    "why",
    ["enrolled", "had_one", "reset", "recovery", "recovery_cancelled", "email", "inactive"],
)
def test_no_setup_link_where_a_reset_is_needed(stuck, board, why):
    user = stuck.user
    if why == "enrolled":
        enroll_totp(user)
    elif why == "had_one":
        record_event("mfa.authenticator_added", actor=user, target=user, metadata={"type": "totp"})
    elif why == "reset":
        with system_access("t"):
            MfaReset.objects.create(
                subject=user,
                verification_method=VerificationMethod.IN_PERSON,
                started_by=board,
                request_expires_at=timezone.now() + datetime.timedelta(days=1),
            )
    elif why in ("recovery", "recovery_cancelled"):
        # Any self-service account recovery on record (D58), open or not,
        # counts as a reset: the two repair paths never overlap.
        now = timezone.now()
        with system_access("t"):
            AccountRecovery.objects.create(
                subject=user,
                wait_hours=48,
                lock_hours=48,
                ready_at=now + datetime.timedelta(hours=48),
                expires_at=now + datetime.timedelta(hours=72),
                cancelled_at=now if why == "recovery_cancelled" else None,
            )
    elif why == "email":
        with system_access("t"):
            Person.objects.filter(pk=stuck.pk).update(email="changed@example.com")
            stuck.refresh_from_db()
    else:
        User.objects.filter(pk=user.pk).update(is_active=False)
    assert services.setup_again_refusal(stuck)
    with pytest.raises(services.InvitationError):
        services.create_invitation(person=stuck, actor=board)


def test_accepting_refuses_if_the_login_enrolled_meanwhile(stuck, board):
    _, token = _invite(stuck, board)
    enroll_totp(stuck.user)
    browser = Client()
    browser.get(reverse("invite_landing", args=[token]))
    response = browser.post(
        reverse("invite_accept"), {"password1": NEW_PASSWORD, "password2": NEW_PASSWORD}
    )
    assert response.status_code == 200 and b"no longer valid" in response.content
    assert "_auth_user_id" not in browser.session
    assert not User.objects.get(pk=stuck.user_id).check_password(NEW_PASSWORD)


def test_only_the_board_sends_a_setup_link(client, stuck):
    sysadmin = make_member(roles=[Role.SYSADMIN])
    client.force_login(sysadmin)
    assert client.post(reverse("invite_person", args=[stuck.pk])).status_code in (403, 404)
    with system_access("t"):
        assert not Invitation.objects.filter(person=stuck).pending().exists()


def test_signups_show_the_stuck_login_with_a_setup_link_button(client, stuck, board):
    client.force_login(board)
    assert {r["person"].pk: r["status"] for r in services.onboarding_rows()}[
        stuck.pk
    ] == "no_authenticator"
    page = client.get(reverse("lot_list"), {"view": "signups"}).content.decode()
    assert "Signed in, no passkey or app yet" in page
    assert "Send a new setup link" in page
    page = client.get(reverse("lot_list"), {"view": "signups", "status": "no_authenticator"})
    assert stuck.display_name in page.content.decode()
    with mock.patch("accounts.tasks.send_invitation.defer"):
        response = client.post(reverse("invite_person", args=[stuck.pk]), {"back": "signups"})
    assert response["Location"].startswith(reverse("lot_list") + "?view=signups")
    page = client.get(reverse("lot_list"), {"view": "signups"}).content.decode()
    assert "New setup link queued" in page


def test_person_page_offers_the_setup_link_or_the_reset(client, stuck, board):
    client.force_login(board)
    page = client.get(reverse("person_detail", args=[stuck.pk])).content.decode()
    assert "Send a new setup link" in page
    record_event(
        "mfa.authenticator_added", actor=stuck.user, target=stuck.user, metadata={"type": "totp"}
    )
    page = client.get(reverse("person_detail", args=[stuck.pk])).content.decode()
    assert "Send a new setup link" not in page and "Start a sign-in reset" in page


def test_board_my_items_lists_logins_stuck_for_a_week(stuck, board):
    def titles():
        return [i.title for i in items_for(board) if "can't sign in yet" in i.title]

    assert titles() == []  # accepted today
    User.objects.filter(pk=stuck.user_id).update(
        date_joined=timezone.now() - datetime.timedelta(days=8)
    )
    with system_access("t"):
        Invitation.objects.filter(person=stuck).update(
            accepted_at=timezone.now() - datetime.timedelta(days=8)
        )
    assert titles() == [f"{stuck.display_name} can't sign in yet"]
    owner = make_member(lots=[LotFactory()])
    assert not [i for i in items_for(owner) if "can't sign in yet" in i.title]
    _invite(stuck, board)  # a new setup link is out
    assert titles() == []
