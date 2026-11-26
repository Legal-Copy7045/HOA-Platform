"""Owners ask the board to add people with a sign-in, and to change the owners
on the deed (D41): the email is required unless they don't use email, approval
invites them, deed changes carry the recorded deed, keep at least one owner,
and are never approved by the person added or removed."""

import datetime

import pytest
from django.core.exceptions import PermissionDenied
from django.urls import reverse

from accounts.models import Invitation
from audit.models import AuditEvent
from community import services
from community.models import (
    Account,
    AccountChangeRequest,
    AccountContact,
    Ownership,
    Person,
    Role,
    today,
)
from community.tests.factories import LotFactory, UserFactory, days_ago, enroll_totp, make_member
from core import policy
from core.policy import SYSTEM
from core.scoping import system_access
from documents.models import Attachment
from documents.tests.helpers import pdf

pytestmark = pytest.mark.django_db


def _person(user):
    with system_access("t"):
        return Person.objects.get(user=user)


def _account(lot):
    with system_access("t"):
        return Account.objects.get(lot=lot, closed_on__isnull=True)


def _ownership(user, lot):
    with system_access("t"):
        return Ownership.objects.get(person__user=user, lot=lot, end_date__isnull=True)


def _change(pk):
    with system_access("t"):
        return AccountChangeRequest.objects.get(pk=pk)


@pytest.fixture
def w():
    lot = LotFactory()
    return {
        "lot": lot,
        "owner": make_member(lots=[lot]),
        "board": make_member(board=True),
        "board2": make_member(board=True),
    }


def _add_owner(w, **overrides):
    values = {
        "action": "add_owner",
        "name": "Pat Spouse",
        "email": "pat.spouse@example.com",
        "ownership_type": "joint",
        "deed_date": days_ago(10),
        "deed": pdf("deed.pdf"),
        "actor": w["owner"],
    }
    values.update(overrides)
    return services.request_account_change(_account(w["lot"]), **values)


def _contact_user(account, kind):
    user = UserFactory()
    person = services.record_person(display_name=f"{kind} person", actor=SYSTEM)
    with system_access("t"):
        Person.objects.filter(pk=person.pk).update(user=user)
    person.refresh_from_db()
    enroll_totp(user)
    services.add_account_contact(account, person=person, kind=kind, actor=SYSTEM)
    return user


# --- A. Email and sign-in ------------------------------------------------------------------


def test_adding_someone_needs_their_email_unless_they_dont_use_email(w):
    account = _account(w["lot"])
    with pytest.raises(services.CommunityError, match="they sign in with it"):
        services.request_account_change(
            account, action="add", name="Teen", kind="view_only", actor=w["owner"]
        )
    with pytest.raises(services.CommunityError, match="valid email"):
        services.request_account_change(
            account, action="add", name="Teen", kind="view_only", email="nope", actor=w["owner"]
        )
    change = services.request_account_change(
        account, action="add", name="Grandma", kind="view_only", no_login=True, actor=w["owner"]
    )
    assert change.no_login and not change.will_invite


def test_my_home_form_asks_for_the_email_and_explains_the_invitation(client, w):
    client.force_login(w["owner"])
    page = client.get(reverse("my_lot")).content.decode()
    assert "Their email (they sign in with this)" in page
    assert "They don&#x27;t use email: keep them on the account without a sign-in" in page
    assert "Once a board member approves, they get an email invitation to sign in." in page
    assert "Ask to change the owners on the deed" in page


def test_an_add_without_email_comes_back_with_what_was_typed(client, w):
    client.force_login(w["owner"])
    response = client.post(
        reverse("account_change_request", args=[_account(w["lot"]).pk]),
        {"action": "add", "name": "Typed Name Here", "kind": "tenant", "reason": "Renting"},
    )
    assert response.status_code == 200
    page = response.content.decode()
    assert "Nothing has been saved yet" in page
    assert "Typed Name Here" in page and "Renting" in page
    assert "Enter their email, or tick that they don" in page
    assert not AccountChangeRequest.objects.for_user(w["owner"]).exists()


