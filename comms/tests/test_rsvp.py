"""Event RSVPs and volunteer sign-ups (comms.rsvp)."""

import csv
import io
import json
from datetime import timedelta
from unittest import mock

import pytest
from django.core import mail
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone

from audit.models import AuditEvent
from comms import rsvp
from comms import services as comms
from comms.models import EventRsvp, VolunteerSignup
from community import services as community
from community.models import Account, Role
from community.tests.factories import LotFactory, days_ago, make_member
from core.policy import SYSTEM

pytestmark = pytest.mark.django_db


@pytest.fixture
def world():
    lot_a, lot_b = LotFactory(), LotFactory()
    board = make_member(board=True, email="board@example.com")
    event = comms.create_event(
        title="Spring picnic", starts_at=timezone.now() + timedelta(days=3), actor=board
    )
    rsvp.save_settings(event, enabled=True, max_guests=2, actor=board)
    event.refresh_from_db()
    return {
        "lot_a": lot_a,
        "lot_b": lot_b,
        "owner_a": make_member(lots=[lot_a], email="a@example.com"),
        "owner_b": make_member(lots=[lot_b], email="b@example.com"),
        "board": board,
        "manager": make_member(roles=[Role.MANAGER]),
        "event": event,
    }


def _contact(world, kind):
    account = Account.objects.unscoped("t").get(lot=world["lot_a"], closed_on__isnull=True)
    user = make_member()
    community.add_account_contact(account, person=user.person, kind=kind, actor=SYSTEM)
    return user


# --- Answering ---------------------------------------------------------------------


def test_answer_and_change_keeps_one_row_with_history(world):
    e, a = world["event"], world["owner_a"]
    rsvp.answer(e, answer="maybe", actor=a)
    row = rsvp.answer(e, answer="going", guests=2, actor=a)
    assert EventRsvp.objects.unscoped("t").filter(event=e).count() == 1
    assert (row.answer, row.guests) == ("going", 2)
    assert [h.answer for h in row.history.order_by("history_id")] == ["maybe", "going"]
    row = rsvp.answer(e, answer="not_going", guests=2, actor=a)
    assert row.guests == 0  # guests only count when going


def test_one_answer_per_person_in_the_database(world):
    e, a = world["event"], world["owner_a"]
    rsvp.answer(e, answer="going", actor=a)
    with pytest.raises(IntegrityError), transaction.atomic():
        EventRsvp.objects.unscoped("t").create(event=e, person=a.person, answer="maybe")


def test_answers_need_rsvps_turned_on(world):
    plain = comms.create_event(
        title="Pond work", starts_at=timezone.now() + timedelta(days=2), actor=world["board"]
    )
    with pytest.raises(rsvp.RsvpError, match="not taking RSVPs"):
        rsvp.answer(plain, answer="going", actor=world["owner_a"])


def test_deadline_closes_answers_and_slots(world):
    e, a = world["event"], world["owner_a"]
    slot = rsvp.add_slot(e, title="Bring a dessert", capacity=3, actor=world["board"])
    rsvp.answer(e, answer="going", actor=a)
    rsvp.save_settings(
        e, enabled=True, deadline=timezone.now() + timedelta(hours=1), actor=world["board"]
    )
    with mock.patch("django.utils.timezone.now", return_value=timezone.now() + timedelta(hours=2)):
        with pytest.raises(rsvp.RsvpError, match="Answers closed"):
            rsvp.answer(e, answer="maybe", actor=a)
        with pytest.raises(rsvp.RsvpError, match="Answers closed"):
            rsvp.sign_up(slot, actor=a)
    with pytest.raises(rsvp.RsvpError, match="cannot be after"):
        rsvp.save_settings(
            e, enabled=True, deadline=e.starts_at + timedelta(hours=1), actor=world["board"]
        )


def test_default_deadline_is_the_start(world):
    e = world["event"]
    assert e.answers_close_at == e.starts_at
    after = e.starts_at + timedelta(minutes=1)
    with (
        mock.patch("django.utils.timezone.now", return_value=after),
        pytest.raises(rsvp.RsvpError, match="Answers closed"),
    ):
        rsvp.answer(e, answer="going", actor=world["owner_a"])


