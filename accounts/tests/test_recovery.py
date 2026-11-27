"""Self-service account recovery with a waiting period, and the lock after it (D58)."""

import datetime
import re
from unittest import mock

import pytest
from allauth.mfa.models import Authenticator
from django.core import mail
from django.urls import reverse
from django.utils import timezone

from accounts import enrollment, recovery
from accounts.models import (
    AccountRecovery,
    RecoveryCancelledBy,
    RecoveryLink,
    RecoveryLinkPurpose,
)
from accounts.tests.helpers import PASSWORD, totp_code
from audit.actions import ACTIONS
from audit.models import AuditEvent
from community import services as community
from community.models import (
    Account,
    BoardSeat,
    ContactKind,
    ContactMethodKind,
    ContactProfile,
    GoverningRule,
    Person,
    Role,
    RoleAssignment,
)
from community.tests.factories import LotFactory, PersonFactory, days_ago, make_member
from core import my_items
from core.policy import SYSTEM, roles_for
from core.scoping import system_access

pytestmark = pytest.mark.django_db


def _person(user):
    return Person.objects.unscoped("t").get(user=user)


def _owner(email="owner@example.com", **kwargs):
    return make_member(lots=[LotFactory()], email=email, **kwargs)


def _links_in(message) -> list[str]:
    return re.findall(r"/sign-in-help/recover/([A-Za-z0-9_\-]+)/", message.body)


def _send(user, django_capture_on_commit_callbacks) -> str:
    mail.outbox = []
    with django_capture_on_commit_callbacks(execute=True):
        return recovery.send_link(user.pk)


def _start(client, user, django_capture_on_commit_callbacks) -> AccountRecovery:
    assert _send(user, django_capture_on_commit_callbacks) == "sent_start"
    token = _links_in(mail.outbox[-1])[0]
    client.get(reverse("recovery_link", args=[token]))
    mail.outbox = []
    with django_capture_on_commit_callbacks(execute=True):
        page = client.post(reverse("recovery_continue"))
    assert page.status_code == 200 and b"Your recovery has started" in page.content
    return recovery.open_recovery(user)


def _past_wait(row: AccountRecovery) -> None:
    now = timezone.now()
    AccountRecovery.objects.unscoped("t").filter(pk=row.pk).update(
        started_at=now - datetime.timedelta(hours=49),
        ready_at=now - datetime.timedelta(hours=1),
    )


def _finish(client, user, django_capture_on_commit_callbacks):
    assert _send(user, django_capture_on_commit_callbacks) == "sent_finish"
    token = _links_in(mail.outbox[-1])[0]
    client.get(reverse("recovery_link", args=[token]))
    with django_capture_on_commit_callbacks(execute=True):
        return client.post(reverse("recovery_continue"))


def _recovered(client, user, django_capture_on_commit_callbacks) -> AccountRecovery:
    row = _start(client, user, django_capture_on_commit_callbacks)
    _past_wait(row)
    response = _finish(client, user, django_capture_on_commit_callbacks)
    assert response["Location"] == reverse("mfa_index")
    row.refresh_from_db()
    return row


# --- Who may recover themselves --------------------------------------------------------


def test_owner_is_eligible():
    assert recovery.eligible(_owner())


@pytest.mark.parametrize(
    "kind", [ContactKind.AUTHORIZED, ContactKind.VIEW_ONLY, ContactKind.TENANT]
)
def test_account_contacts_and_tenants_are_eligible(kind):
    owner = _owner("deed@example.com")
    lot_id = next(iter(roles_for(owner).owned_lot_ids))
    with system_access("t"):
        account = Account.objects.filter(lot_id=lot_id).first()
    contact = make_member(email=f"{kind}@example.com")
    community.add_account_contact(account, person=_person(contact), kind=kind, actor=SYSTEM)
    assert recovery.eligible(contact)


def test_former_owner_is_eligible():
    lot = LotFactory()
    seller = make_member(lots=[lot], email="seller@example.com")
    with system_access("t"):
        ownership = _person(seller).ownerships.get()
    community.end_ownership(ownership, end_date=days_ago(1), actor=SYSTEM)
    assert recovery.eligible(seller)


