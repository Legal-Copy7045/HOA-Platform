"""Add to calendar (D49): a signed-in .ics file and Google / Microsoft links
for one event or meeting, built from what that person's page already shows.

Rules:
- Location always carries the location: the place; the join link when the
  meeting is online only; "place · join link" when it is both (the join link
  is then also the first line of the description).
- The description holds the event's own text (shown on its page) or, for a
  meeting, only its type: never the agenda, so nothing of an executive
  session beyond its title and time leaves the portal.
- No public feed: the .ics file is served only after sign-in, through the
  same scoping as the page (comms.views.event_ics, governance.views.meeting_ics).
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from urllib.parse import urlencode, urlparse

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.utils import timezone

JOIN_URL_MAX = 500
DEFAULT_LENGTH = datetime.timedelta(hours=1)
NOTE = "Google and Microsoft links pass the meeting details to that service."

_https_only = URLValidator(schemes=["https"])


def clean_join_url(value: str | None) -> str:
    """A virtual meeting link: https only, no spaces or control characters, no
    user name or password in it, at most 500 characters. Empty is allowed."""
    value = (value or "").strip()
    if not value:
        return ""
    if len(value) > JOIN_URL_MAX:
        raise ValidationError(f"The join link can be at most {JOIN_URL_MAX} characters.")
    if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValidationError("The join link cannot contain spaces.")
    try:
        _https_only(value)
    except ValidationError:
        raise ValidationError(
            "Enter the full meeting link starting with https:// (for example "
            "https://zoom.us/j/123456789)."
        ) from None
    if "@" in (urlparse(value).netloc or ""):
        raise ValidationError("The join link cannot include a user name or password.")
    return value


@dataclass(frozen=True)
class CalendarItem:
    uid: str
    title: str
    starts_at: datetime.datetime
    ends_at: datetime.datetime
    place: str
    join_url: str
    details: str
    page_url: str
    cancelled: bool = False

    @property
    def location(self) -> str:
        if self.place and self.join_url:
            return f"{self.place} · {self.join_url}"
        return self.place or self.join_url

    @property
    def description(self) -> str:
        lines = []
        if self.join_url:
            lines.append(f"Join online: {self.join_url}")
        if self.details:
            lines.append(self.details.strip())
        lines.append(f"Portal page (sign-in required): {self.page_url}")
        return "\n\n".join(lines)


def _host() -> str:
    return urlparse(settings.PORTAL_BASE_URL).hostname or "portal.invalid"


def _absolute(path: str) -> str:
    return settings.PORTAL_BASE_URL + path


def item_for_event(event) -> CalendarItem:
    from django.urls import reverse

    return CalendarItem(
        uid=f"event-{event.pk}@{_host()}",
        title=event.title,
        starts_at=event.starts_at,
        ends_at=event.ends_at or event.starts_at + DEFAULT_LENGTH,
        place=event.location,
        join_url=event.join_url,
        details=event.description,
        page_url=_absolute(reverse("event_detail", args=[event.pk])),
        cancelled=event.cancelled,
    )


def item_for_meeting(meeting) -> CalendarItem:
    """A meeting: its type only, never the agenda (executive sessions)."""
    from django.urls import reverse

    return CalendarItem(
        uid=f"meeting-{meeting.pk}@{_host()}",
        title=meeting.title,
        starts_at=meeting.starts_at,
        ends_at=meeting.ends_at or meeting.starts_at + DEFAULT_LENGTH,
        place=meeting.location,
        join_url=meeting.join_url,
        details=f"{meeting.get_kind_display()}. The agenda and minutes are on the portal page.",
        page_url=_absolute(reverse("meeting_detail", args=[meeting.pk])),
        cancelled=meeting.status == "cancelled",
    )


# --- iCalendar (RFC 5545) -------------------------------------------------------------


def _utc(value: datetime.datetime) -> str:
    return value.astimezone(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")


def escape_text(value: str) -> str:
    """TEXT value escaping (RFC 5545 3.3.11): backslash, semicolon, comma and
    newlines; other control characters are dropped."""
    value = (value or "").replace("\r\n", "\n").replace("\r", "\n")
    value = "".join(c for c in value if c == "\n" or c == "\t" or (ord(c) >= 32 and ord(c) != 127))
    return value.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def fold(line: str) -> str:
    """Fold a content line at 75 octets (RFC 5545 3.1), never inside a
    multi-byte character; continuation lines start with one space."""
    out, current, size = [], "", 0
    for char in line:
        width = len(char.encode("utf-8"))
        limit = 75 if not out else 74  # a continuation line's leading space counts
        if size + width > limit:
            out.append(current)
            current, size = "", 0
        current += char
        size += width
    out.append(current)
    return "\r\n ".join(out)


def ics(item: CalendarItem, *, now: datetime.datetime | None = None) -> str:
    stamp = _utc(now or timezone.now())
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:-//{escape_text(settings.SITE_NAME)}//Portal//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "BEGIN:VEVENT",
        f"UID:{escape_text(item.uid)}",
        f"DTSTAMP:{stamp}",
        f"DTSTART:{_utc(item.starts_at)}",
        f"DTEND:{_utc(item.ends_at)}",
        f"SUMMARY:{escape_text(item.title)}",
    ]
    if item.location:
        lines.append(f"LOCATION:{escape_text(item.location)}")
    lines += [
        f"DESCRIPTION:{escape_text(item.description)}",
        f"URL:{item.page_url}",
        # A cancellation replaces the earlier copy in people's calendars.
        f"SEQUENCE:{1 if item.cancelled else 0}",
        f"STATUS:{'CANCELLED' if item.cancelled else 'CONFIRMED'}",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    return "".join(fold(line) + "\r\n" for line in lines)


def ics_filename(item: CalendarItem) -> str:
    from django.utils.text import slugify

    return f"{slugify(item.title)[:60] or 'event'}.ics"


# --- Google Calendar and Outlook links -------------------------------------------------


def google_url(item: CalendarItem) -> str:
    query = urlencode(
        {
            "action": "TEMPLATE",
            "text": item.title,
            "dates": f"{_utc(item.starts_at)}/{_utc(item.ends_at)}",
            "details": item.description,
            "location": item.location,
        }
    )
    return f"https://calendar.google.com/calendar/render?{query}"


def _outlook(base: str, item: CalendarItem) -> str:
    def iso(value):
        return value.astimezone(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    query = urlencode(
        {
            "path": "/calendar/action/compose",
            "rru": "addevent",
            "subject": item.title,
            "startdt": iso(item.starts_at),
            "enddt": iso(item.ends_at),
            "location": item.location,
            "body": item.description,
        }
    )
    return f"{base}/calendar/0/deeplink/compose?{query}"


def outlook_com_url(item: CalendarItem) -> str:
    return _outlook("https://outlook.live.com", item)


def microsoft_365_url(item: CalendarItem) -> str:
    return _outlook("https://outlook.office.com", item)


def menu(item: CalendarItem, ics_url: str) -> dict:
    """The "Add to calendar" menu: online calendars (not for a cancelled
    item: there is nothing to add) and the .ics download, which also takes a
    cancellation into calendars that imported it."""
    online = []
    if not item.cancelled:
        online = [
            {"label": "Google Calendar", "url": google_url(item)},
            {"label": "Outlook.com", "url": outlook_com_url(item)},
            {"label": "Microsoft 365", "url": microsoft_365_url(item)},
        ]
    files = [
        {"label": "Apple Calendar (iPhone, Mac)", "url": ics_url},
        {"label": "Outlook (desktop)", "url": ics_url},
        {"label": "Other (.ics file)", "url": ics_url},
    ]
    return {"online": online, "files": files, "note": NOTE if online else ""}


def ics_response(item: CalendarItem):
    from django.http import HttpResponse

    response = HttpResponse(ics(item), content_type="text/calendar; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{ics_filename(item)}"'
    response["Cache-Control"] = "private, no-store"
    return response
