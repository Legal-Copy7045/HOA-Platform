"""D40: the one-time "Official notices" step after sign-up, the My items
to-do for people who have not answered, and the board's counts."""

import json
import re
from unittest import mock

import pytest
from django.core import mail
from django.urls import reverse
from django.utils.html import escape

from accounts import services as accounts
from accounts.tests.helpers import PASSWORD, totp_code
from audit.models import AuditEvent
from community import notices, services
from community.models import ContactKind, ContactProfile, Person
from community.tests.factories import (
    LotFactory,
    PersonFactory,
    UserFactory,
    days_ago,
    enroll_totp,
    make_member,
)
from core import my_items
from core.policy import SYSTEM
from core.scoping import system_access

pytestmark = pytest.mark.django_db


def _profile(user):
    return ContactProfile.objects.unscoped("test").filter(person__user=user).first()


def _todo_titles(user):
    return [i.title for i in my_items.items_for(user)]


TODO = "Choose how to get official notices"


def _signed_up_owner(client, lot=None, email="newowner@example.com"):
    """Accept an invitation as an owner on the deed; returns the client at step 2."""
    board = make_member(board=True)
    person = PersonFactory(email=email)
    services.record_ownership(
        person=person, lot=lot or LotFactory(), start_date=days_ago(10), actor=SYSTEM
    )
    with mock.patch("accounts.tasks.send_invitation.defer"):
        invitation = accounts.create_invitation(person=person, actor=board)
    mail.outbox = []
    accounts.send_invitation_email(invitation.pk)
    token = re.search(r"/invite/([^/\s]+)/", mail.outbox[-1].body).group(1)
    client.get(reverse("invite_landing", args=[token]))
    response = client.post(reverse("invite_accept"), {"password1": PASSWORD, "password2": PASSWORD})
    assert response["Location"] == reverse("mfa_index")
    return person


def _activate_totp(client):
    page = client.get(reverse("mfa_activate_totp"))
    secret = page.context["form"].secret
    return client.post(reverse("mfa_activate_totp"), {"code": totp_code(secret)})


# --- Where the step sits in the sign-up flow -------------------------------------------


def test_step_is_never_reachable_before_mfa_enrollment(client):
    _signed_up_owner(client)
    # Signed in with a password only: the MFA middleware sends them to step 2.
    response = client.get(reverse("notice_choice"))
    assert response.status_code == 302 and response["Location"] == reverse("mfa_index")
    response = client.post(reverse("notice_choice"), {"choice": "email"})
    assert response["Location"] == reverse("mfa_index")
    person = Person.objects.unscoped("t").get(email="newowner@example.com")
    assert not ContactProfile.objects.unscoped("t").filter(person=person).exists()


def test_step_follows_the_recovery_codes_for_an_owner(client):
    _signed_up_owner(client)
    _activate_totp(client)
    response = client.post(reverse("account_setup_done"))
    assert response["Location"] == reverse("notice_choice")
    page = client.get(reverse("notice_choice")).content.decode()
    assert "<h1>Official notices</h1>" in page
    for kind in notices.NOTICE_KINDS:
        assert escape(kind) in page
    assert notices.YES_LABEL in page and notices.NO_LABEL in page
    assert "postage" in page and "My profile" in page and "on request" in page
    assert "checked" not in page  # no default answer
    assert "Skip for now" in page


def test_people_without_an_ownership_go_straight_home_after_setup(client):
    board = make_member(board=True)
    person = PersonFactory(email="notowner@example.com")
    with mock.patch("accounts.tasks.send_invitation.defer"):
        invitation = accounts.create_invitation(person=person, actor=board)
    mail.outbox = []
    accounts.send_invitation_email(invitation.pk)
    token = re.search(r"/invite/([^/\s]+)/", mail.outbox[-1].body).group(1)
    client.get(reverse("invite_landing", args=[token]))
    client.post(reverse("invite_accept"), {"password1": PASSWORD, "password2": PASSWORD})
    _activate_totp(client)
    assert client.post(reverse("account_setup_done"))["Location"] == reverse("home")


# --- Recording the choice ------------------------------------------------------------


def test_choosing_email_is_recorded_with_the_version_and_audited(client):
    owner = make_member(lots=[LotFactory()], email="yes@example.com")
    client.force_login(owner)
    response = client.post(reverse("notice_choice"), {"choice": "email"})
    assert response["Location"] == reverse("home")
    profile = _profile(owner)
    assert profile.electronic_notice_consent and profile.consent_changed_at
    assert profile.notice_consent_version == notices.CONSENT_VERSION
    event = AuditEvent.objects.get(action="profile.updated")
    assert event.metadata == {
        "notice_consent": True,
        "consent_changed": True,
        "notice_consent_version": notices.CONSENT_VERSION,
        "via": "signup",
    }
    assert "yes@example.com" not in json.dumps(event.metadata)
    # The meeting notice code now emails them.
    emails, _paper = services.owner_notice_recipients()
    assert "yes@example.com" in emails


