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
    client.post(reverse("my_profile"), {"weekly_digest": "on"})
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