@pytest.mark.parametrize("role", [Role.MANAGER, Role.SYSADMIN, Role.AUDITOR, Role.ARC_REVIEWER])
def test_staff_roles_are_never_eligible(role):
    assert not recovery.eligible(make_member(lots=[LotFactory()], roles=[role]))


def test_board_and_officers_are_never_eligible():
    assert not recovery.eligible(make_member(lots=[LotFactory()], board=True))


def _end_role(user, days):
    with system_access("t"):
        RoleAssignment.objects.filter(person__user=user).update(
            start_date=days_ago(200), end_date=days_ago(days)
        )
    from core.policy import invalidate_roles

    invalidate_roles()


def test_role_ended_ten_days_ago_is_still_ineligible():
    user = make_member(lots=[LotFactory()], roles=[Role.MANAGER])
    _end_role(user, 10)
    assert not recovery.eligible(user)


def test_role_ended_forty_days_ago_is_eligible_again():
    user = make_member(lots=[LotFactory()], roles=[Role.ARC_REVIEWER])
    _end_role(user, 40)
    assert recovery.eligible(user)


def test_board_seat_ended_recently_or_long_ago():
    user = make_member(lots=[LotFactory()], board=True)
    with system_access("t"):
        BoardSeat.objects.filter(person__user=user).update(
            ended_on=days_ago(10), end_reason="resigned"
        )
    assert not recovery.eligible(user)
    with system_access("t"):
        BoardSeat.objects.filter(person__user=user).update(
            ended_on=days_ago(40), end_reason="resigned"
        )
    assert recovery.eligible(user)


def test_no_authenticator_inactive_or_staff_flag_is_ineligible():
    assert not recovery.eligible(make_member(lots=[LotFactory()], mfa=False))
    user = _owner("inactive@example.com")
    user.is_active = False
    user.save()
    assert not recovery.eligible(user)
    staff = _owner("staff@example.com")
    staff.is_staff = True
    staff.save()
    assert not recovery.eligible(staff)


# --- Neutral answers (no account enumeration) ----------------------------------------


def test_same_answer_and_same_path_for_unknown_ineligible_and_eligible(
    client, django_capture_on_commit_callbacks
):
    owner = _owner("eligible@example.com")
    board = make_member(board=True, email="director@example.com")
    pages = []
    with mock.patch("accounts.tasks.send_recovery_link.defer") as defer:
        for email in ("nobody@example.com", board.email, owner.email):
            with django_capture_on_commit_callbacks(execute=True):
                pages.append(client.post(reverse("recovery_request"), {"email": email}))
    assert {p.status_code for p in pages} == {200}
    assert pages[0].content == pages[1].content == pages[2].content
    assert b"If that address belongs to an account" in pages[0].content
    # One queued job each, carrying only a login id (0 for unknown).
    assert [c.kwargs for c in defer.call_args_list] == [
        {"user_id": 0},
        {"user_id": board.pk},
        {"user_id": owner.pk},
    ]
    assert "_auth_user_id" not in client.session


def test_ineligible_login_gets_no_email(django_capture_on_commit_callbacks):
    board = make_member(board=True, email="noemail@example.com")
    assert _send(board, django_capture_on_commit_callbacks) == "ineligible"
    assert mail.outbox == []
    assert recovery.send_link(0) == "unknown"
    event = AuditEvent.objects.filter(action="account_recovery.link_refused").last()
    assert event.metadata == {"user_id": board.pk, "reason": "ineligible"}


def test_eligible_login_gets_a_single_use_link(client, django_capture_on_commit_callbacks):
    owner = _owner()
    assert _send(owner, django_capture_on_commit_callbacks) == "sent_start"
    assert mail.outbox[-1].to == [owner.email]
    token = _links_in(mail.outbox[-1])[0]
    link = RecoveryLink.objects.unscoped("t").get(subject=owner)
    assert link.token_hash != token and link.expires_at <= timezone.now() + recovery.LINK_TTL
    response = client.get(reverse("recovery_link", args=[token]))
    assert response["Location"] == reverse("recovery_continue")
    assert token not in str(client.session.items())
    page = client.get(reverse("recovery_continue"))
    assert b"Start account recovery?" in page.content and b"48 hours" in page.content
    client.post(reverse("recovery_continue"))
    # Used once: the link no longer opens anything.
    assert client.get(reverse("recovery_link", args=[token])).status_code == 404


