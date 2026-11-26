"""Add to calendar (D49): .ics building (RFC 5545 escaping and folding,
UTC times, stable UID, cancellation) and the Google / Microsoft links, with
the location rule for in-person, online-only and hybrid meetings."""

import datetime
from urllib.parse import parse_qs, urlparse

import pytest
from django.core.exceptions import ValidationError

from comms import calendar_links as cl

START = datetime.datetime(2026, 12, 3, 23, 30, tzinfo=datetime.UTC)  # 6:30 PM New York
JOIN = "https://meet.example.com/j/123?pwd=a,b;c"


def item(place="", join="", **kw):
    data = {
        "uid": "meeting-7@portal.example",
        "title": "Board meeting",
        "starts_at": START,
        "ends_at": START + datetime.timedelta(hours=1),
        "place": place,
        "join_url": join,
        "details": "Board meeting. The agenda and minutes are on the portal page.",
        "page_url": "https://portal.example/meetings/7/",
    }
    data.update(kw)
    return cl.CalendarItem(**data)


def unfold(text: str) -> str:
    return text.replace("\r\n ", "")


def prop(text: str, name: str) -> str:
    for line in unfold(text).split("\r\n"):
        if line.startswith(name + ":"):
            return line[len(name) + 1 :]
    raise AssertionError(name)


def ics_unescape(value: str) -> str:
    return value.replace("\\n", "\n").replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")


def q(url: str) -> dict:
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


CASES = [
    ("Clubhouse", "", "Clubhouse"),
    ("", JOIN, JOIN),
    ("Clubhouse", JOIN, f"Clubhouse · {JOIN}"),
]


@pytest.mark.parametrize("place,join,expected", CASES)
def test_location_in_every_target(place, join, expected):
    it = item(place, join)
    assert it.location == expected
    assert ics_unescape(prop(cl.ics(it), "LOCATION")) == expected
    assert q(cl.google_url(it))["location"] == expected
    assert q(cl.outlook_com_url(it))["location"] == expected
    assert q(cl.microsoft_365_url(it))["location"] == expected


@pytest.mark.parametrize("place,join,expected", CASES)
def test_join_link_is_the_first_line_of_the_description(place, join, expected):
    it = item(place, join)
    desc = ics_unescape(prop(cl.ics(it), "DESCRIPTION"))
    if join:
        assert desc.splitlines()[0] == f"Join online: {JOIN}"
        assert q(cl.google_url(it))["details"].startswith(f"Join online: {JOIN}")
        assert q(cl.outlook_com_url(it))["body"].startswith(f"Join online: {JOIN}")
    else:
        assert "Join online" not in desc
    assert desc.endswith("Portal page (sign-in required): https://portal.example/meetings/7/")


def test_times_are_utc_everywhere():
    it = item("Clubhouse")
    text = cl.ics(it)
    assert prop(text, "DTSTART") == "20261203T233000Z"
    assert prop(text, "DTEND") == "20261204T003000Z"
    assert q(cl.google_url(it))["dates"] == "20261203T233000Z/20261204T003000Z"
    out = q(cl.outlook_com_url(it))
    assert out["startdt"] == "2026-12-03T23:30:00Z" and out["enddt"] == "2026-12-04T00:30:00Z"
    assert out["path"] == "/calendar/action/compose" and out["rru"] == "addevent"
    assert cl.outlook_com_url(it).startswith(
        "https://outlook.live.com/calendar/0/deeplink/compose?"
    )
    assert cl.microsoft_365_url(it).startswith(
        "https://outlook.office.com/calendar/0/deeplink/compose?"
    )
    assert cl.google_url(it).startswith(
        "https://calendar.google.com/calendar/render?action=TEMPLATE&"
    )
    # A time in another zone is converted, not relabelled.
    eastern = START.astimezone(datetime.timezone(datetime.timedelta(hours=-5)))
    assert prop(cl.ics(item("x", starts_at=eastern)), "DTSTART") == "20261203T233000Z"


def test_ics_shape_escaping_and_folding():
    title = "Pool, spa; and \\ fence\nreview " + "é" * 60
    text = cl.ics(item("Clubhouse", title=title), now=START)
    assert text.startswith("BEGIN:VCALENDAR\r\nVERSION:2.0\r\n")
    assert text.endswith("END:VEVENT\r\nEND:VCALENDAR\r\n")
    assert "\n" not in text.replace("\r\n", "")  # only CRLF line breaks
    for line in text.split("\r\n"):
        assert len(line.encode("utf-8")) <= 75
        line.encode("utf-8").decode("utf-8")  # never cut inside a character
    summary = prop(text, "SUMMARY")
    assert summary.startswith("Pool\\, spa\\; and \\\\ fence\\nreview ")
    assert ics_unescape(summary) == title
    assert prop(text, "DTSTAMP") == "20261203T233000Z"
    assert prop(text, "URL") == "https://portal.example/meetings/7/"
    assert prop(text, "STATUS") == "CONFIRMED"


def test_control_characters_are_dropped():
    text = cl.ics(item("Club\x00house\x1b"))
    assert prop(text, "LOCATION") == "Clubhouse"


def test_uid_is_stable_and_cancellation_is_marked():
    first = cl.ics(item("Clubhouse"))
    again = cl.ics(item("Clubhouse", title="Board meeting (moved)"))
    assert prop(first, "UID") == prop(again, "UID") == "meeting-7@portal.example"
    cancelled = cl.ics(item("Clubhouse", cancelled=True))
    assert prop(cancelled, "STATUS") == "CANCELLED"
    assert prop(cancelled, "SEQUENCE") == "1"


def test_menu_offers_online_calendars_only_for_items_still_on():
    menu = cl.menu(item("Clubhouse"), "/meetings/7/meeting.ics")
    assert [x["label"] for x in menu["online"]] == [
        "Google Calendar",
        "Outlook.com",
        "Microsoft 365",
    ]
    assert [x["label"] for x in menu["files"]] == [
        "Apple Calendar (iPhone, Mac)",
        "Outlook (desktop)",
        "Other (.ics file)",
    ]
    assert menu["note"] == "Google and Microsoft links pass the meeting details to that service."
    cancelled = cl.menu(item("Clubhouse", cancelled=True), "/x.ics")
    assert cancelled["online"] == [] and len(cancelled["files"]) == 3


@pytest.mark.parametrize(
    "value",
    [
        "http://zoom.us/j/1",
        "javascript:alert(1)",
        "data:text/html,hi",
        "zoom.us/j/1",
        "https://user:pw@zoom.us/j/1",
        "https://zoom.us/j/1 2",
        "https://zoom.us/" + "a" * 500,
    ],
)
def test_join_links_must_be_plain_https(value):
    with pytest.raises(ValidationError):
        cl.clean_join_url(value)


def test_join_link_accepted():
    assert cl.clean_join_url("  https://zoom.us/j/123?pwd=x  ") == "https://zoom.us/j/123?pwd=x"
    assert cl.clean_join_url("") == ""