def test_cancelled_event_refuses_answers(world):
    comms.cancel_event(world["event"], actor=world["board"])
    with pytest.raises(rsvp.RsvpError, match="cancelled"):
        rsvp.answer(world["event"], answer="going", actor=world["owner_a"])


def test_cap_counts_guests_and_refuses_going_but_not_maybe(world):
    e, board = world["event"], world["board"]
    rsvp.save_settings(e, enabled=True, cap=3, max_guests=2, actor=board)
    rsvp.answer(e, answer="going", guests=1, actor=world["owner_a"])  # 2 of 3
    with pytest.raises(rsvp.RsvpError, match="Only 1 place left"):
        rsvp.answer(e, answer="going", guests=1, actor=world["owner_b"])
    rsvp.answer(e, answer="going", actor=world["owner_b"])  # 3 of 3
    late = make_member(lots=[LotFactory()])
    with pytest.raises(rsvp.RsvpError, match="full"):
        rsvp.answer(e, answer="going", actor=late)
    rsvp.answer(e, answer="maybe", actor=late)
    # Someone already going may change their own answer within the cap.
    rsvp.answer(e, answer="going", guests=0, actor=world["owner_a"])
    assert rsvp.counts(e)["headcount"] == 2


def test_guest_limit(world):
    e = world["event"]
    with pytest.raises(rsvp.RsvpError, match="up to 2 guests"):
        rsvp.answer(e, answer="going", guests=3, actor=world["owner_a"])
    rsvp.save_settings(e, enabled=True, max_guests=0, actor=world["board"])
    with pytest.raises(rsvp.RsvpError, match="guests are not included"):
        rsvp.answer(e, answer="going", guests=1, actor=world["owner_a"])


def test_tenant_and_authorized_contact_can_answer_view_only_cannot(world):
    e = world["event"]
    for kind in ("tenant", "authorized"):
        user = _contact(world, kind)
        assert rsvp.answer(e, answer="going", actor=user).person == user.person
    viewer = _contact(world, "view_only")
    with pytest.raises(PermissionDenied):
        rsvp.answer(e, answer="going", actor=viewer)


def test_former_owner_cannot_answer_or_sign_up(client, world):
    seller = make_member(lots=[LotFactory()])
    ownership = seller.person.ownerships.model.objects.unscoped("t").get(person=seller.person)
    community.end_ownership(ownership, end_date=days_ago(1), actor=SYSTEM)
    slot = rsvp.add_slot(world["event"], title="Set up tables", capacity=2, actor=world["board"])
    with pytest.raises(PermissionDenied):
        rsvp.answer(world["event"], answer="going", actor=seller)
    with pytest.raises(PermissionDenied):
        rsvp.sign_up(slot, actor=seller)
    client.force_login(seller)
    response = client.post(reverse("event_rsvp", args=[world["event"].pk]), {"answer": "going"})
    assert response.status_code in (403, 404)
    assert not EventRsvp.objects.unscoped("t").exists()


def test_owner_cannot_answer_a_board_only_event(world):
    secret = comms.create_event(
        title="Board session",
        starts_at=timezone.now() + timedelta(days=2),
        audience="board",
        actor=world["board"],
    )
    rsvp.save_settings(secret, enabled=True, actor=world["board"])
    with pytest.raises(PermissionDenied):
        rsvp.answer(secret, answer="going", actor=world["owner_a"])


def test_manager_sets_up_but_cannot_see_names(world):
    e = world["event"]
    rsvp.save_settings(e, enabled=True, cap=40, actor=world["manager"])
    rsvp.add_slot(e, title="Bring ice", capacity=2, actor=world["manager"])
    with pytest.raises(PermissionDenied):
        rsvp.answers_with_names(e, world["manager"])
    with pytest.raises(PermissionDenied):
        rsvp.save_settings(e, enabled=True, actor=world["owner_a"])
    with pytest.raises(PermissionDenied):
        rsvp.add_slot(e, title="x", capacity=1, actor=world["owner_a"])
    assert AuditEvent.objects.filter(action="event.rsvp_settings_saved").count() == 2
    assert AuditEvent.objects.filter(action="event.slot_added").count() == 1


# --- Volunteer slots ---------------------------------------------------------------


