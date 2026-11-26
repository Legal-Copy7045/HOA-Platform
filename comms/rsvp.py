"""Event RSVPs and volunteer sign-ups on the community calendar.

The only supported way to change answers, slots and sign-ups. Every function
checks permission itself and locks what it changes.

* The board or manager turns RSVPs on for an event: an optional headcount
  cap (people going, guests included), guests each person may bring, a
  deadline (default: when the event starts) and an optional reminder email
  the day before for people going.
* Owners, authorized contacts and tenants who can see the event answer
  Going / Maybe / Not going, one answer per person, changeable until the
  deadline. Past the deadline nothing changes; at the cap "Going" is refused.
* Volunteer slots ("Bring a dessert") each have a capacity. Sign-ups lock
  the slot row, so concurrent sign-ups never overfill it.
* Other owners see counts only. Names, guests and the CSV export are for the
  board (``event.rsvp_names``). Former owners (read-only logins) cannot answer.
* RSVPs are not legal records: answers are changed in place, with history.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.db.models import Count, Q, Sum
from django.urls import reverse
from django.utils import timezone

from audit.services import record_event
from comms.models import Event, EventRsvp, RsvpAnswer, VolunteerSignup, VolunteerSlot
from core import policy
from core.policy import SYSTEM, require
from core.scoping import system_access
from core.text import single_line

MAX_GUESTS_LIMIT = 20
MAX_SLOTS_PER_EVENT = 50


class RsvpError(ValueError):
    """An answer or sign-up that breaks a rule. The message is safe to show."""


def _user(actor):
    return None if actor is SYSTEM else actor


def _refuse_read_only(actor) -> None:
    from community.services import _refuse_read_only

    _refuse_read_only(actor)


def _person_id(actor) -> int:
    person_id = policy.roles_for(actor).person_id
    if person_id is None:
        raise PermissionDenied("no_person")
    return person_id


def _visible_event(event: Event, actor) -> Event:
    """The event as this person may see it, locked for the change."""
    found = Event.objects.for_user(actor).select_for_update().filter(pk=event.pk).first()
    if found is None:
        raise PermissionDenied("event_not_visible")
    return found


def _check_open(event: Event) -> None:
    if event.cancelled:
        raise RsvpError("This event was cancelled.")
    if timezone.now() >= event.answers_close_at:
        when = timezone.localtime(event.answers_close_at)
        raise RsvpError(
            f"Answers closed on {when:%b} {when.day}, {when:%Y} at "
            f"{when.strftime('%I:%M %p').lstrip('0')}."
        )


# --- Setting up an event (board or manager) ----------------------------------------


def save_settings(
    event: Event,
    *,
    enabled: bool,
    cap: int | None = None,
    max_guests: int = 0,
    deadline=None,
    remind: bool = False,
    actor,
    request=None,
) -> Event:
    require(actor, "event.rsvp_manage")
    if cap is not None and not 1 <= cap <= 10_000:
        raise RsvpError("The headcount limit must be between 1 and 10,000, or left empty.")
    if not 0 <= (max_guests or 0) <= MAX_GUESTS_LIMIT:
        raise RsvpError(f"Guests each person may bring: 0 to {MAX_GUESTS_LIMIT}.")
    with transaction.atomic():
        event = Event.objects.select_for_update().get(pk=event.pk)
        if event.cancelled:
            raise RsvpError("This event was cancelled.")
        if deadline is not None and deadline > event.starts_at:
            raise RsvpError("The RSVP deadline cannot be after the event starts.")
        if remind and not event.rsvp_remind:
            event.rsvp_reminded_at = None
        event.rsvp_enabled = bool(enabled)
        event.rsvp_cap = cap
        event.rsvp_max_guests = max_guests or 0
        event.rsvp_deadline = deadline
        event.rsvp_remind = bool(remind)
        event.save(
            update_fields=[
                "rsvp_enabled",
                "rsvp_cap",
                "rsvp_max_guests",
                "rsvp_deadline",
                "rsvp_remind",
                "rsvp_reminded_at",
            ]
        )
        record_event(
            "event.rsvp_settings_saved",
            actor=_user(actor),
            request=request,
            target=event,
            metadata={
                "enabled": event.rsvp_enabled,
                "cap": event.rsvp_cap,
                "max_guests": event.rsvp_max_guests,
                "remind": event.rsvp_remind,
            },
        )
    return event


def add_slot(event: Event, *, title: str, capacity: int, actor, request=None) -> VolunteerSlot:
    require(actor, "event.rsvp_manage")
    title = single_line(title)[:200]
    if not title:
        raise RsvpError("Say what the volunteers will do.")
    if not 1 <= int(capacity or 0) <= 500:
        raise RsvpError("Each slot needs room for 1 to 500 people.")
    with transaction.atomic():
        event = Event.objects.select_for_update().get(pk=event.pk)
        if event.cancelled:
            raise RsvpError("This event was cancelled.")
        if VolunteerSlot.objects.filter(event=event, removed=False).count() >= MAX_SLOTS_PER_EVENT:
            raise RsvpError(f"{MAX_SLOTS_PER_EVENT} slots is the most for one event.")
        slot = VolunteerSlot.objects.create(
            event=event, title=title, capacity=int(capacity), created_by=_user(actor)
        )
        record_event(
            "event.slot_added",
            actor=_user(actor),
            request=request,
            target=slot,
            metadata={"event_id": event.pk, "capacity": slot.capacity},
        )
    return slot


def remove_slot(slot: VolunteerSlot, *, actor, request=None) -> VolunteerSlot:
    """Hide a slot. Its sign-ups stay in the database as a record."""
    require(actor, "event.rsvp_manage")
    with transaction.atomic():
        slot = VolunteerSlot.objects.select_for_update().get(pk=slot.pk)
        if slot.removed:
            raise RsvpError("That slot was already removed.")
        slot.removed = True
        slot.save(update_fields=["removed"])
        record_event(
            "event.slot_removed",
            actor=_user(actor),
            request=request,
            target=slot,
            metadata={"event_id": slot.event_id},
        )
    return slot


# --- Answering ---------------------------------------------------------------------


def _headcount(event: Event, *, excluding_person: int | None = None) -> int:
    with system_access("rsvp headcount"):
        rows = EventRsvp.objects.filter(event=event, answer=RsvpAnswer.GOING)
        if excluding_person is not None:
            rows = rows.exclude(person_id=excluding_person)
        going = rows.aggregate(n=Count("id"), g=Sum("guests"))
    return (going["n"] or 0) + (going["g"] or 0)


def answer(event: Event, *, answer: str, guests: int = 0, actor, request=None) -> EventRsvp:
    """Record or change this person's answer. Guests count only with Going."""
    _refuse_read_only(actor)
    require(actor, "event.rsvp")
    if answer not in RsvpAnswer.values:
        raise RsvpError("Choose Going, Maybe or Not going.")
    try:
        guests = int(guests or 0)
    except (TypeError, ValueError) as exc:
        raise RsvpError("Enter the number of guests as a whole number.") from exc
    if answer != RsvpAnswer.GOING:
        guests = 0
    person_id = _person_id(actor)
    with transaction.atomic():
        # Locking the event serializes answers, so the cap holds under load.
        event = _visible_event(event, actor)
        if not event.rsvp_enabled:
            raise RsvpError("This event is not taking RSVPs.")
        _check_open(event)
        if guests < 0:
            raise RsvpError("Guests cannot be negative.")
        if guests > event.rsvp_max_guests:
            if event.rsvp_max_guests == 0:
                raise RsvpError("This event is for residents only; guests are not included.")
            raise RsvpError(
                f"Each person may bring up to {event.rsvp_max_guests} "
                f"guest{'s' if event.rsvp_max_guests != 1 else ''}."
            )
        if answer == RsvpAnswer.GOING and event.rsvp_cap is not None:
            others = _headcount(event, excluding_person=person_id)
            if others + 1 + guests > event.rsvp_cap:
                room = max(event.rsvp_cap - others, 0)
                if room == 0:
                    raise RsvpError("This event is full. You can still answer Maybe.")
                raise RsvpError(
                    f"Only {room} place{'s' if room != 1 else ''} left, counting you and "
                    "your guests."
                )
        row = (
            EventRsvp.objects.unscoped("own rsvp").filter(event=event, person_id=person_id).first()
        )
        if row is None:
            row = EventRsvp(event=event, person_id=person_id)
        row.answer = answer
        row.guests = guests
        row.updated_at = timezone.now()
        row.save()
    return row