def test_choosing_post_is_recorded_and_shows_the_mailing_address(client):
    lot = LotFactory(street_address="12 Oak Creek Lane")
    owner = make_member(lots=[lot])
    client.force_login(owner)
    response = client.post(reverse("notice_choice"), {"choice": "post"})
    assert response["Location"] == reverse("notice_choice") + "?chosen=post"
    page = client.get(response["Location"]).content.decode()
    assert "12 Oak Creek Lane" in page
    assert reverse("my_profile") + "#mailing" in page
    profile = _profile(owner)
    assert not profile.electronic_notice_consent and profile.consent_changed_at
    assert profile.notice_consent_version == notices.CONSENT_VERSION
    event = AuditEvent.objects.get(action="profile.updated")
    assert event.metadata["notice_consent"] is False and event.metadata["via"] == "signup"


def test_choosing_post_shows_the_owners_own_mailing_address(client):
    owner = make_member(lots=[LotFactory(street_address="12 Oak Creek Lane")])
    services.set_mailing_address(
        user=owner, to_home=False, street="9 Elm St", city="Media", state="PA", zip_code="19063"
    )
    client.force_login(owner)
    client.post(reverse("notice_choice"), {"choice": "post"})
    page = client.get(reverse("notice_choice") + "?chosen=post").content.decode()
    assert "9 Elm St" in page and "Media, PA 19063" in page


def test_a_missing_or_unknown_choice_re_renders_with_errors(client):
    owner = make_member(lots=[LotFactory()])
    client.force_login(owner)
    response = client.post(reverse("notice_choice"), {"choice": "fax"})
    assert response.status_code == 200
    assert 'class="alert danger form-errors"' in response.content.decode()
    assert _profile(owner) is None or _profile(owner).consent_changed_at is None


def test_step_is_post_only_for_changes_and_needs_csrf(client):
    from django.test import Client

    owner = make_member(lots=[LotFactory()])
    strict = Client(enforce_csrf_checks=True)
    strict.force_login(owner)
    assert strict.post(reverse("notice_choice"), {"choice": "email"}).status_code == 403
    assert _profile(owner) is None or not _profile(owner).electronic_notice_consent
    client.force_login(owner)
    assert client.put(reverse("notice_choice")).status_code == 405
    # A GET never records anything.
    client.get(reverse("notice_choice") + "?choice=email")
    assert _profile(owner) is None or _profile(owner).consent_changed_at is None


def test_the_step_has_no_person_in_the_url_so_nobody_sets_anothers_choice(client):
    owner, other = make_member(lots=[LotFactory()]), make_member(lots=[LotFactory()])
    client.force_login(owner)
    client.post(reverse("notice_choice"), {"choice": "email", "person": _profile_person(other)})
    assert _profile(owner).electronic_notice_consent
    assert _profile(other) is None or not _profile(other).electronic_notice_consent


def _profile_person(user):
    with system_access("t"):
        return Person.objects.get(user=user).pk


def test_answered_people_never_see_the_step_again(client):
    owner = make_member(lots=[LotFactory()])
    client.force_login(owner)
    client.post(reverse("notice_choice"), {"choice": "post"})
    response = client.get(reverse("notice_choice"))
    assert response["Location"] == reverse("my_profile") + "#notices"
    assert TODO not in _todo_titles(owner)
    assert not services.notice_choice_due(owner)


def test_answering_on_my_profile_also_counts(client):
    owner = make_member(lots=[LotFactory()])
    client.force_login(owner)
    client.post(reverse("my_profile"), {"section": "notices", "choice": "email"})
    profile = _profile(owner)
    assert profile.electronic_notice_consent
    assert profile.notice_consent_version == notices.CONSENT_VERSION
    assert AuditEvent.objects.get(action="profile.updated").metadata["via"] == "profile"
    assert client.get(reverse("notice_choice"))["Location"].endswith("#notices")
    assert TODO not in _todo_titles(owner)


# --- Who is asked --------------------------------------------------------------------


def _contact(kind):
    lot = LotFactory()
    make_member(lots=[lot])
    with system_access("t"):
        from community.models import Account

        account = Account.objects.get(lot=lot, closed_on__isnull=True)
    user = UserFactory()
    person = services.record_person(display_name=f"{kind} person", actor=SYSTEM)
    with system_access("t"):
        Person.objects.filter(pk=person.pk).update(user=user)
    enroll_totp(user)
    services.add_account_contact(account, person=person, kind=kind, actor=SYSTEM)
    return user


