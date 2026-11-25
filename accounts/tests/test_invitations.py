"""Invitations: invite-only accounts, single-use expiring links (D4, D5)."""

import re
from datetime import timedelta
from unittest import mock

import pytest
from allauth.account.models import EmailAddress
from django.core import mail
from django.core.exceptions import PermissionDenied
from django.urls import reverse
from django.utils import timezone

from accounts import services
from accounts.models import Invitation, User
from accounts.tests.helpers import PASSWORD
from audit.models import AuditEvent
from community.models import Lot, Person, Role
from community.services import record_ownership
from community.tests.factories import LotFactory, PersonFactory, days_ago, make_member
from core import policy
from core.policy import SYSTEM
from core.scoping import system_access

pytestmark = pytest.mark.django_db


@pytest.fixture
def board():
    return make_member(board=True)


def invite_and_send(person, actor):
    with mock.patch("accounts.tasks.send_invitation.defer"):
        invitation = services.create_invitation(person=person, actor=actor)
    mail.outbox = []
    services.send_invitation_email(invitation.pk)
    token = re.search(r"/invite/([^/\s]+)/", mail.outbox[-1].body).group(1)
    return invitation, token


def accept(client, token, password=PASSWORD):
    client.get(reverse("invite_landing", args=[token]))
    return client.post(reverse("invite_accept"), {"password1": password, "password2": password})


# --- Who may invite ---------------------------------------------------------------


@pytest.mark.parametrize("who", ["board", "sysadmin"])
def test_board_and_sysadmin_can_invite(who, django_capture_on_commit_callbacks):
    actor = make_member(board=True) if who == "board" else make_member(roles=[Role.SYSADMIN])
    with (
        mock.patch("accounts.tasks.send_invitation.defer") as defer,
        django_capture_on_commit_callbacks(execute=True),
    ):
        invitation = services.create_invitation(person=PersonFactory(), actor=actor)
    defer.assert_called_once_with(invitation_id=invitation.pk)  # only the ID is queued
    assert AuditEvent.objects.filter(action="invite.created", actor=actor).exists()


@pytest.mark.parametrize("roles", [[Role.AUDITOR], [Role.ARC_REVIEWER]])
def test_others_cannot_invite(roles):
    with pytest.raises(PermissionDenied):
        services.create_invitation(person=PersonFactory(), actor=make_member(roles=roles))


def test_owner_cannot_invite():
    with pytest.raises(PermissionDenied):
        services.create_invitation(person=PersonFactory(), actor=make_member(lots=[LotFactory()]))


def test_cannot_invite_someone_who_already_has_a_login(board):
    with pytest.raises(services.InvitationError):
        services.create_invitation(person=Person.objects.unscoped("t").get(user=board), actor=board)


def test_new_invitation_replaces_the_open_one(board):
    person = PersonFactory()
    first, first_token = invite_and_send(person, board)
    invite_and_send(person, board)
    first.refresh_from_db()
    assert first.status == "revoked"
    assert services.find_open_invitation(first_token) is None


# --- The email and the token --------------------------------------------------------


def test_only_the_token_hash_is_stored_and_email_says_little(board):
    person = PersonFactory(display_name="Fictional Owner Name")
    lot = LotFactory(street_address="12 Imaginary Way")
    record_ownership(person=person, lot=lot, start_date=days_ago(1), actor=SYSTEM)
    invitation, token = invite_and_send(person, board)
    invitation.refresh_from_db()
    assert token not in invitation.token_hash
    assert len(invitation.token_hash) == 64
    body = mail.outbox[-1].body
    assert "Oak Creek Farms HOA" in body
    for private in ("Fictional Owner Name", "12 Imaginary Way", f"Lot {lot.number}", "board"):
        assert private not in body


# --- Accepting ----------------------------------------------------------------------


def test_link_is_swapped_for_session_and_leaves_the_url(client, board):
    _, token = invite_and_send(PersonFactory(), board)
    response = client.get(reverse("invite_landing", args=[token]))
    assert response.status_code == 302
    assert response["Location"] == reverse("invite_accept")
    assert token not in response["Location"]


