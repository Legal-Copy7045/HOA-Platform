""" "Ask the board for a new setup link" from Can't sign in? (decision D62).

Someone whose setup link expired, or who never finished setting up, types
their email on a sign-in step. The page answers every address the same way
and does the same work for each (one lookup, one queued job carrying only a
login id, 0 when unknown). In the worker, and only when the address belongs
to a login that may be sent a new setup link (D59,
``accounts.services.setup_again_refusal``), an audit event
``auth.setup_link_requested`` is recorded (ids only). The board's My items
reads those events: "<person> asked for a new setup link", until a new link
is sent, the login no longer qualifies, or ``OPEN_DAYS`` pass. Nobody is
emailed: the person was already told on the page what happens next.
"""

from __future__ import annotations

import datetime

from django.utils import timezone

from accounts.models import Invitation, User
from audit.models import AuditEvent
from audit.services import record_event
from core.scoping import system_access

ACTION = "auth.setup_link_requested"
# A request nobody acted on leaves My items after this many days.
OPEN_DAYS = 14


def _cutoff() -> datetime.datetime:
    return timezone.now() - datetime.timedelta(days=OPEN_DAYS)


def _open_since(person) -> datetime.datetime | None:
    """When this person's newest unanswered request was made, or None."""
    event = (
        AuditEvent.objects.filter(
            action=ACTION, metadata__person_id=person.pk, occurred_at__gte=_cutoff()
        )
        .order_by("-occurred_at")
        .first()
    )
    if event is None:
        return None
    with system_access("setup link requests"):
        answered = Invitation.objects.filter(
            person=person, created_at__gte=event.occurred_at
        ).exists()
    return None if answered else event.occurred_at


def record_request(user_id: int) -> str:
    """The worker's half. Returns what happened (for tests and the job log
    only; never shown to the person who asked)."""
    from accounts.services import setup_again_refusal
    from community.models import Person

    if not user_id:
        return "unknown"
    with system_access("setup link request"):
        user = User.objects.filter(pk=user_id).first()
        person = Person.objects.filter(user_id=user_id).first() if user else None
    if person is None:
        return "unknown"
    if setup_again_refusal(person):
        return "not_eligible"
    if _open_since(person) is not None:
        return "already_open"
    record_event(
        ACTION,
        target=person,
        metadata={"person_id": person.pk, "user_id": user.pk},
    )
    return "recorded"


def open_requests() -> list[tuple]:
    """(person, asked_at) for every request still waiting for the board, newest
    first. Board only: the caller checks ``invite.send``."""
    from accounts.services import setup_again_refusal
    from community.models import Person

    person_ids = []
    for pid in (
        AuditEvent.objects.filter(action=ACTION, occurred_at__gte=_cutoff())
        .order_by("-occurred_at")
        .values_list("metadata__person_id", flat=True)
    ):
        if isinstance(pid, int) and pid not in person_ids:
            person_ids.append(pid)
    if not person_ids:
        return []
    with system_access("setup link requests"):
        people = {p.pk: p for p in Person.objects.filter(pk__in=person_ids)}
    out = []
    for pid in person_ids:
        person = people.get(pid)
        if person is None or setup_again_refusal(person):
            continue
        asked = _open_since(person)
        if asked is not None:
            out.append((person, asked))
    return out
