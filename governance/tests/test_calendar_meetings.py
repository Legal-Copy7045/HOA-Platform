"""Meetings merged into Calendar (D49, owner decision 2026-11-26): one menu
entry, a Meetings / Events filter and Past meetings, "New meeting" beside
"New event", meeting places and join links, Add to calendar, rows that open
their page, and the meeting page's steps in order (WT-008, WT-044).
Executive sessions stay off the owners' calendar (WT-026)."""

import datetime
import re

import pytest
from django.core import mail
from django.urls import reverse
from django.utils import timezone

from comms import services as comms
from comms.models import Event
from community.models import Role
from community.tests.factories import LotFactory, make_member
from core import features
from documents.tests.helpers import pdf as pdf_upload
from governance import services as gov
from governance.models import Meeting
from governance.tests.test_surveys import _account, _contact_user

pytestmark = pytest.mark.django_db

JOIN = "https://meet.example.com/j/42"


@pytest.fixture
def w():
    lot_a, lot_b = LotFactory(), LotFactory()
    return {
        "board": make_member(board=True),
        "board2": make_member(board=True),
        "owner": make_member(lots=[lot_a]),
        "owner_b": make_member(lots=[lot_b]),
        "manager": make_member(roles=[Role.MANAGER]),
        "tenant": _contact_user(_account(lot_a), "tenant", "Tess Tenant"),
    }


def soon(days=10):
    return timezone.now() + datetime.timedelta(days=days)


def meeting(w, kind="board", title="Board meeting", **kw):
    kw.setdefault("location", "Clubhouse")
    return gov.create_meeting(
        title=title, kind=kind, starts_at=kw.pop("at", soon()), actor=w["board"], **kw
    )


def page(client, url):
    response = client.get(url)
    assert response.status_code == 200, url
    return response.content.decode()


def nav_items(response):
    return [i for s in response.context["nav"]["sections"] for i in s["items"]]


# --- One menu entry, filters, redirect ---------------------------------------------------


def test_meetings_address_leads_to_the_calendar_meetings_filter(client, w):
    for user in (w["owner"], w["board"]):
        client.force_login(user)
        response = client.get(reverse("meetings"))
        assert response.status_code == 302
        assert response["Location"] == reverse("calendar") + "?type=meetings"
    response = client.get(reverse("meetings") + "?view=past")
    assert response["Location"] == reverse("calendar") + "?type=past"


def test_one_menu_entry_and_meeting_pages_mark_calendar_current(client, w):
    m = meeting(w)
    client.force_login(w["owner"])
    labels = [i["label"] for i in nav_items(client.get(reverse("home")))]
    assert "Calendar" in labels and "Meetings" not in labels
    items = nav_items(client.get(reverse("meeting_detail", args=[m.pk])))
    assert [i["label"] for i in items if i["current"]] == ["Calendar"]


def test_with_the_calendar_off_meetings_keep_their_own_page(client, w):
    meeting(w, title="Spring board meeting")
    features.set_feature("calendar", False, actor=w["board"])
    client.force_login(w["owner"])
    response = client.get(reverse("meetings"))
    assert response.status_code == 200
    assert "Spring board meeting" in response.content.decode()
    labels = [i["label"] for i in nav_items(response)]
    assert "Meetings" in labels and "Calendar" not in labels
    assert client.get(reverse("calendar")).status_code == 404


def test_filters_all_meetings_events(client, w):
    m = meeting(w, title="Budget meeting")
    m.refresh_from_db()
    e = comms.create_event(title="Pool party", starts_at=soon(5), actor=w["board"])
    client.force_login(w["owner"])
    html = page(client, reverse("calendar"))
    assert "Budget meeting" in html and "Pool party" in html
    # Whole-row links: the meeting opens its meeting page, the event its event page.
    assert f'class="todo-link" href="{reverse("meeting_detail", args=[m.pk])}"' in html
    assert f'class="todo-link" href="{reverse("event_detail", args=[e.pk])}"' in html
    assert f'href="{reverse("event_detail", args=[m.calendar_event_id])}"' not in html
    html = page(client, reverse("calendar") + "?type=meetings")
    assert "Budget meeting" in html and "Pool party" not in html
    html = page(client, reverse("calendar") + "?type=events")
    assert "Budget meeting" not in html and "Pool party" in html
    assert 'aria-current="page">Events<' in html
    for label in ("All", "Meetings", "Events", "Past meetings"):
        assert f">{label}</a>" in html