def test_signed_in_user_cannot_use_a_link(client, django_capture_on_commit_callbacks):
    owner = _owner()
    _send(owner, django_capture_on_commit_callbacks)
    token = _links_in(mail.outbox[-1])[0]
    client.force_login(owner)
    assert client.get(reverse("recovery_link", args=[token])).status_code == 400


# --- Starting: the wait and the alerts -------------------------------------------------


def test_start_alerts_every_email_the_text_number_and_the_board(
    client, django_capture_on_commit_callbacks
):
    from core.models import FeatureSetting

    owner = _owner("main@example.com")
    person = _person(owner)
    person.email = "register@example.com"
    person.save()
    community.add_contact_method(
        person, kind=ContactMethodKind.EMAIL, value="extra@example.com", actor=SYSTEM
    )
    with system_access("t"):
        ContactProfile.objects.update_or_create(
            person=person,
            defaults={
                "sms_number": "+12155550123",
                "sms_opt_in": True,
                "sms_verified_at": timezone.now(),
            },
        )
    FeatureSetting.objects.create(key="sms", enabled=True)
    board = make_member(board=True, email="board1@example.com")
    sent = []
    fake = mock.Mock(available=True)
    fake.send.side_effect = lambda to, body: sent.append((to, body)) or mock.Mock(ok=True)
    with mock.patch("comms.sms.provider", return_value=fake):
        row = _start(client, owner, django_capture_on_commit_callbacks)
    assert row.status == "waiting" and row.wait_hours == 48 and row.lock_hours == 48
    assert row.ready_at - row.started_at == datetime.timedelta(hours=48)
    to_person = [m for m in mail.outbox if "Someone asked to recover" in m.subject]
    assert sorted(m.to[0] for m in to_person) == [
        "extra@example.com",
        "main@example.com",
        "register@example.com",
    ]
    assert all("If this wasn't you, cancel" in m.body for m in to_person)
    assert all(reverse("account_recovery_status") in m.body for m in to_person)
    to_board = [m for m in mail.outbox if m.to == [board.email]]
    assert to_board and "Account recovery requested" in to_board[0].subject
    assert reverse("recovery_board", args=[row.pk]) in to_board[0].body
    assert sent and sent[0][0] == "+12155550123" and "cancel" in sent[0][1]
    assert "main@example.com" not in sent[0][1]
    event = AuditEvent.objects.get(action="account_recovery.started")
    assert event.metadata["emails"] == 3 and event.metadata["sms"] is True


def test_no_text_when_the_feature_is_off(client, django_capture_on_commit_callbacks):
    owner = _owner()
    with system_access("t"):
        ContactProfile.objects.update_or_create(
            person=_person(owner),
            defaults={
                "sms_number": "+12155550199",
                "sms_opt_in": True,
                "sms_verified_at": timezone.now(),
            },
        )
    fake = mock.Mock(available=True)
    with mock.patch("comms.sms.provider", return_value=fake):
        _start(client, owner, django_capture_on_commit_callbacks)
    fake.send.assert_not_called()


def test_ineligible_at_confirm_time_is_refused(client, django_capture_on_commit_callbacks):
    owner = _owner()
    _send(owner, django_capture_on_commit_callbacks)
    token = _links_in(mail.outbox[-1])[0]
    client.get(reverse("recovery_link", args=[token]))
    community.grant_role(
        person=_person(owner),
        role=Role.MANAGER,
        start_date=days_ago(1),
        appointed_by="board",
        actor=SYSTEM,
    )
    page = client.post(reverse("recovery_continue"))
    assert page.status_code == 400 and b"assisted reset" in page.content
    assert recovery.open_recovery(owner) is None


# --- The wait is enforced --------------------------------------------------------------


def test_asking_again_during_the_wait_sends_no_link(client, django_capture_on_commit_callbacks):
    owner = _owner()
    _start(client, owner, django_capture_on_commit_callbacks)
    assert _send(owner, django_capture_on_commit_callbacks) == "waiting"
    assert mail.outbox and not _links_in(mail.outbox[-1])
    assert "still waiting" in mail.outbox[-1].subject