def test_approving_an_add_with_an_email_invites_them_from_the_person_record(client, w):
    change = services.request_account_change(
        _account(w["lot"]),
        action="add",
        name="Sam Renter",
        kind="tenant",
        email="sam.renter@example.com",
        actor=w["owner"],
    )
    client.force_login(w["board"])
    page = client.get(reverse("lot_detail", args=[w["lot"].pk])).content.decode()
    assert "Approving sends sam.renter@example.com an email invitation to sign in." in page
    response = client.post(
        reverse("account_change_decide", args=[change.pk, "approve"]), follow=True
    )
    assert "they were emailed an invitation" in response.content.decode()
    change = _change(change.pk)
    assert change.state == "approved" and change.invitation_sent is True
    invitation = Invitation.objects.unscoped("t").get(person=change.person)
    assert invitation.email == "sam.renter@example.com"
    assert invitation.created_by == w["board"]
    with system_access("t"):
        assert AccountContact.objects.filter(person=change.person, kind="tenant").exists()
    client.force_login(w["owner"])
    assert "Invitation to sign in emailed." in client.get(reverse("my_lot")).content.decode()


def test_a_failed_invitation_leaves_the_approval_and_tells_the_board(client, w):
    make_member(email="taken@example.com")
    change = services.request_account_change(
        _account(w["lot"]),
        action="add",
        name="Already Has Login",
        kind="authorized",
        email="taken@example.com",
        actor=w["owner"],
    )
    client.force_login(w["board"])
    response = client.post(
        reverse("account_change_decide", args=[change.pk, "approve"]), follow=True
    )
    assert "Send it from Invitations and sign-ups" in response.content.decode()
    change = _change(change.pk)
    assert change.state == "approved" and change.invitation_sent is False
    assert not Invitation.objects.unscoped("t").filter(person=change.person).exists()
    with system_access("t"):
        assert AccountContact.objects.filter(person=change.person).exists()


def test_without_email_the_person_is_a_record_only(client, w):
    change = services.request_account_change(
        _account(w["lot"]),
        action="add",
        name="No Email Nan",
        kind="view_only",
        no_login=True,
        actor=w["owner"],
    )
    client.force_login(w["board"])
    page = client.get(reverse("lot_detail", args=[w["lot"].pk])).content.decode()
    assert "No sign-in: they don't use email." in page
    response = client.post(
        reverse("account_change_decide", args=[change.pk, "approve"]), follow=True
    )
    assert "cannot sign in until the board adds an email" in response.content.decode()
    change = _change(change.pk)
    assert change.invitation_sent is None
    assert not Invitation.objects.unscoped("t").filter(person=change.person).exists()


# --- B. Owners on the deed ----------------------------------------------------------------