def test_slot_capacity_sign_up_and_leave(world):
    slot = rsvp.add_slot(world["event"], title="Bring a dessert", capacity=1, actor=world["board"])
    rsvp.sign_up(slot, actor=world["owner_a"])
    with pytest.raises(rsvp.RsvpError, match="already signed up"):
        rsvp.sign_up(slot, actor=world["owner_a"])
    with pytest.raises(rsvp.RsvpError, match="full"):
        rsvp.sign_up(slot, actor=world["owner_b"])
    with pytest.raises(rsvp.RsvpError, match="not signed up"):
        rsvp.leave(slot, actor=world["owner_b"])
    rsvp.leave(slot, actor=world["owner_a"])
    rsvp.sign_up(slot, actor=world["owner_b"])
    assert VolunteerSignup.objects.unscoped("t").filter(slot=slot).count() == 1
    history = VolunteerSignup.history.filter(slot_id=slot.pk)
    assert history.filter(history_type="-").count() == 1  # leaving is kept in history


def test_removed_slot_is_hidden_and_closed(world):
    slot = rsvp.add_slot(world["event"], title="Bring chairs", capacity=4, actor=world["board"])
    rsvp.remove_slot(slot, actor=world["manager"])
    assert rsvp.slots_for(world["event"], world["owner_a"]) == []
    with pytest.raises(rsvp.RsvpError, match="removed"):
        rsvp.sign_up(slot, actor=world["owner_a"])


# --- Privacy -----------------------------------------------------------------------


def test_owner_sees_counts_only_board_sees_names(client, world):
    e = world["event"]
    world["owner_b"].person.display_name = "Bravo Neighbor"
    world["owner_b"].person.save()
    rsvp.answer(e, answer="going", guests=2, actor=world["owner_b"])
    rsvp.answer(e, answer="maybe", actor=world["owner_a"])
    slot = rsvp.add_slot(e, title="Bring a dessert", capacity=3, actor=world["board"])
    rsvp.sign_up(slot, actor=world["owner_b"])
    client.force_login(world["owner_a"])
    page = client.get(reverse("event_detail", args=[e.pk])).content.decode()
    assert "Bravo Neighbor" not in page
    assert "Going 1" in page and "Maybe 1" in page and "1 of 3 filled" in page
    assert "Your answer" in page and reverse("event_rsvp_export", args=[e.pk]) not in page
    assert client.get(reverse("event_rsvp_export", args=[e.pk])).status_code == 403
    # Owner A's scoped queries never return owner B's rows.
    assert list(EventRsvp.objects.for_user(world["owner_a"])) == [
        rsvp.own_answer(e, world["owner_a"])
    ]
    assert not VolunteerSignup.objects.for_user(world["owner_a"]).exists()
    client.force_login(world["board"])
    page = client.get(reverse("event_detail", args=[e.pk])).content.decode()
    assert "Bravo Neighbor" in page


def test_tenant_answers_through_the_page(client, world):
    tenant = _contact(world, "tenant")
    client.force_login(tenant)
    url = reverse("event_rsvp", args=[world["event"].pk])
    response = client.post(url, {"answer": "going", "guests": "1"})
    assert response.status_code == 302
    assert rsvp.own_answer(world["event"], tenant).guests == 1


def test_refusal_rerenders_the_form_with_the_message(client, world):
    client.force_login(world["owner_a"])
    url = reverse("event_rsvp", args=[world["event"].pk])
    response = client.post(url, {"answer": "going", "guests": "5"})
    assert response.status_code == 200
    assert b"up to 2 guests" in response.content
    assert not EventRsvp.objects.unscoped("t").exists()


def test_board_page_forms(client, world):
    e = world["event"]
    client.force_login(world["board"])
    response = client.post(
        reverse("event_rsvp_settings", args=[e.pk]),
        {"enabled": "on", "cap": "25", "max_guests": "3", "remind": "on"},
    )
    assert response.status_code == 302
    e.refresh_from_db()
    assert (e.rsvp_cap, e.rsvp_max_guests, e.rsvp_remind) == (25, 3, True)
    response = client.post(
        reverse("event_slot_add", args=[e.pk]), {"title": "Set up tables 9-10am", "capacity": "4"}
    )
    assert response.status_code == 302
    slot = e.volunteer_slots.get()
    response = client.post(reverse("event_slot_add", args=[e.pk]), {"title": "", "capacity": "0"})
    assert response.status_code == 200 and b"Nothing has been saved yet" in response.content
    client.force_login(world["owner_a"])
    client.post(reverse("event_slot_action", args=[slot.pk, "join"]))
    assert rsvp.slots_for(e, world["owner_a"])[0].mine
    assert client.post(reverse("event_slot_action", args=[slot.pk, "remove"])).status_code == 403
    client.post(reverse("event_slot_action", args=[slot.pk, "leave"]))
    assert not rsvp.slots_for(e, world["owner_a"])[0].mine


