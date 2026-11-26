"""Milestone 4: bulk onboarding after the register import."""

import datetime
from unittest import mock

import pytest
from django.urls import reverse
from django.utils import timezone

from accounts import services
from accounts.models import Invitation
from community import services as community
from community.tests.factories import LotFactory, days_ago, make_member
from core.policy import SYSTEM

pytestmark = pytest.mark.django_db


@pytest.fixture
def w():
    board = make_member(board=True)
    people = {}
    for name, email in (("With Email", "with@example.com"), ("No Email", "")):
        person = community.record_person(display_name=name, email=email, actor=SYSTEM)
        community.record_ownership(
            person=person, lot=LotFactory(), start_date=days_ago(10), actor=SYSTEM
        )
        people[name] = person
    member = make_member(lots=[LotFactory()])
    return {"board": board, "people": people, "member": member}


def _status(name):
    return {r["person"].display_name: r["status"] for r in services.onboarding_rows()}[name]


@mock.patch("accounts.tasks.send_invitation.defer")
def test_status_and_invite_everyone(_defer, w):
    assert _status("With Email") == "not_invited"
    assert _status("No Email") == "no_email"
    assert services.invite_everyone(actor=w["board"]) == 1
    assert _status("With Email") == "invited"
    assert services.invite_everyone(actor=w["board"]) == 0  # nothing twice
    inv = Invitation.objects.unscoped("t").get(person=w["people"]["With Email"])
    assert inv.email == "with@example.com"  # only the address on record


@mock.patch("accounts.tasks.send_invitation.defer")
def test_expired_invitations_get_a_fresh_one_and_old_ones_are_reminded(_defer, w):
    services.invite_everyone(actor=w["board"])
    inv = Invitation.objects.unscoped("t").get(person=w["people"]["With Email"])
    Invitation.objects.unscoped("t").filter(pk=inv.pk).update(
        created_at=timezone.now() - datetime.timedelta(days=5)
    )
    assert services.remind_pending(actor=w["board"]) == 1
    Invitation.objects.unscoped("t").filter(pk=inv.pk).update(
        expires_at=timezone.now() - datetime.timedelta(minutes=1)
    )
    assert _status("With Email") == "expired"
    assert services.invite_everyone(actor=w["board"]) == 1


def test_letters_for_owners_without_email(client, w):
    client.force_login(w["board"])
    response = client.get(reverse("invitations"), {"letters": "1"})
    assert response["Content-Type"] == "application/pdf"
    assert b"No Email" in response.content and b"with@example.com" not in response.content


def test_owners_cannot_open_onboarding(client, w):
    client.force_login(w["member"])
    assert client.get(reverse("onboarding")).status_code == 403


def _signups(client):
    return client.get(reverse("lot_list"), {"view": "signups"})


def test_page_shows_counts(client, w):
    client.force_login(w["board"])
    page = _signups(client).content
    assert b"No email on record" in page and b"With Email" in page


# --- Invitations and sign-ups (streamlining: Onboarding merged into Invitations) ---------


def test_onboarding_redirects_to_the_sign_ups_tab_and_keeps_the_letters(client, w):
    client.force_login(w["board"])
    response = client.get(reverse("onboarding"))
    assert response.status_code == 302
    assert response["Location"] == reverse("lot_list") + "?view=signups"
    response = client.get(reverse("onboarding"), {"letters": "1"})
    assert response["Location"] == reverse("invitations") + "?letters=1"


def test_invitations_redirects_the_board_to_the_sign_ups_tab(client, w):
    """WT-002: sign-ups are a tab of Homes and owners; bookmarks and emails
    to the old address still land there."""
    client.force_login(w["board"])
    response = client.get(reverse("invitations"))
    assert response.status_code == 302
    assert response["Location"] == reverse("lot_list") + "?view=signups"


def test_sign_ups_tab_has_counts_and_bulk_buttons_for_the_board(client, w):
    client.force_login(w["board"])
    page = _signups(client).content.decode()
    assert "<h1>Homes and owners</h1>" in page
    assert 'href="/lots/?view=signups" aria-current="page"' in page
    assert "With Email" in page and "Invite everyone not yet invited" in page
    assert "Every invitation" in page and "Official notices" in page
    # The bulk buttons and the letters download still go to the invitations URL.
    assert f'action="{reverse("invitations")}"' in page
    assert f'href="{reverse("invitations")}?letters=1"' in page


@pytest.mark.parametrize("role", ["MANAGER", "AUDITOR"])
def test_sign_ups_tab_is_board_only(client, role):
    """Managers and auditors see Homes and owners but never the sign-ups or
    the bulk buttons (invite.send is board only)."""
    from community.models import Role

    client.force_login(make_member(roles=[getattr(Role, role)]))
    page = client.get(reverse("lot_list"), {"view": "signups"}).content.decode()
    assert "Invite everyone" not in page and "Every invitation" not in page
    assert "?view=signups" not in page


@mock.patch("accounts.tasks.send_invitation.defer")
def test_board_bulk_invites_from_the_merged_page(_defer, client, w):
    client.force_login(w["board"])
    response = client.post(reverse("invitations"), {"action": "invite_all"})
    assert response.status_code == 302
    assert response["Location"] == reverse("lot_list") + "?view=signups"
    assert _status("With Email") == "invited"


@mock.patch("accounts.tasks.send_invitation.defer")
def test_sysadmin_sees_invitation_status_only_and_no_bulk_buttons(_defer, client, w):
    from community.models import Role

    services.invite_everyone(actor=w["board"])
    sysadmin = make_member(roles=[Role.SYSADMIN])
    client.force_login(sysadmin)
    response = client.get(reverse("invitations"))
    assert response.status_code == 200
    page = response.content.decode()
    assert "<h1>Invitations</h1>" in page  # WT-018: no sign-ups shown, so not named so
    # Not the register (D9): no names, no sign-up table.
    assert "With Email" not in page and "No Email" not in page
    assert "Invite everyone" not in page and "Sign-ups by lot" not in page
    # Each bulk button keeps its own check: invite.send is board only.
    for action in ("invite_all", "remind"):
        assert client.post(reverse("invitations"), {"action": action}).status_code == 403
    assert client.get(reverse("invitations"), {"letters": "1"}).status_code == 403
    assert client.get(reverse("onboarding")).status_code == 403
    assert Invitation.objects.unscoped("t").count() == 1


def test_owners_cannot_open_invitations(client, w):
    client.force_login(w["member"])
    assert client.get(reverse("invitations")).status_code == 403
    assert client.post(reverse("invitations"), {"action": "invite_all"}).status_code == 403
