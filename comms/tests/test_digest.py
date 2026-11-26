"""Milestone 4: routine updates at once or in a weekly digest."""

import pytest
from django.core import mail
from django.urls import reverse

from comms import threads
from comms.models import DigestItem
from comms.notify import send_digests
from community.tests.factories import LotFactory, make_member
from operations import services as ops

pytestmark = pytest.mark.django_db


@pytest.fixture
def w():
    lot = LotFactory()
    owner = make_member(lots=[lot])
    return {"owner": owner, "board": make_member(board=True), "lot": lot}


def _reply(w, django_capture_on_commit_callbacks):
    req = ops.submit_request(
        lot=w["lot"], request_type="general", subject="Q", description="?", actor=w["owner"]
    )
    mail.outbox.clear()
    with django_capture_on_commit_callbacks(execute=True):
        threads.post("request", req, body="Answer", actor=w["board"])
    return req


def test_updates_are_sent_at_once_by_default(w, django_capture_on_commit_callbacks):
    _reply(w, django_capture_on_commit_callbacks)
    assert [m.to for m in mail.outbox] == [[w["owner"].email]]
    assert not DigestItem.objects.exists()


def test_weekly_digest_holds_updates_and_sends_one_summary(
    client, w, django_capture_on_commit_callbacks
):
    client.force_login(w["owner"])
    client.post(reverse("my_profile"), {"section": "prefs", "weekly_digest": "weekly"})
    _reply(w, django_capture_on_commit_callbacks)
    _reply(w, django_capture_on_commit_callbacks)
    assert mail.outbox == []
    assert DigestItem.objects.filter(user=w["owner"]).count() == 2
    assert send_digests() == 1
    summary = mail.outbox[-1]
    assert summary.to == [w["owner"].email] and "2 updates" in summary.body
    assert "Answer" not in summary.body  # links only, never the message
    assert not DigestItem.objects.exists()


def test_official_notices_ignore_the_digest(w):
    import datetime

    from django.utils import timezone

    from community.models import ContactProfile, Person
    from core.scoping import system_access
    from governance import services as gov

    with system_access("t"):
        person = Person.objects.get(user=w["owner"])
        ContactProfile.objects.update_or_create(
            person=person, defaults={"weekly_digest": True, "electronic_notice_consent": True}
        )
    meeting = gov.create_meeting(
        title="Annual",
        kind="annual",
        starts_at=timezone.now() + datetime.timedelta(days=20),
        actor=w["board"],
    )
    mail.outbox.clear()
    gov.send_notice(meeting, actor=w["board"])
    assert any(w["owner"].email in m.to for m in mail.outbox)


# --- Only when there is something new (D44) -------------------------------------------


def _digest_reader(user):
    from community.models import ContactProfile, Person
    from core.scoping import system_access

    with system_access("t"):
        person = Person.objects.get(user=user)
        ContactProfile.objects.update_or_create(person=person, defaults={"weekly_digest": True})


def test_no_summary_without_held_updates(w):
    _digest_reader(w["owner"])
    mail.outbox.clear()
    assert send_digests() == 0
    assert mail.outbox == []


def test_one_email_per_update_sends_nothing_without_updates(w, django_capture_on_commit_callbacks):
    from comms.notify import send_update

    mail.outbox.clear()
    with django_capture_on_commit_callbacks(execute=True):
        send_update(set(), subject="x", body="y", link="/requests/1/")
        send_update({""}, subject="x", body="y", link="/requests/1/")
    assert mail.outbox == []
    assert send_digests() == 0 and mail.outbox == []


def test_held_updates_the_person_can_no_longer_see_are_dropped_silently(
    w, django_capture_on_commit_callbacks
):
    from community import services as community
    from community.models import Account, Person
    from community.tests.factories import UserFactory, enroll_totp
    from core.policy import SYSTEM
    from core.scoping import system_access

    req = _reply(w, django_capture_on_commit_callbacks)
    # A tenant on the account held an update about the lot's request; then
    # the board ended their access: no role is left, so nothing is sent.
    user = UserFactory()
    person = community.record_person(display_name="Tenant", actor=SYSTEM)
    with system_access("t"):
        Person.objects.filter(pk=person.pk).update(user=user)
        account = Account.objects.get(lot=w["lot"], closed_on__isnull=True)
    enroll_totp(user)
    contact = community.add_account_contact(account, person=person, kind="tenant", actor=SYSTEM)
    _digest_reader(user)
    DigestItem.objects.create(
        user=user, subject="Update on your request", link=f"http://x/requests/{req.pk}/"
    )
    community.end_account_contact(contact, actor=SYSTEM)
    mail.outbox.clear()
    assert send_digests() == 0
    assert mail.outbox == []
    assert not DigestItem.objects.exists()


def test_a_former_owner_gets_only_what_their_history_still_opens(
    client, w, django_capture_on_commit_callbacks
):
    from community import services as community
    from community.models import Ownership
    from core.policy import SYSTEM
    from core.scoping import system_access

    _digest_reader(w["owner"])
    req = _reply(w, django_capture_on_commit_callbacks)
    with system_access("t"):
        ownership = Ownership.objects.get(person__user=w["owner"])
    community.end_ownership(ownership, actor=SYSTEM)
    client.force_login(w["owner"])
    opens = client.get(reverse("request_detail", args=[req.pk])).status_code == 200
    mail.outbox.clear()
    assert send_digests() == (1 if opens else 0)
    assert not DigestItem.objects.exists()


def test_only_visible_updates_are_listed(w, django_capture_on_commit_callbacks):
    _digest_reader(w["owner"])
    _reply(w, django_capture_on_commit_callbacks)
    DigestItem.objects.create(
        user=w["owner"], subject="Someone else's request", link="http://x/requests/999999/"
    )
    mail.outbox.clear()
    assert send_digests() == 1
    body = mail.outbox[-1].body
    assert "1 update)" in body and "Someone else's request" not in body
    assert not DigestItem.objects.exists()


def test_every_routine_update_link_has_a_visibility_check():
    """send_update callers link to these detail pages; each is checked through
    its own scoping before a summary lists it."""
    from comms import threads
    from comms.notify import VISIBILITY

    linked = set(threads.DETAIL_URLS.values()) | {
        "reservation_detail",
        "survey_detail",
        "signoff_detail",
        "event_detail",
    }
    assert linked <= set(VISIBILITY)
