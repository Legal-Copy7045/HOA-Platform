from datetime import timedelta
from unittest import mock

import pytest
from django.core import mail
from django.urls import reverse
from django.utils import timezone

from comms import services
from comms.models import AnnouncementRead
from comms.tasks import email_announcement
from community.models import Role
from community.tests.factories import LotFactory, make_member

pytestmark = pytest.mark.django_db


@pytest.fixture
def people():
    return {
        "board": make_member(board=True, email="board@example.com"),
        "owner": make_member(lots=[LotFactory()], email="owner@example.com"),
        "manager": make_member(roles=[Role.MANAGER], email="manager@example.com"),
        "sysadmin": make_member(roles=[Role.SYSADMIN], email="sysadmin@example.com"),
    }


def publish(actor, **kw):
    with mock.patch("comms.tasks.email_announcement.defer"):
        return services.publish_announcement(
            title=kw.pop("title", "Pond work"), body="Details", actor=actor, **kw
        )


def test_board_and_manager_publish_owner_cannot(client, people):
    publish(people["board"])
    publish(people["manager"])
    client.force_login(people["owner"])
    response = client.post(
        reverse("announcements"), {"title": "x", "body": "y", "audience": "members"}
    )
    assert response.status_code == 403


def test_email_goes_to_the_right_audience_with_a_link_only(people):
    members = publish(people["board"], title="Members news")
    mail.outbox = []
    email_announcement(members.pk)
    to = sorted(m.to[0] for m in mail.outbox)
    assert to == ["board@example.com", "manager@example.com", "owner@example.com"]
    assert "Details" not in mail.outbox[0].body  # content stays behind sign-in
    board_only = publish(people["board"], title="Board news", audience="board")
    mail.outbox = []
    email_announcement(board_only.pk)
    assert sorted(m.to[0] for m in mail.outbox) == ["board@example.com", "manager@example.com"]


def test_reading_marks_once(client, people):
    a = publish(people["board"])
    client.force_login(people["owner"])
    client.get(reverse("announcement_detail", args=[a.pk]))
    client.get(reverse("announcement_detail", args=[a.pk]))
    assert AnnouncementRead.objects.filter(user=people["owner"]).count() == 1


def test_events(people):
    start = timezone.now() + timedelta(days=1)
    with pytest.raises(services.CommsError):
        services.create_event(
            title="x", starts_at=start, ends_at=start - timedelta(hours=1), actor=people["board"]
        )
    event = services.create_event(title="Annual meeting", starts_at=start, actor=people["board"])
    assert list(services.upcoming(people["owner"])) == [event]
    services.cancel_event(event, actor=people["manager"])
    event.refresh_from_db()
    assert event.cancelled
