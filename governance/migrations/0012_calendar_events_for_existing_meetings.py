"""Meetings scheduled before meetings went on the calendar automatically get
their calendar event now (future, non-executive, not cancelled). A hand-made
event with the same title and start time is linked instead of duplicated."""

from django.db import migrations
from django.utils import timezone

DESCRIPTION = "{kind}. The agenda and minutes are on the meeting page."
KINDS = {
    "board": "Board meeting",
    "annual": "Annual owners' meeting",
    "special": "Special owners' meeting",
    "committee": "Committee meeting",
}


def forwards(apps, schema_editor):
    Meeting = apps.get_model("governance", "Meeting")
    Event = apps.get_model("comms", "Event")
    meetings = Meeting.objects.filter(
        calendar_event__isnull=True, starts_at__gte=timezone.now(), kind__in=list(KINDS)
    ).exclude(status="cancelled")
    for meeting in meetings:
        event = Event.objects.filter(
            title=meeting.title[:200], starts_at=meeting.starts_at, cancelled=False
        ).first()
        if event is None:
            event = Event.objects.create(
                title=meeting.title[:200],
                starts_at=meeting.starts_at,
                location=meeting.location[:200],
                description=DESCRIPTION.format(kind=KINDS[meeting.kind]),
                audience="members",
                created_by_id=meeting.created_by_id,
            )
        Meeting.objects.filter(pk=meeting.pk).update(calendar_event=event)


class Migration(migrations.Migration):
    dependencies = [
        ("governance", "0011_meeting_calendar_event_vote_meeting"),
        ("comms", "0008_event_rsvps"),
    ]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