def test_still_waiting_notices_are_capped_per_day(client, django_capture_on_commit_callbacks):
    """D87 (R2-004): asking again and again cannot flood the person's inboxes."""
    owner = _owner()
    _start(client, owner, django_capture_on_commit_callbacks)
    assert _send(owner, django_capture_on_commit_callbacks) == "waiting"
    assert len(mail.outbox) >= 1
    for _ in range(5):
        assert _send(owner, django_capture_on_commit_callbacks) == "waiting_limit"
        assert mail.outbox == []
    # A day later (the recovery still waits) one more notice may go.
    later = timezone.now() + datetime.timedelta(hours=25)
    with mock.patch.object(recovery.timezone, "now", return_value=later):
        assert _send(owner, django_capture_on_commit_callbacks) == "waiting"
    assert mail.outbox and "still waiting" in mail.outbox[-1].subject


def test_cannot_finish_before_the_wait_is_over(client, django_capture_on_commit_callbacks):
    owner = _owner()
    row = _start(client, owner, django_capture_on_commit_callbacks)
    token = recovery._new_link(owner, RecoveryLinkPurpose.FINISH, row)
    client.get(reverse("recovery_link", args=[token]))
    page = client.post(reverse("recovery_continue"))
    assert page.status_code == 200 and b"not over yet" in page.content
    assert Authenticator.objects.filter(user=owner).exists()
    assert "_auth_user_id" not in client.session
    row.refresh_from_db()
    assert row.completed_at is None


def test_finishing_only_in_the_browser_session_that_opened_the_link(
    client, django_capture_on_commit_callbacks
):
    from django.test import Client

    owner = _owner()
    row = _start(client, owner, django_capture_on_commit_callbacks)
    _past_wait(row)
    _send(owner, django_capture_on_commit_callbacks)
    token = _links_in(mail.outbox[-1])[0]
    client.get(reverse("recovery_link", args=[token]))
    other = Client()
    assert other.post(reverse("recovery_continue")).status_code == 404
    assert Authenticator.objects.filter(user=owner).exists()


def test_finish_removes_old_authenticators_and_opens_enrollment_here(
    client, django_capture_on_commit_callbacks
):
    owner = _owner()
    old = Authenticator.objects.filter(user=owner).values_list("pk", flat=True)
    old = set(old)
    generation = owner.session_generation
    row = _recovered(client, owner, django_capture_on_commit_callbacks)
    assert row.status == "completed"
    assert not Authenticator.objects.filter(user=owner).exists()
    owner.refresh_from_db()
    assert owner.session_generation == generation + 1
    assert int(client.session["_auth_user_id"]) == owner.pk
    assert client.session.get(enrollment.SESSION_KEY)
    assert client.session[enrollment.VIA_KEY] == "recovery"
    page = client.get(reverse("mfa_index")).content.decode()
    assert 'aria-current="step">Confirm recovery' not in page  # step 1 done
    assert "Use this phone's fingerprint or face (passkey)" in page
    # Enroll a new app: new recovery codes come with it.
    setup = client.get(reverse("mfa_activate_totp"))
    secret = setup.context["form"].secret
    response = client.post(reverse("mfa_activate_totp"), {"code": totp_code(secret)})
    assert response["Location"] == reverse("mfa_view_recovery_codes")
    new = set(Authenticator.objects.filter(user=owner).values_list("pk", flat=True))
    assert new and not (new & old)
    assert Authenticator.objects.filter(user=owner, type=Authenticator.Type.RECOVERY_CODES).exists()
    event = AuditEvent.objects.get(action="account_recovery.completed")
    assert event.metadata["recovery_id"] == row.pk and event.metadata["removed"] >= 1


def test_another_browser_without_the_window_is_signed_out(
    client, django_capture_on_commit_callbacks
):
    owner = _owner()
    _recovered(client, owner, django_capture_on_commit_callbacks)
    owner.refresh_from_db()
    from django.test import Client

    other = Client()
    other.force_login(owner)
    assert other.get(reverse("home"))["Location"] == reverse("account_setup_expired")