def test_executive_sessions_only_in_the_boards_view(client, w):
    ex = meeting(w, kind="executive", title="Executive session: counsel")
    past = meeting(w, kind="executive", title="Executive session: past", at=soon(-10))
    for user in (w["owner"], w["tenant"]):
        client.force_login(user)
        for query in ("", "?type=meetings", "?type=past"):
            assert "Executive session" not in page(client, reverse("calendar") + query)
        assert client.get(reverse("meeting_ics", args=[ex.pk])).status_code == 404
        assert client.get(reverse("meeting_detail", args=[past.pk])).status_code == 404
    client.force_login(w["board"])
    html = page(client, reverse("calendar") + "?type=meetings")
    assert "Executive session: counsel" in html and "Board only" in html
    assert "Executive session: past" in page(client, reverse("calendar") + "?type=past")
    ics = client.get(reverse("meeting_ics", args=[ex.pk]))
    assert ics.status_code == 200
    assert not Event.objects.filter(title__startswith="Executive").exists()


def test_past_meetings_show_minutes_only_once_approved(client, w):
    held = meeting(w, title="October board meeting", at=soon(-20))
    gov.set_meeting_status(held, "held", actor=w["board"])
    doc = gov.upload_minutes(held, file=pdf_upload("october.pdf"), actor=w["board"])
    minutes_url = reverse("document_detail", args=[doc.pk])
    client.force_login(w["owner"])
    html = page(client, reverse("calendar") + "?type=past")
    assert "October board meeting" in html and minutes_url not in html
    assert "Minutes not yet approved" in html
    client.force_login(w["manager"])
    assert "Draft minutes" in page(client, reverse("calendar") + "?type=past")
    gov.approve_minutes(held, actor=w["board2"])
    client.force_login(w["owner"])
    html = page(client, reverse("calendar") + "?type=past")
    assert minutes_url in html


# --- New meeting next to New event ----------------------------------------------------------


@pytest.mark.parametrize("who", ["board", "manager"])
def test_new_meeting_beside_new_event_for_the_board_side(client, w, who):
    client.force_login(w[who])
    html = page(client, reverse("calendar"))
    assert 'data-testid="new-event"' in html and 'data-testid="new-meeting"' in html
    assert 'id="new-meeting"' in html and 'action="/meetings/"' in html
    # One control per form: with the panel open, its header button goes.
    html = page(client, reverse("calendar") + "?new=meeting")
    assert 'data-testid="new-meeting"' not in html
    assert re.search(r'open id="new-meeting"', html)


def test_owners_get_no_new_buttons(client, w):
    client.force_login(w["owner"])
    html = page(client, reverse("calendar"))
    assert "new-meeting" not in html and "new-event" not in html
    assert client.post(reverse("meetings"), {"title": "x"}).status_code == 403


def _post(client, **data):
    base = {"title": "Spring meeting", "kind": "board", "starts_at": "2027-03-01T18:30"}
    return client.post(reverse("meetings"), {**base, **data})


def test_a_meeting_needs_a_place_or_a_join_link(client, w):
    client.force_login(w["board"])
    response = _post(client, agenda="1. Budget")
    assert response.status_code == 200
    html = response.content.decode()
    assert "a place, a link to join online, or both" in html
    assert re.search(r'open id="new-meeting"', html)
    assert "1. Budget" in html  # what was typed is kept
    assert not Meeting.objects.exists()
    with pytest.raises(gov.GovernanceError):
        gov.create_meeting(title="x", kind="board", starts_at=soon(), actor=w["board"])


@pytest.mark.parametrize("bad", ["http://meet.example.com/j/1", "javascript:alert(1)"])
def test_join_link_must_be_https(client, w, bad):
    client.force_login(w["board"])
    response = _post(client, join_url=bad)
    assert response.status_code == 200
    assert "https://" in response.content.decode()
    assert not Meeting.objects.exists()
    with pytest.raises(gov.GovernanceError):
        gov.create_meeting(
            title="x", kind="board", starts_at=soon(), join_url=bad, actor=w["board"]
        )