def test_calendar_badge_and_dashboard(client, world):
    e = world["event"]
    rsvp.answer(e, answer="going", actor=world["owner_a"])
    client.force_login(world["owner_a"])
    page = client.get(reverse("calendar")).content.decode()
    assert "RSVP" in page and "You're going" in page
    home = client.get(reverse("home")).content.decode()
    assert "Events I'm going to" in home and "Spring picnic" in home
    client.force_login(world["owner_b"])
    assert "Events I'm going to" not in client.get(reverse("home")).content.decode()


# --- Export ------------------------------------------------------------------------


def test_board_export_is_audited(client, world):
    e = world["event"]
    rsvp.answer(e, answer="going", guests=1, actor=world["owner_a"])
    slot = rsvp.add_slot(e, title="Bring a dessert", capacity=3, actor=world["board"])
    rsvp.sign_up(slot, actor=world["owner_b"])
    client.force_login(world["board"])
    response = client.get(reverse("event_rsvp_export", args=[e.pk]))
    assert response.status_code == 200
    rows = list(csv.reader(io.StringIO(response.content.decode())))
    assert rows[0] == ["Kind", "Name", "Answer or slot", "Guests", "Updated"]
    assert [r[0] for r in rows[1:]] == ["RSVP", "Volunteer"]
    event = AuditEvent.objects.filter(action="list.exported").latest("id")
    assert event.metadata == {"list": "event_rsvps", "rows": 2}
    client.force_login(world["manager"])
    assert client.get(reverse("event_rsvp_export", args=[e.pk])).status_code == 403


def test_own_data_export_has_only_my_answers(client, world):
    e = world["event"]
    rsvp.answer(e, answer="going", actor=world["owner_a"])
    rsvp.answer(e, answer="maybe", actor=world["owner_b"])
    client.force_login(world["owner_a"])
    data = json.loads(client.get(reverse("export_my_data")).content)
    assert [r["answer"] for r in data["events"]["rsvps"]] == ["going"]


# --- Reminder ----------------------------------------------------------------------


def test_reminder_goes_once_to_people_going_with_a_link_only(world):
    e = world["event"]
    rsvp.save_settings(e, enabled=True, max_guests=2, remind=True, actor=world["board"])
    rsvp.answer(e, answer="going", actor=world["owner_a"])
    rsvp.answer(e, answer="maybe", actor=world["owner_b"])
    assert rsvp.send_reminders() == 0  # three days away: not yet
    later = e.starts_at - timedelta(hours=20)
    mail.outbox = []
    with mock.patch("django.db.transaction.on_commit", side_effect=lambda fn: fn()):
        assert rsvp.send_reminders(now=later) == 1
        assert rsvp.send_reminders(now=later) == 0
    assert [m.to for m in mail.outbox] == [["a@example.com"]]
    assert reverse("event_detail", args=[e.pk]) in mail.outbox[0].body
    assert AuditEvent.objects.filter(action="event.reminders_sent").count() == 1


def test_reminder_honours_the_weekly_digest(world):
    from comms.models import DigestItem
    from community.models import ContactProfile

    e = world["event"]
    rsvp.save_settings(e, enabled=True, remind=True, actor=world["board"])
    rsvp.answer(e, answer="going", actor=world["owner_a"])
    ContactProfile.objects.unscoped("t").update_or_create(
        person=world["owner_a"].person, defaults={"weekly_digest": True}
    )
    mail.outbox = []
    with mock.patch("django.db.transaction.on_commit", side_effect=lambda fn: fn()):
        rsvp.send_reminders(now=e.starts_at - timedelta(hours=20))
    assert mail.outbox == []
    assert DigestItem.objects.filter(user=world["owner_a"]).count() == 1


def test_reminder_task_is_scheduled():
    from comms.tasks import event_reminders

    assert event_reminders.name.endswith("event_reminders")