# --- Cancelling ------------------------------------------------------------------------


def test_board_cancels_from_my_items(client, django_capture_on_commit_callbacks):
    owner = _owner()
    row = _start(client, owner, django_capture_on_commit_callbacks)
    board = make_member(board=True)
    titles = [i.title for i in my_items.items_for(board)]
    assert any(t.startswith("Account recovery requested for") for t in titles)
    client.force_login(board)
    page = client.get(reverse("recovery_board", args=[row.pk]))
    assert page.status_code == 200 and b"Cancel the recovery" in page.content
    with django_capture_on_commit_callbacks(execute=True):
        client.post(reverse("recovery_board", args=[row.pk]))
    row.refresh_from_db()
    assert row.status == "cancelled" and row.cancel_reason == RecoveryCancelledBy.BOARD
    assert any("cancelled" in m.subject.lower() for m in mail.outbox)


def test_owner_cannot_open_the_board_page(client, django_capture_on_commit_callbacks):
    owner = _owner()
    row = _start(client, owner, django_capture_on_commit_callbacks)
    client.force_login(_owner("other@example.com"))
    assert client.get(reverse("recovery_board", args=[row.pk])).status_code == 403
    assert client.post(reverse("recovery_board", args=[row.pk])).status_code == 403


def test_person_cancels_from_the_signed_in_page(client, django_capture_on_commit_callbacks):
    owner = _owner()
    row = _start(client, owner, django_capture_on_commit_callbacks)
    client.force_login(owner)  # no authenticator used: does not cancel by itself
    page = client.get(reverse("account_recovery_status"))
    assert b"A recovery is waiting" in page.content
    client.post(reverse("account_recovery_status"))
    row.refresh_from_db()
    assert row.status == "cancelled" and row.cancel_reason == RecoveryCancelledBy.PERSON


def test_status_page_needs_sign_in(client):
    response = client.get(reverse("account_recovery_status"))
    assert response["Location"].startswith(reverse("account_login"))


def test_signing_in_with_the_existing_authenticator_cancels(
    client, django_capture_on_commit_callbacks
):
    from community.tests.factories import enroll_totp

    owner = make_member(lots=[LotFactory()], email="signin@example.com", mfa=False)
    owner.set_password(PASSWORD)
    owner.save()
    secret = enroll_totp(owner)
    row = _start(client, owner, django_capture_on_commit_callbacks)
    client.post(reverse("account_login"), {"login": owner.email, "password": PASSWORD})
    with django_capture_on_commit_callbacks(execute=True):
        client.post(reverse("mfa_authenticate"), {"code": totp_code(secret)})
    assert int(client.session["_auth_user_id"]) == owner.pk
    row.refresh_from_db()
    assert row.status == "cancelled" and row.cancel_reason == RecoveryCancelledBy.SIGNED_IN


# --- Limits ------------------------------------------------------------------------------


def _row(user, **kwargs):
    now = timezone.now()
    values = {
        "subject": user,
        "started_at": now - datetime.timedelta(days=1),
        "wait_hours": 48,
        "lock_hours": 48,
        "ready_at": now + datetime.timedelta(hours=47),
        "expires_at": now + datetime.timedelta(days=8),
    }
    values.update(kwargs)
    return AccountRecovery.objects.create(**values)


def test_at_most_three_recoveries_in_thirty_days(django_capture_on_commit_callbacks):
    owner = _owner()
    for _ in range(3):
        _row(owner, cancelled_at=timezone.now())
    assert _send(owner, django_capture_on_commit_callbacks) == "limit"
    assert mail.outbox == []


def test_one_open_request_per_login(client, django_capture_on_commit_callbacks):
    owner = _owner()
    _send(owner, django_capture_on_commit_callbacks)
    first = _links_in(mail.outbox[-1])[0]
    _send(owner, django_capture_on_commit_callbacks)
    second = _links_in(mail.outbox[-1])[0]
    client.get(reverse("recovery_link", args=[first]))
    client.post(reverse("recovery_continue"))
    from django.test import Client

    other = Client()
    other.get(reverse("recovery_link", args=[second]))
    page = other.post(reverse("recovery_continue"))
    assert page.status_code == 400 and b"already under way" in page.content
    assert AccountRecovery.objects.unscoped("t").filter(subject=owner).count() == 1