def own_answer(event: Event, user) -> EventRsvp | None:
    person_id = policy.roles_for(user).person_id
    if person_id is None:
        return None
    return EventRsvp.objects.for_user(user).filter(event=event, person_id=person_id).first()


# --- Volunteer slots ---------------------------------------------------------------


def sign_up(slot: VolunteerSlot, *, actor, request=None) -> VolunteerSignup:
    _refuse_read_only(actor)
    require(actor, "event.rsvp")
    person_id = _person_id(actor)
    with transaction.atomic():
        # Lock the slot first: two people taking its last place are serialized.
        slot = VolunteerSlot.objects.select_for_update().select_related("event").get(pk=slot.pk)
        event = _visible_event(slot.event, actor)
        if slot.removed:
            raise RsvpError("That slot was removed.")
        _check_open(event)
        with system_access("volunteer slot fill"):
            signups = VolunteerSignup.objects.filter(slot=slot)
            if signups.filter(person_id=person_id).exists():
                raise RsvpError("You are already signed up for this.")
            if signups.count() >= slot.capacity:
                raise RsvpError("That slot is full.")
        try:
            with transaction.atomic():
                return VolunteerSignup.objects.create(slot=slot, person_id=person_id)
        except IntegrityError as exc:
            raise RsvpError("You are already signed up for this.") from exc


