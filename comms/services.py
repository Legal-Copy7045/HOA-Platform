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
    event = Event.objects.create(
        title=single_line(title)[:200],
        starts_at=starts_at,
        ends_at=ends_at,
        location=location.strip()[:200],
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


def upcoming(user):
    return Event.objects.for_user(user).filter(
        starts_at__gte=timezone.now() - timezone.timedelta(hours=6)
    )