def test_links_per_day_are_limited(django_capture_on_commit_callbacks):
    owner = _owner()
    for _ in range(recovery.MAX_LINKS_PER_DAY):
        assert _send(owner, django_capture_on_commit_callbacks) == "sent_start"
    assert _send(owner, django_capture_on_commit_callbacks) == "link_limit"


def test_the_form_is_rate_limited_per_address(client):
    with mock.patch("accounts.tasks.send_recovery_link.defer"):
        codes = [
            client.post(reverse("recovery_request"), {"email": "x@example.com"}).status_code
            for _ in range(4)
        ]
    assert codes[:3] == [200, 200, 200] and codes[3] == 429


# --- Board settings: wait and lock ----------------------------------------------------


def test_minimums_and_maximums_are_enforced_on_the_settings():
    board = make_member(board=True)
    for key, value in (
        ("recovery.wait_hours", "24"),
        ("recovery.wait_hours", "47"),
        ("recovery.wait_hours", "337"),
        ("recovery.lock_hours", "12"),
        ("recovery.lock_hours", "400"),
    ):
        with pytest.raises(community.CommunityError):
            community.set_rule(key=key, value=value, actor=board)
    community.set_rule(key="recovery.wait_hours", value="72", citation="R-1", actor=board)
    community.set_rule(key="recovery.lock_hours", value="24", citation="R-1", actor=board)
    assert recovery.wait_hours() == 72 and recovery.lock_hours() == 24
    assert AuditEvent.objects.filter(action="community.rule_changed").count() == 2


def test_the_minimum_wait_holds_even_if_the_stored_value_is_lower():
    GoverningRule.objects.create(key="recovery.wait_hours", value="10")
    GoverningRule.objects.create(key="recovery.lock_hours", value="1")
    assert recovery.wait_hours() == 48
    assert recovery.lock_hours() == 24


def test_defaults_are_48_hours():
    assert recovery.wait_hours() == 48 and recovery.lock_hours() == 48


def test_a_change_applies_only_to_requests_started_afterwards(
    client, django_capture_on_commit_callbacks
):
    first = _owner("first@example.com")
    before = _start(client, first, django_capture_on_commit_callbacks)
    board = make_member(board=True)
    community.set_rule(key="recovery.wait_hours", value="96", citation="R-2", actor=board)
    community.set_rule(key="recovery.lock_hours", value="72", citation="R-2", actor=board)
    before.refresh_from_db()
    assert (before.wait_hours, before.lock_hours) == (48, 48)
    from django.test import Client

    second = _owner("second@example.com")
    after = _start(Client(), second, django_capture_on_commit_callbacks)
    assert (after.wait_hours, after.lock_hours) == (96, 72)
    assert after.ready_at - after.started_at == datetime.timedelta(hours=96)


def test_settings_show_on_community_settings(client):
    client.force_login(make_member(board=True))
    page = client.get(reverse("community_settings")).content.decode()
    assert "Account recovery" in page and "recovery.wait_hours" in page


# --- The lock after a recovery ---------------------------------------------------------


@pytest.fixture
def locked(client, django_capture_on_commit_callbacks):
    owner = _owner("locked@example.com")
    row = _recovered(client, owner, django_capture_on_commit_callbacks)
    owner.refresh_from_db()
    return owner, row


def _lot_and_account(user):
    lot_id = next(iter(roles_for(user).owned_lot_ids))
    with system_access("t"):
        from community.models import Lot

        lot = Lot.objects.get(pk=lot_id)
        account = Account.objects.filter(lot=lot).first()
    return lot, account