def test_owner_asks_to_add_a_co_owner_with_the_recorded_deed(client, w):
    from ledger import services as ledger
    from ledger.models import EntryMoveNotice, LedgerEntry

    ledger.post_charge(
        lot=w["lot"],
        kind="other_charge",
        amount_cents=2500,
        description="Earlier charge",
        posted_on=days_ago(20),
        actor=SYSTEM,
    )
    account = _account(w["lot"])
    client.force_login(w["owner"])
    response = client.post(
        reverse("account_change_request", args=[account.pk]),
        {
            "action": "add_owner",
            "name": "Pat Spouse",
            "email": "pat.spouse@example.com",
            "ownership_type": "joint",
            "deed_date": days_ago(10).isoformat(),
            "deed": pdf("deed.pdf"),
        },
    )
    assert response.status_code == 302
    change = AccountChangeRequest.objects.for_user(w["owner"]).get()
    assert change.action == "add_owner" and change.deed_date == days_ago(10)
    with system_access("t"):
        assert (
            Attachment.objects.filter(
                owner_type="community.accountchangerequest", owner_id=str(change.pk)
            ).count()
            == 1
        )
    with system_access("t"):
        entries_before = LedgerEntry.objects.count()
        accounts_before = Account.objects.filter(lot=w["lot"]).count()

    client.force_login(w["board"])
    page = client.get(reverse("lot_detail", args=[w["lot"].pk])).content.decode()
    assert "Add an owner on the deed" in page and "deed.pdf" in page
    client.post(reverse("account_change_decide", args=[change.pk, "approve"]))
    change = _change(change.pk)
    assert change.state == "approved" and change.invitation_sent is True
    with system_access("t"):
        added = Ownership.objects.get(person=change.person)
        assert added.account_id == account.pk
        assert added.start_date == days_ago(10) and added.ownership_type == "joint"
        assert Account.objects.filter(lot=w["lot"]).count() == accounts_before
        assert LedgerEntry.objects.count() == entries_before
        assert not EntryMoveNotice.objects.exists()
    first = _ownership(w["owner"], w["lot"])
    assert first.ownership_type == "joint"
    assert AuditEvent.objects.get(action="ownership.type_changed").metadata == {
        "lot_id": w["lot"].pk,
        "person_id": _person(w["owner"]).pk,
        "ownership_type": "joint",
    }
    for event in AuditEvent.objects.filter(action__startswith="account.change"):
        assert "Pat" not in str(event.metadata) and "@" not in str(event.metadata)
    assert Invitation.objects.unscoped("t").get(person=change.person).email == (
        "pat.spouse@example.com"
    )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"deed": None}, "Attach a copy of the recorded deed"),
        ({"deed_date": today() + datetime.timedelta(days=1)}, "cannot be in the future"),
        ({"deed_date": None}, "Enter the date on the recorded deed"),
        ({"ownership_type": "sole"}, "joint owner, trust or entity"),
        ({"email": ""}, "they sign in with it"),
        ({"deed_date": days_ago(400)}, "before your account began"),
        ({"deed": pdf("deed.exe", body=b"MZ")}, "file types"),
    ],
)
def test_add_owner_requests_are_checked(w, overrides, message):
    with pytest.raises(services.CommunityError, match=message):
        _add_owner(w, **overrides)


def test_an_add_owner_form_with_a_future_date_comes_back(client, w):
    client.force_login(w["owner"])
    response = client.post(
        reverse("account_change_request", args=[_account(w["lot"]).pk]),
        {
            "action": "add_owner",
            "name": "Future Fran",
            "email": "fran@example.com",
            "ownership_type": "trust",
            "deed_date": (today() + datetime.timedelta(days=3)).isoformat(),
            "deed": pdf("deed.pdf"),
        },
    )
    assert response.status_code == 200
    page = response.content.decode()
    assert "Future Fran" in page and "The deed date cannot be in the future." in page
    assert f' open id="deed-change-{_account(w["lot"]).pk}"' in page


def test_a_trust_can_be_added_without_email(w):
    change = _add_owner(w, name="Family Trust", email="", no_login=True, ownership_type="trust")
    services.decide_account_change(change, approve=True, actor=w["board"])
    change = _change(change.pk)
    assert change.invitation_sent is None
    with system_access("t"):
        assert Ownership.objects.get(person=change.person).ownership_type == "trust"


def test_a_deed_dated_before_the_account_is_refused_at_approval(w):
    change = _add_owner(w)
    with system_access("t"):
        AccountChangeRequest.objects.filter(pk=change.pk).update(deed_date=days_ago(500))
    with pytest.raises(services.CommunityError, match="home's page"):
        services.decide_account_change(change, approve=True, actor=w["board"])
    assert _change(change.pk).state == "pending"
    with system_access("t"):
        assert Ownership.objects.filter(lot=w["lot"]).count() == 1


@pytest.fixture
def joint(w):
    """The lot has two owners on the deed (the owner and a co-owner)."""
    co_owner = make_member()
    services.record_ownership(
        person=_person(co_owner),
        lot=w["lot"],
        start_date=days_ago(300),
        ownership_type="joint",
        actor=SYSTEM,
    )
    services.set_ownership_type(_ownership(w["owner"], w["lot"]), "joint", actor=SYSTEM)
    return {**w, "co_owner": co_owner}