@pytest.mark.parametrize(
    "place,join",
    [("Clubhouse", ""), ("", JOIN), ("Clubhouse", JOIN)],
    ids=["place", "online", "hybrid"],
)
def test_meeting_where_is_mirrored_to_its_event(client, w, place, join):
    client.force_login(w["board"])
    response = _post(client, location=place, join_url=join, ends_at="2027-03-01T20:00")
    m = Meeting.objects.get()
    assert response.status_code == 302 and response["Location"] == reverse(
        "meeting_detail", args=[m.pk]
    )
    event = Event.objects.get(pk=m.calendar_event_id)
    assert (event.location, event.join_url) == (place, join)
    assert event.ends_at == m.ends_at and m.ends_at is not None


def test_an_end_before_the_start_is_refused(client, w):
    client.force_login(w["board"])
    response = _post(client, location="Clubhouse", ends_at="2027-03-01T17:00")
    assert response.status_code == 200 and not Meeting.objects.exists()


def test_older_meetings_without_a_place_stay_readable(client, w):
    m = meeting(w)
    Meeting.objects.filter(pk=m.pk).update(location="", join_url="")
    client.force_login(w["owner"])
    assert client.get(reverse("meeting_detail", args=[m.pk])).status_code == 200
    assert client.get(reverse("meeting_ics", args=[m.pk])).status_code == 200


# --- The join link --------------------------------------------------------------------------


def test_join_link_on_the_page_never_in_the_notice_email(client, w):
    m = meeting(w, kind="annual", title="Annual meeting", join_url=JOIN)
    client.force_login(w["owner"])
    html = page(client, reverse("meeting_detail", args=[m.pk]))
    assert f'href="{JOIN}" target="_blank" rel="noopener noreferrer"' in html
    m.refresh_from_db()
    assert JOIN in page(client, reverse("event_detail", args=[m.calendar_event_id]))
    from community import services as community

    community.update_my_profile(user=w["owner"], electronic_notice_consent=True)
    mail.outbox.clear()
    gov.send_notice(m, actor=w["board"])
    assert mail.outbox
    for message in mail.outbox:
        assert JOIN not in message.body and "meet.example.com" not in message.body
        assert "Clubhouse, or online (the link to join is on the meeting page)" in message.body
        assert reverse("meeting_detail", args=[m.pk]) in message.body


def test_event_form_takes_an_optional_join_link(client, w):
    client.force_login(w["board"])
    data = {"title": "Webinar", "starts_at": "2027-02-01T19:00", "audience": "members"}
    response = client.post(reverse("calendar"), {**data, "join_url": "http://x.example"})
    assert response.status_code == 200 and not Event.objects.exists()
    assert re.search(r'open id="new-event"', response.content.decode())
    client.post(reverse("calendar"), {**data, "join_url": JOIN})
    assert Event.objects.get().join_url == JOIN


# --- Add to calendar ------------------------------------------------------------------------


def test_add_to_calendar_menus(client, w):
    m = meeting(w, join_url=JOIN)
    m.refresh_from_db()
    e = comms.create_event(title="Pool party", starts_at=soon(5), location="Pool", actor=w["board"])
    client.force_login(w["owner"])
    for url, ics in (
        (reverse("meeting_detail", args=[m.pk]), reverse("meeting_ics", args=[m.pk])),
        (reverse("event_detail", args=[e.pk]), reverse("event_ics", args=[e.pk])),
        # A meeting's event adds the meeting (same entry as the meeting page).
        (reverse("event_detail", args=[m.calendar_event_id]), reverse("meeting_ics", args=[m.pk])),
        (reverse("calendar"), reverse("meeting_ics", args=[m.pk])),
    ):
        html = page(client, url)
        assert "Add to calendar" in html
        assert f'href="{ics}" download>Apple Calendar (iPhone, Mac)<' in html
        assert "Outlook (desktop)" in html and "Other (.ics file)" in html
        assert "Google and Microsoft links pass the meeting details to that service." in html
        for link in re.findall(
            r'<a href="(https://(?:calendar\.google|outlook)[^"]+)"([^>]*)>', html
        ):
            assert 'target="_blank" rel="noopener noreferrer"' in link[1]