def _locked_calls(user):
    from comms import sms
    from governance import elections, surveys
    from governance import services as governance
    from governance.models import Election, Meeting, Proxy, Survey, Vote
    from payments import autopay

    lot, account = _lot_and_account(user)
    person = _person(user)
    with system_access("t"):
        method = person.contact_methods.model(person=person, kind="email", value="m@example.com")
    return {
        "vote": lambda: governance.cast_ballot(Vote(), option_id=1, actor=user),
        "election ballot": lambda: elections.cast_election_ballot(
            Election(), lot_id=lot.pk, candidate_ids=[1], actor=user
        ),
        "survey": lambda: surveys.submit_response(Survey(), answers={}, actor=user),
        "give proxy": lambda: elections.grant_proxy(
            Meeting(), lot=lot, holder=PersonFactory(), actor=user
        ),
        "revoke proxy": lambda: elections.revoke_proxy(Proxy(lot_id=lot.pk), actor=user),
        "add email": lambda: community.add_contact_method(
            person, kind=ContactMethodKind.EMAIL, value="new@example.com", actor=user
        ),
        "add phone": lambda: community.add_contact_method(
            person,
            kind=ContactMethodKind.PHONE,
            value="215-555-0100",
            phone_type="mobile",
            actor=user,
        ),
        "remove contact": lambda: community.remove_contact_method(method, actor=user),
        "email notices": lambda: community.set_method_notices(method, notices=True, actor=user),
        "mailing address": lambda: community.set_mailing_address(user=user, to_home=True),
        "notice choice": lambda: community.record_notice_choice(user=user, consent=True),
        "old profile phone": lambda: community.update_my_profile(user=user, phone="215-555-0101"),
        # D87 (R2-005): what reaches the owner's inbox is locked too.
        "statement emails": lambda: community.update_my_profile(user=user, statement_emails=False),
        "weekly digest": lambda: community.update_my_profile(user=user, weekly_digest=True),
        "text number": lambda: community.set_sms_number(user=user, number="+12155550100"),
        "texts start": lambda: sms.start_verification(user=user, number="215-555-0100"),
        "texts confirm": lambda: sms.confirm(user=user, code="123456"),
        "texts stop": lambda: sms.stop(user=user),
        "autopay setup": lambda: autopay.start_setup(user, lot, method_type="card"),
        "autopay cancel": lambda: autopay.cancel(user, lot),
        "account change": lambda: community.request_account_change(
            account, action="add", kind="authorized", name="X", email="x@example.com", actor=user
        ),
    }


def test_the_lock_blocks_every_listed_service(locked):
    owner, _row = locked
    calls = _locked_calls(owner)
    for what, call in calls.items():
        with pytest.raises(Exception) as caught:
            call()
        assert "recently recovered" in str(caught.value), what
        assert "available again on" in str(caught.value), what


def test_the_lock_message_says_when(locked):
    owner, row = locked
    with pytest.raises(community.CommunityError) as caught:
        community.record_notice_choice(user=owner, consent=True)
    assert recovery.when(row.lock_until) in str(caught.value)
    assert str(caught.value).startswith("For your security, this is available again on ")


def test_directory_choice_stays_open_during_the_lock(locked):
    """The directory stays open; the email choices may be resent unchanged
    (the form always posts them) but not changed (D87)."""
    owner, _row = locked
    profile = community.update_my_profile(
        user=owner, in_directory=True, weekly_digest=False, statement_emails=True
    )
    assert profile.in_directory and profile.statement_emails and not profile.weekly_digest


def test_the_lock_ends_after_48_hours(locked):
    owner, row = locked
    later = row.completed_at + datetime.timedelta(hours=48, minutes=1)
    with mock.patch("django.utils.timezone.now", return_value=later):
        assert recovery.locked_until(owner) is None
        community.record_notice_choice(user=owner, consent=True)
    almost = row.completed_at + datetime.timedelta(hours=47)
    with mock.patch("django.utils.timezone.now", return_value=almost):
        assert recovery.locked_until(owner) == row.lock_until


def test_banner_while_locked(client, locked):
    owner, row = locked
    from community.tests.factories import enroll_totp

    enroll_totp(owner)
    client.force_login(owner)
    page = client.get(reverse("home")).content.decode()
    assert "Your sign-in was recently recovered." in page
    assert recovery.when(row.lock_until) in page
    later = row.completed_at + datetime.timedelta(hours=49)
    with mock.patch("django.utils.timezone.now", return_value=later):
        page = client.get(reverse("my_profile")).content.decode()
    assert "Your sign-in was recently recovered." not in page


