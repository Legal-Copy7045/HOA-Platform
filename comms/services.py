from __future__ import annotations

from django.db import IntegrityError, transaction
from django.utils import timezone

from audit.services import record_event
from comms.models import Announcement, AnnouncementRead, Audience, Event
from core.policy import require
from core.text import single_line


class CommsError(ValueError):
    pass


def publish_announcement(
    *,
    title: str,
    body: str,
    audience: str = Audience.MEMBERS,
    urgent: bool = False,
    actor,
    request=None,
) -> Announcement:
    require(actor, "announcement.publish")
    if not single_line(title) or not body.strip():
        raise CommsError("An announcement needs a title and a message.")
    if audience not in (Audience.MEMBERS, Audience.BOARD):
        raise CommsError("Unknown audience.")
    with transaction.atomic():
        announcement = Announcement.objects.create(
            title=single_line(title)[:200],
            body=body.strip(),
            audience=audience,
            urgent=urgent,
            published_by=actor,
        )
        record_event(
            "announcement.published",
            actor=actor,
            request=request,
            target=announcement,
            metadata={"audience": audience, "urgent": urgent},
        )
        from comms.tasks import email_announcement

        announcement_id = announcement.pk
        transaction.on_commit(lambda: email_announcement.defer(announcement_id=announcement_id))
    return announcement


def mark_read(announcement: Announcement, user) -> None:
    try:
        with transaction.atomic():
            AnnouncementRead.objects.create(announcement=announcement, user=user)
    except IntegrityError:
        pass


def recipients_for(audience: str) -> list[str]:
    """Emails of active logins who can see this audience."""
    from accounts.models import User
    from community.services import notice_emails_for_users
    from core import policy

    action = "announcement.view" if audience == Audience.MEMBERS else "document.view_board"
    users = [u for u in User.objects.filter(is_active=True) if policy.can(u, action)]
    out = [u.email for u in users]
    if audience == Audience.MEMBERS:
        # Extra addresses people asked to get notices at (their own choice).
        out += [e for e in notice_emails_for_users(users) if e not in out]
    return out


def create_event(
    *,
    title: str,
    starts_at,
    ends_at=None,
    location: str = "",
    join_url: str = "",
    description: str = "",
    audience: str = Audience.MEMBERS,
    committee=None,
    actor,
    request=None,
) -> Event:
    """Board/manager events; a committee's own event may also be created by
    that committee's members (checked through governance.services)."""
    if committee is not None:
        from governance.services import require_committee_member

        require_committee_member(actor, committee)
        audience = Audience.COMMITTEE
    else:
        require(actor, "calendar.manage")
        if audience not in (Audience.MEMBERS, Audience.BOARD):
            raise CommsError("Unknown audience.")
    if not single_line(title):
        raise CommsError("An event needs a title.")
    if ends_at and ends_at < starts_at:
        raise CommsError("An event cannot end before it starts.")
    from django.core.exceptions import ValidationError

    from comms.calendar_links import clean_join_url

    try:
        join_url = clean_join_url(join_url)
    except ValidationError as exc:
        raise CommsError(" ".join(exc.messages)) from exc
    event = Event.objects.create(
        title=single_line(title)[:200],
        starts_at=starts_at,
        ends_at=ends_at,
        location=location.strip()[:200],
        join_url=join_url,
        description=description,
        audience=audience,
        committee=committee,
        created_by=actor,
    )
    record_event(
        "event.created", actor=actor, request=request, target=event, metadata={"audience": audience}
    )
    return event


def cancel_event(event: Event, *, actor, request=None) -> Event:
    require(actor, "calendar.manage")
    if event.cancelled:
        raise CommsError("Already cancelled.")
    event.cancelled = True
    event.save(update_fields=["cancelled"])
    record_event("event.cancelled", actor=actor, request=request, target=event)
    return event


def with_page_urls(events, user) -> list[Event]:
    """Give each event its page as ``page_url``: the event page, or the
    meeting page for an event a meeting made when this person may open that
    meeting (D49: clicking an event opens its page)."""
    from django.urls import reverse

    from governance.services import meetings_for_events

    events = list(events)
    meetings = meetings_for_events([e.pk for e in events], user)
    for e in events:
        m = meetings.get(e.pk)
        e.page_url = (
            reverse("meeting_detail", args=[m.pk]) if m else reverse("event_detail", args=[e.pk])
        )
    return events


def upcoming(user):
    return Event.objects.for_user(user).filter(
        starts_at__gte=timezone.now() - timezone.timedelta(hours=6)
    )


# --- FAQ ----------------------------------------------------------------------------


def save_faq(
    *,
    question: str,
    answer: str,
    category: str = "",
    sort_order: int = 100,
    published: bool = True,
    entry=None,
    actor,
    request=None,
):
    from comms.models import FaqEntry

    require(actor, "faq.manage")
    question = single_line(question)[:300]
    answer = (answer or "").strip()[:10_000]
    if not question or not answer:
        raise CommsError("A question and an answer are needed.")
    fields = {
        "question": question,
        "answer": answer,
        "category": single_line(category)[:80],
        "sort_order": max(0, min(int(sort_order or 100), 999)),
        "published": bool(published),
        "updated_by": actor,
        "updated_at": timezone.now(),
    }
    if entry is None:
        entry = FaqEntry.objects.create(**fields)
    else:
        FaqEntry.objects.filter(pk=entry.pk).update(**fields)
        entry.refresh_from_db()
    record_event(
        "faq.saved",
        actor=actor,
        request=request,
        target=entry,
        metadata={"published": entry.published},
    )
    return entry


# --- Paper mail (D55): what other apps may ask -----------------------------------------


def paper_notice_status(meeting, user) -> dict:
    """For the meeting page: how many homes need a paper notice and whether
    a paper notice mailing was prepared or mailed. Empty unless ``user`` may
    print paper mail (board and manager), and for executive sessions."""
    from core.policy import can

    if not can(user, "mail.print") or meeting.kind == "executive":
        return {}
    from comms import papermail

    groups = papermail.who_gets_paper()
    return {
        "can_print_mail": True,
        "paper_homes": len({g["lot"].pk for g in groups}),
        **papermail.meeting_status(meeting),
    }


def paper_copy_of_case_letter(letter, *, actor, request=None):
    """Collections hook (WT-006): prepare a one-home paper mailing from a
    collection case's letter; returns the mailing. Raises ValueError
    (PaperMailError) when it cannot be prepared."""
    from comms import papermail
    from comms.papermail import PaperMailError
    from core import conflicts
    from core.scoping import system_access

    with system_access("own matter: case of the letter"):
        case = letter.case
    # Own matters (D65): a letter about one's own home's arrears.
    conflicts.refuse(
        actor,
        case,
        area="collection",
        action="paper_copy",
        request=request,
        error=PaperMailError,
    )
    return papermail.prepare_case_copy(letter, actor=actor, request=request)


def paper_homes_count(user) -> int | None:
    """Homes that get paper now, for hooks on other pages (None when
    ``user`` may not print paper mail)."""
    from core.policy import can

    if not can(user, "mail.print"):
        return None
    from comms import papermail

    return len({g["lot"].pk for g in papermail.who_gets_paper()})