def test_owner_asks_to_remove_a_co_owner_with_the_deed(client, joint):
    w = joint
    account = _account(w["lot"])
    leaving = _ownership(w["co_owner"], w["lot"])
    client.force_login(w["owner"])
    page = client.get(reverse("my_lot")).content.decode()
    assert "Remove an owner" in page and "Owner to remove" in page
    response = client.post(
        reverse("account_change_request", args=[account.pk]),
        {
            "action": "remove_owner",
            "ownership": leaving.pk,
            "deed_date": days_ago(3).isoformat(),
            "deed": pdf("quitclaim.pdf"),
        },
    )
    assert response.status_code == 302
    change = AccountChangeRequest.objects.for_user(w["owner"]).get()
    services.decide_account_change(change, approve=True, actor=w["board"])
    leaving.refresh_from_db()
    assert leaving.end_date == days_ago(3)
    assert _ownership(w["owner"], w["lot"]).ownership_type == "sole"
    assert _account(w["lot"]).pk == account.pk  # the account carries on
    assert not policy.roles_for(w["co_owner"]).owned_lot_ids


def test_an_owner_may_ask_to_leave_the_deed_while_another_owner_remains(joint):
    w = joint
    change = services.request_account_change(
        _account(w["lot"]),
        action="remove_owner",
        ownership=_ownership(w["owner"], w["lot"]),
        deed_date=days_ago(1),
        deed=pdf("deed.pdf"),
        actor=w["owner"],
    )
    services.decide_account_change(change, approve=True, actor=w["board"])
    assert not policy.roles_for(w["owner"]).owned_lot_ids


def test_the_last_owner_cannot_be_removed(client, w):
    with pytest.raises(services.CommunityError, match="at least one owner"):
        services.request_account_change(
            _account(w["lot"]),
            action="remove_owner",
            ownership=_ownership(w["owner"], w["lot"]),
            deed_date=days_ago(1),
            deed=pdf("deed.pdf"),
            actor=w["owner"],
        )
    client.force_login(w["owner"])
    page = client.get(reverse("my_lot")).content.decode()
    assert "The account must keep at least one owner on the deed." in page
    assert "Owner to remove" not in page


def test_two_removals_cannot_leave_the_account_without_owners(joint):
    w = joint
    account = _account(w["lot"])
    first = services.request_account_change(
        account,
        action="remove_owner",
        ownership=_ownership(w["co_owner"], w["lot"]),
        deed_date=days_ago(1),
        deed=pdf("a.pdf"),
        actor=w["owner"],
    )
    second = services.request_account_change(
        account,
        action="remove_owner",
        ownership=_ownership(w["owner"], w["lot"]),
        deed_date=days_ago(1),
        deed=pdf("b.pdf"),
        actor=w["co_owner"],
    )
    services.decide_account_change(first, approve=True, actor=w["board"])
    with pytest.raises(services.CommunityError, match="at least one owner"):
        services.decide_account_change(second, approve=True, actor=w["board"])
    assert _ownership(w["owner"], w["lot"]).end_date is None


def test_the_person_removed_cannot_approve_it(joint):
    w = joint
    lot = w["lot"]
    board_co_owner = make_member(board=True)
    services.record_ownership(
        person=_person(board_co_owner),
        lot=lot,
        start_date=days_ago(300),
        ownership_type="joint",
        actor=SYSTEM,
    )
    change = services.request_account_change(
        _account(lot),
        action="remove_owner",
        ownership=_ownership(board_co_owner, lot),
        deed_date=days_ago(1),
        deed=pdf("deed.pdf"),
        actor=w["owner"],
    )
    with pytest.raises(services.CommunityError, match="your own ownership"):
        services.decide_account_change(change, approve=True, actor=board_co_owner)
    services.decide_account_change(change, approve=True, actor=w["board"])


def test_the_person_added_cannot_approve_it(w):
    board_email = w["board"].email
    change = _add_owner(w, email=board_email.upper())
    with pytest.raises(services.CommunityError, match="adds you"):
        services.decide_account_change(change, approve=True, actor=w["board"])
    assert _change(change.pk).state == "pending"
    services.decide_account_change(change, approve=True, actor=w["board2"])