def test_ics_downloads(client, w):
    m = meeting(w, title="Board, budget; review", join_url=JOIN, agenda="Secret agenda item")
    client.force_login(w["owner"])
    response = client.get(reverse("meeting_ics", args=[m.pk]))
    assert response.status_code == 200
    assert response["Content-Type"] == "text/calendar; charset=utf-8"
    assert response["Content-Disposition"].startswith("attachment;")
    assert "no-store" in response["Cache-Control"]
    body = response.content.decode().replace("\r\n ", "")
    assert f"UID:meeting-{m.pk}@" in body and "SUMMARY:Board\\, budget\\; review" in body
    assert "STATUS:CONFIRMED" in body and "Secret agenda item" not in body
    m.refresh_from_db()
    via_event = client.get(reverse("event_ics", args=[m.calendar_event_id])).content.decode()
    assert f"UID:meeting-{m.pk}@" in via_event
    gov.set_meeting_status(m, "cancelled", actor=w["board"])
    assert b"STATUS:CANCELLED" in client.get(reverse("meeting_ics", args=[m.pk])).content


def test_ics_is_scoped_like_the_page(client, w):
    board_event = comms.create_event(
        title="Board workshop", starts_at=soon(4), audience="board", actor=w["board"]
    )
    client.force_login(w["owner"])
    assert client.get(reverse("event_ics", args=[board_event.pk])).status_code == 404
    client.logout()
    response = client.get(reverse("event_ics", args=[board_event.pk]))
    assert response.status_code == 302 and response["Location"].startswith(reverse("account_login"))
    client.force_login(w["board"])
    assert client.get(reverse("event_ics", args=[board_event.pk])).status_code == 200


# --- Clicking an event opens its page -------------------------------------------------------


def test_search_and_dashboard_open_the_event_or_meeting_page(client, w):
    m = meeting(w, title="Quokka meeting")
    m.refresh_from_db()
    e = comms.create_event(title="Quokka picnic", starts_at=soon(3), actor=w["board"])
    client.force_login(w["owner"])
    html = page(client, reverse("search") + "?q=quokka")
    assert f'href="{reverse("event_detail", args=[e.pk])}"' in html
    assert f'href="{reverse("meeting_detail", args=[m.pk])}"' in html
    home = page(client, reverse("home"))
    assert f'class="todo-link" href="{reverse("meeting_detail", args=[m.pk])}"' in home
    assert f'class="todo-link" href="{reverse("event_detail", args=[e.pk])}"' in home


# --- Meeting page steps (WT-008, WT-044) -----------------------------------------------------


def test_steps_before_notice(client, w):
    m = meeting(w)
    client.force_login(w["board"])
    html = page(client, reverse("meeting_detail", args=[m.pk]))
    assert 'data-testid="step-notice"' in html
    # Mark held waits for the meeting; minutes wait for Mark held.
    assert (
        'aria-describedby="blocked-held"' in html
        and "Possible once the meeting has started" in html
    )
    assert 'aria-describedby="blocked-minutes"' in html
    assert 'name="how" value="upload"' not in html
    assert html.index("step-notice") < html.index("step-held")
    assert "data-collapse-narrow" in html and 'id="board-actions" open' in html
    assert "Cancel meeting" in html


def test_steps_after_notice_and_after_the_meeting(client, w):
    m = meeting(w, at=timezone.now() - datetime.timedelta(hours=1))
    gov.send_notice(m, actor=w["board"])
    client.force_login(w["board"])
    html = page(client, reverse("meeting_detail", args=[m.pk]))
    assert 'data-testid="step-notice"' not in html
    assert f'action="{reverse("meeting_action", args=[m.pk, "held"])}"' in html
    gov.set_meeting_status(m, "held", actor=w["board"])
    html = page(client, reverse("meeting_detail", args=[m.pk]))
    assert 'name="how" value="upload"' in html and "Mark held" not in html
    assert "Cancel meeting" not in html


def test_owners_see_no_board_steps(client, w):
    m = meeting(w)
    client.force_login(w["owner"])
    html = page(client, reverse("meeting_detail", args=[m.pk]))
    assert "board-actions" not in html and "Send notice" not in html