def leave(slot: VolunteerSlot, *, actor, request=None) -> None:
    _refuse_read_only(actor)
    require(actor, "event.rsvp")
    person_id = _person_id(actor)
    with transaction.atomic():
        slot = VolunteerSlot.objects.select_for_update().select_related("event").get(pk=slot.pk)
        event = _visible_event(slot.event, actor)
        _check_open(event)
        mine = VolunteerSignup.objects.for_user(actor).filter(slot=slot, person_id=person_id)
        found = mine.first()
        if found is None:
            raise RsvpError("You are not signed up for this.")
        found.delete()


# --- What people see -----------------------------------------------------------------


@dataclass
class SlotView:
    slot: VolunteerSlot
    filled: int
    mine: bool
    names: list[str] | None = None

    @property
    def full(self) -> bool:
        return self.filled >= self.slot.capacity


def counts(event: Event) -> dict:
    """Counts only (no names): safe for every viewer of the event."""
    with system_access("rsvp counts"):
        rows = EventRsvp.objects.filter(event=event).aggregate(
            going=Count("id", filter=Q(answer=RsvpAnswer.GOING)),
            guests=Sum("guests", filter=Q(answer=RsvpAnswer.GOING)),
            maybe=Count("id", filter=Q(answer=RsvpAnswer.MAYBE)),
            not_going=Count("id", filter=Q(answer=RsvpAnswer.NOT_GOING)),
        )
    rows["guests"] = rows["guests"] or 0
    rows["headcount"] = rows["going"] + rows["guests"]
    if event.rsvp_cap is not None:
        rows["places_left"] = max(event.rsvp_cap - rows["headcount"], 0)
    return rows


def slots_for(event: Event, user) -> list[SlotView]:
    person_id = policy.roles_for(user).person_id
    names_ok = policy.can(user, "event.rsvp_names")
    slots = list(VolunteerSlot.objects.filter(event=event, removed=False))
    with system_access("volunteer slot fill"):
        signups = list(
            VolunteerSignup.objects.filter(slot__in=slots).select_related("person").order_by("id")
        )
    out = []
    for slot in slots:
        mine = [s for s in signups if s.slot_id == slot.pk]
        out.append(
            SlotView(
                slot=slot,
                filled=len(mine),
                mine=any(s.person_id == person_id for s in mine),
                names=[s.person.display_name for s in mine] if names_ok else None,
            )
        )
    return out