def test_the_requester_cannot_approve_their_own_deed_change(w):
    board_owner = make_member(board=True, lots=[w["lot"]])
    change = _add_owner(w, actor=board_owner)
    with pytest.raises(services.CommunityError, match="you asked for"):
        services.decide_account_change(change, approve=True, actor=board_owner)


# --- C. Who may ask, and who sees what -----------------------------------------------------


@pytest.mark.parametrize("kind", ["authorized", "view_only", "tenant"])
def test_contacts_and_tenants_cannot_ask(client, w, kind):
    account = _account(w["lot"])
    user = _contact_user(account, kind)
    assert not policy.can(user, "account.request_change")
    with pytest.raises(PermissionDenied):
        _add_owner(w, actor=user)
    client.force_login(user)
    response = client.post(
        reverse("account_change_request", args=[account.pk]),
        {"action": "add", "name": "X", "kind": "tenant", "no_login": "on"},
    )
    assert response.status_code == 403
    assert (
        "Ask to change the owners on the deed" not in client.get(reverse("my_lot")).content.decode()
    )


def test_former_owners_cannot_ask(client, w):
    account = _account(w["lot"])
    services.transfer_lot(
        w["lot"], new_owners=[_person(make_member())], transfer_date=today(), actor=SYSTEM
    )
    seller = w["owner"]
    assert policy.roles_for(seller).groups == {policy.FORMER_OWNER}
    with pytest.raises(PermissionDenied):
        services.request_account_change(
            account, action="add", name="X", kind="tenant", no_login=True, actor=seller
        )
    client.force_login(seller)
    response = client.post(
        reverse("account_change_request", args=[account.pk]),
        {"action": "add", "name": "X", "kind": "tenant", "no_login": "on"},
    )
    assert response.status_code == 403


def test_the_deed_is_for_the_board_and_that_accounts_owners_only(client, joint):
    w = joint
    change = _add_owner(w)
    with system_access("t"):
        file_id = Attachment.objects.get(owner_id=str(change.pk)).pk
    url = reverse("account_change_deed", args=[change.pk, file_id])
    for user in (w["owner"], w["co_owner"], w["board"]):
        client.force_login(user)
        response = client.get(url)
        assert response.status_code == 200, user
        assert response["Content-Disposition"].startswith("attachment")
    assert AuditEvent.objects.filter(action="attachment.downloaded").count() == 3
    manager = make_member(roles=[Role.MANAGER])
    authorized = _contact_user(_account(w["lot"]), "authorized")
    intruder = make_member(lots=[LotFactory()])
    for user in (manager, authorized, intruder):
        client.force_login(user)
        assert client.get(url).status_code == 404, user


def test_owners_of_another_home_reach_nothing(client, w):
    change = _add_owner(w)
    with system_access("t"):
        file_id = Attachment.objects.get(owner_id=str(change.pk)).pk
    intruder = make_member(lots=[LotFactory()])
    client.force_login(intruder)
    assert (
        client.post(
            reverse("account_change_request", args=[_account(w["lot"]).pk]),
            {"action": "add_owner", "name": "X"},
        ).status_code
        == 404
    )
    assert client.get(reverse("account_change_deed", args=[change.pk, file_id])).status_code == 404
    assert client.post(
        reverse("account_change_decide", args=[change.pk, "approve"])
    ).status_code in (403, 404)
    assert client.post(reverse("account_change_withdraw", args=[change.pk])).status_code == 404
    assert "Pat Spouse" not in client.get(reverse("my_lot")).content.decode()
    assert _change(change.pk).state == "pending"


def test_an_owner_cannot_remove_an_owner_of_another_home(w):
    other_lot = LotFactory()
    other_owner = make_member(lots=[other_lot])
    with pytest.raises(services.CommunityError, match="current owner on this account"):
        services.request_account_change(
            _account(w["lot"]),
            action="remove_owner",
            ownership=_ownership(other_owner, other_lot),
            deed_date=days_ago(1),
            deed=pdf("deed.pdf"),
            actor=w["owner"],
        )