def test_lock_refusal_shows_on_the_profile_form(client, locked):
    owner, _row = locked
    from community.tests.factories import enroll_totp

    enroll_totp(owner)
    client.force_login(owner)
    page = client.post(
        reverse("my_profile"), {"section": "notices", "choice": "email"}
    ).content.decode()
    assert "recently recovered" in page


def test_board_sees_the_completed_recovery_for_seven_days(locked):
    _owner_user, row = locked
    board = make_member(board=True)
    titles = [i.title for i in my_items.items_for(board)]
    assert any("recovered their sign-in; review if unexpected" in t for t in titles)
    AccountRecovery.objects.unscoped("t").filter(pk=row.pk).update(
        completed_at=timezone.now() - datetime.timedelta(days=8)
    )
    titles = [i.title for i in my_items.items_for(board)]
    assert not any("recovered their sign-in" in t for t in titles)


def test_owners_never_see_board_recovery_items(locked):
    other = _owner("neighbor@example.com")
    assert not any("recover" in i.title.lower() for i in my_items.items_for(other))


# --- Audit -----------------------------------------------------------------------------


def test_audit_metadata_is_allowlisted_and_has_no_contact_details(client, locked):
    events = AuditEvent.objects.filter(action__startswith="account_recovery.")
    assert {e.action for e in events} >= {
        "account_recovery.link_sent",
        "account_recovery.started",
        "account_recovery.completed",
    }
    for event in events:
        assert set(event.metadata) <= ACTIONS[event.action].metadata_fields
        for value in event.metadata.values():
            assert "@" not in str(value) and "+1" not in str(value)


# --- Pages ------------------------------------------------------------------------------


def test_signin_help_explains_recovery_and_the_board_reset(client):
    """D62: Can't sign in?, lost phone, no recovery code or other device."""
    page = client.get(reverse("signin_help_device")).content.decode()
    assert "recovery code" in page and "another device" in page
    response = client.get(reverse("signin_help_device"), {"have": "no"})
    assert response["Location"] == reverse("recovery_request")
    page = client.get(reverse("recovery_request")).content.decode()
    assert "48 hours" in page and "two board members" in page


def test_recovery_request_page_is_a_sign_in_step(client):
    page = client.get(reverse("recovery_request"))
    assert page.status_code == 200 and b"Recover your sign-in" in page.content
    assert "_auth_user_id" not in client.session


def test_contacts_list_covers_every_email_and_only_an_opted_in_number():
    owner = _owner("c@example.com")
    person = _person(owner)
    with system_access("t"):
        ContactProfile.objects.update_or_create(
            person=person, defaults={"sms_number": "+12155550111", "sms_opt_in": False}
        )
    emails, number = community.recovery_contacts(owner)
    assert "c@example.com" in emails and number == ""


def test_recovery_on_record_is_easy_to_query(client, django_capture_on_commit_callbacks):
    owner = _owner()
    assert not recovery.has_recovery_on_record(owner)
    _start(client, owner, django_capture_on_commit_callbacks)
    assert recovery.has_recovery_on_record(owner)


def test_password_reset_never_opens_the_enrollment_window(
    client, django_capture_on_commit_callbacks
):
    from community.tests.factories import enroll_totp

    owner = make_member(lots=[LotFactory()], email="pwreset@example.com", mfa=False)
    owner.set_password(PASSWORD)
    owner.save()
    secret = enroll_totp(owner)
    mail.outbox = []
    with django_capture_on_commit_callbacks(execute=True):
        client.post(reverse("account_reset_password"), {"email": owner.email})
    path = re.search(r"(/accounts/password/reset/key/[^\s]+/)", mail.outbox[-1].body).group(1)
    url = client.get(path)["Location"]
    client.post(url, {"step": "verify", "code": totp_code(secret)})
    new = "another long passphrase 2026"  # nosec B105 - test-only
    client.post(url, {"password1": new, "password2": new})
    owner.refresh_from_db()
    assert owner.check_password(new)
    assert enrollment.SESSION_KEY not in client.session
    assert "_auth_user_id" not in client.session
    assert Authenticator.objects.filter(user=owner, type="totp").exists()