@pytest.mark.parametrize(
    "kind", [ContactKind.TENANT, ContactKind.VIEW_ONLY, ContactKind.AUTHORIZED]
)
def test_contacts_not_on_the_deed_are_never_asked(client, kind):
    user = _contact(kind)
    assert not services.notice_choice_due(user)
    assert TODO not in _todo_titles(user)
    client.force_login(user)
    assert client.get(reverse("notice_choice")).status_code == 404
    assert client.post(reverse("notice_choice"), {"choice": "email"}).status_code == 404


def test_former_owners_are_never_asked(client):
    lot = LotFactory()
    seller = make_member(lots=[lot])
    buyer = services.record_person(display_name="Buyer", actor=SYSTEM)
    services.transfer_lot(lot=lot, new_owners=[buyer], transfer_date=days_ago(0), actor=SYSTEM)
    assert not services.notice_choice_due(seller)
    assert TODO not in _todo_titles(seller)
    client.force_login(seller)
    assert client.get(reverse("notice_choice")).status_code in (403, 404)


def test_board_members_without_a_home_are_not_asked():
    board = make_member(board=True)
    assert not services.notice_choice_due(board)


def test_the_board_can_switch_the_question_off():
    owner = make_member(lots=[LotFactory()])
    assert services.notice_choice_due(owner)
    services.set_rule(
        key="notices.electronic_consent_required",
        value="false",
        citation="Bylaws art. 9",
        actor=SYSTEM,
    )
    assert not services.notice_choice_due(owner)


# --- Skip, and logins from before the step ---------------------------------------------


def test_skip_leaves_a_my_items_todo_until_answered(client):
    _signed_up_owner(client)
    _activate_totp(client)
    client.post(reverse("account_setup_done"))
    page = client.get(reverse("notice_choice")).content.decode()
    assert f'href="{reverse("home")}"' in page  # "Skip for now"
    home = client.get(reverse("home"))
    assert home.status_code == 200
    user = home.wsgi_request.user
    item = next(i for i in my_items.items_for(user) if i.title == TODO)
    assert item.url == reverse("notice_choice") and item.area == "My home"
    client.post(reverse("notice_choice"), {"choice": "email"})
    assert TODO not in _todo_titles(user)


def test_existing_logins_that_never_answered_get_the_todo_not_a_blocking_page(client):
    owner = make_member(lots=[LotFactory()])
    assert TODO in _todo_titles(owner)
    client.force_login(owner)
    assert client.get(reverse("home")).status_code == 200  # nothing blocks
    assert client.get(reverse("documents")).status_code == 200


def test_an_old_tick_on_my_profile_counts_as_an_answer():
    owner = make_member(lots=[LotFactory()])
    services.update_my_profile(user=owner, electronic_notice_consent=True)
    assert not services.notice_choice_due(owner)
    assert _profile(owner).notice_consent_version == notices.CONSENT_VERSION


# --- Board counts ----------------------------------------------------------------------


def test_invitations_page_counts_homes_by_notice_choice(client):
    board = make_member(board=True)
    email_lot, paper_lot, mixed_lot, silent_lot = (LotFactory() for _ in range(4))
    yes = make_member(lots=[email_lot])
    services.record_notice_choice(user=yes, consent=True)
    no = make_member(lots=[paper_lot])
    services.record_notice_choice(user=no, consent=False)
    # One owner chose email, the other post: the home gets email.
    a, b = make_member(lots=[mixed_lot]), make_member(lots=[mixed_lot])
    services.record_notice_choice(user=a, consent=True)
    services.record_notice_choice(user=b, consent=False)
    make_member(lots=[silent_lot])
    assert services.notice_choice_counts() == {"email": 2, "paper": 1, "unanswered": 1}
    client.force_login(board)
    page = client.get(reverse("invitations")).content.decode()
    assert "Email notices" in page and "Not answered" in page
    assert notices.CONSENT_VERSION in page and "postage" in page
    counts = client.get(reverse("invitations")).context["notice_counts"]
    assert counts == {"email": 2, "paper": 1, "unanswered": 1}


def test_counts_and_wording_are_board_only(client):
    from community.models import Role

    client.force_login(make_member(roles=[Role.SYSADMIN]))
    response = client.get(reverse("invitations"))
    assert response.status_code == 200
    assert "notice_counts" not in response.context
    client.force_login(make_member(lots=[LotFactory()]))
    assert client.get(reverse("invitations")).status_code == 403