def answers_with_names(event: Event, user) -> list[EventRsvp]:
    """Every answer with the person's name: the board only."""
    require(user, "event.rsvp_names")
    with system_access("rsvp names for the board"):
        return list(
            EventRsvp.objects.filter(event=event)
            .select_related("person")
            .order_by("answer", "person__display_name")
        )


def export_rows(event: Event, user, request=None) -> tuple[list[str], list[list]]:
    """CSV rows for the board (names, answers, guests, volunteer slots);
    recorded in the audit log by the caller's CSV response."""
    require(user, "event.rsvp_names")
    header = ["Kind", "Name", "Answer or slot", "Guests", "Updated"]
    rows = []
    for r in answers_with_names(event, user):
        rows.append(
            [
                "RSVP",
                r.person.display_name,
                r.get_answer_display(),
                r.guests,
                timezone.localtime(r.updated_at).strftime("%Y-%m-%d %H:%M"),
            ]
        )
    for view in slots_for(event, user):
        for name in view.names or []:
            rows.append(["Volunteer", name, view.slot.title, "", ""])
    return header, rows


def going_event_ids(user) -> set[int]:
    person_id = policy.roles_for(user).person_id
    if person_id is None:
        return set()
    return set(
        EventRsvp.objects.for_user(user)
        .filter(person_id=person_id, answer=RsvpAnswer.GOING)
        .values_list("event_id", flat=True)
    )


def my_upcoming(user, limit: int = 4) -> list[Event]:
    """Coming events this person said Going to, or volunteers at ("Events
    I'm going to" on the dashboard)."""
    if not policy.can(user, "event.rsvp"):
        return []
    person_id = policy.roles_for(user).person_id
    if person_id is None:
        return []
    ids = going_event_ids(user)
    ids |= set(
        VolunteerSignup.objects.for_user(user)
        .filter(person_id=person_id, slot__removed=False)
        .values_list("slot__event_id", flat=True)
    )
    return list(
        Event.objects.for_user(user).filter(
            pk__in=ids, cancelled=False, starts_at__gte=timezone.now()
        )[:limit]
    )


# --- Reminder the day before -------------------------------------------------------


def send_reminders(now=None) -> int:
    """Email people going to events that start within a day, once per event,
    through comms.notify.send_update (weekly-digest choices honoured). The
    email holds the event title and a link only."""
    from comms.notify import send_update

    now = now or timezone.now()
    sent = 0
    due = Event.objects.filter(
        rsvp_enabled=True,
        rsvp_remind=True,
        rsvp_reminded_at__isnull=True,
        cancelled=False,
        starts_at__gt=now,
        starts_at__lte=now + timezone.timedelta(days=1),
    )
    for event in due:
        with transaction.atomic():
            event = Event.objects.select_for_update().get(pk=event.pk)
            if event.rsvp_reminded_at is not None:
                continue
            emails = _going_emails(event)
            link = settings.PORTAL_BASE_URL + reverse("event_detail", args=[event.pk])
            when = timezone.localtime(event.starts_at)
            title = single_line(event.title)
            body = (
                f"A reminder: you said you are going to {title} on "
                f"{when:%A, %B} {when.day} at {when.strftime('%I:%M %p').lstrip('0')}.\n\n"
                f"Details, and to change your answer (sign-in required):\n{link}\n\n"
                f"-- \n{settings.SITE_NAME}"
            )
            send_update(
                emails,
                subject=f"[{settings.SITE_NAME}] Reminder: {title}",
                body=body,
                link=link,
            )
            event.rsvp_reminded_at = now
            event.save(update_fields=["rsvp_reminded_at"])
            record_event("event.reminders_sent", target=event, metadata={"recipients": len(emails)})
            sent += len(emails)
    return sent


def _going_emails(event: Event) -> list[str]:
    from accounts.models import User

    with system_access("event reminder recipients"):
        person_ids = EventRsvp.objects.filter(event=event, answer=RsvpAnswer.GOING).values_list(
            "person_id", flat=True
        )
        users = User.objects.filter(person__in=list(person_ids), is_active=True)
        # Only people who can still see the event and still may answer.
        return sorted(
            u.email
            for u in users
            if policy.can(u, "event.rsvp")
            and Event.objects.for_user(u).filter(pk=event.pk).exists()
        )