def test_accepting_creates_login_and_goes_straight_to_mfa(client, board):
    person = PersonFactory(email="newowner@example.com")
    invitation, token = invite_and_send(person, board)
    response = accept(client, token)
    assert response["Location"] == reverse("mfa_index")
    user = User.objects.get(email="newowner@example.com")
    assert client.session["_auth_user_id"] == str(user.pk)
    with system_access("test"):
        assert Person.objects.get(pk=person.pk).user == user
    assert EmailAddress.objects.get(user=user).verified
    invitation.refresh_from_db()
    assert invitation.status == "accepted"
    assert invitation.token_hash == ""  # nosec B105
    # Before enrolling MFA, every other page sends them back to enrollment.
    assert client.get(reverse("home"))["Location"] == reverse("mfa_index")
    assert AuditEvent.objects.filter(action="invite.accepted", actor=user).exists()


def test_link_works_only_once(client, board):
    _, token = invite_and_send(PersonFactory(), board)
    accept(client, token)
    client.logout()
    assert client.get(reverse("invite_landing", args=[token])).status_code == 404


def test_expired_link_is_refused(client, board):
    invitation, token = invite_and_send(PersonFactory(), board)
    with system_access("test"):
        Invitation.objects.filter(pk=invitation.pk).update(
            expires_at=timezone.now() - timedelta(minutes=1)
        )
    assert client.get(reverse("invite_landing", args=[token])).status_code == 404


def test_revoked_link_is_refused(client, board):
    invitation, token = invite_and_send(PersonFactory(), board)
    services.revoke_invitation(invitation, actor=board)
    assert client.get(reverse("invite_landing", args=[token])).status_code == 404


def test_resend_invalidates_the_previous_link(client, board):
    invitation, old_token = invite_and_send(PersonFactory(), board)
    with mock.patch("accounts.tasks.send_invitation.defer"):
        services.resend_invitation(invitation, actor=board)
    services.send_invitation_email(invitation.pk)
    assert client.get(reverse("invite_landing", args=[old_token])).status_code == 404


def test_unknown_token_is_refused(client):
    assert client.get(reverse("invite_landing", args=["x" * 43])).status_code == 404


def test_accept_page_without_a_valid_session_is_refused(client):
    assert client.get(reverse("invite_accept")).status_code == 404


@pytest.mark.parametrize("password", ["short", "password1234", "different-one-here"])
def test_weak_or_mismatched_passwords_are_refused(client, board, password):
    _, token = invite_and_send(PersonFactory(email="weak@example.com"), board)
    client.get(reverse("invite_landing", args=[token]))
    response = client.post(
        reverse("invite_accept"),
        {
            "password1": password,
            "password2": "not the same" if password == "different-one-here" else password,  # nosec B105
        },
    )
    assert response.status_code == 200
    assert not User.objects.filter(email="weak@example.com").exists()


def test_signed_in_user_cannot_use_a_link(client, board):
    _, token = invite_and_send(PersonFactory(), board)
    client.force_login(board)
    assert client.get(reverse("invite_landing", args=[token])).status_code == 400


# --- An invitation grants nothing by itself -----------------------------------------


def test_invitation_grants_only_what_the_records_say(client, board):
    nobody = PersonFactory(email="nobody@example.com")
    _, token = invite_and_send(nobody, board)
    accept(client, token)
    user = User.objects.get(email="nobody@example.com")
    assert policy.roles_for(user).groups == frozenset()
    assert not Lot.objects.for_user(user).exists()

    owner = PersonFactory(email="owner9@example.com")
    lot = LotFactory()
    record_ownership(person=owner, lot=lot, start_date=days_ago(1), actor=SYSTEM)
    client.logout()
    _, token = invite_and_send(owner, board)
    accept(client, token)
    owner_user = User.objects.get(email="owner9@example.com")
    assert list(Lot.objects.for_user(owner_user)) == [lot]
