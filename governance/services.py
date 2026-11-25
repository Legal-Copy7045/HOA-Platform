from __future__ import annotations

import datetime

from django.conf import settings
from django.core.mail import send_mail
from django.db import IntegrityError, transaction
from django.db.models import Count
from django.urls import reverse
from django.utils import timezone

from audit.services import record_event
from community.models import ContactProfile, Lot, Ownership, Person
from community.rules import rule, rule_with_citation
from core.policy import SYSTEM, can, require
from core.scoping import system_access
from core.workflow import Transition, Workflow
from governance.models import (
    Attendance,
    Ballot,
    Meeting,
    MeetingKind,
    MeetingStatus,
    RecordsRequest,
    Resolution,
    Vote,
    VoteKind,
    VoteOption,
    VoteStatus,
)


class GovernanceError(ValueError):
    pass


# --- Meetings ----------------------------------------------------------------------


def create_meeting(*, title, kind, starts_at, location="", agenda="", actor, request=None):
    require(actor, "meeting.manage")
    if kind not in MeetingKind.values or not title.strip():
        raise GovernanceError("A meeting needs a title and a type.")
    meeting = Meeting.objects.create(
        title=title.strip()[:200],
        kind=kind,
        starts_at=starts_at,
        location=location[:200],
        agenda=agenda,
        created_by=actor,
    )
    record_event(
        "meeting.created", actor=actor, request=request, target=meeting, metadata={"kind": kind}
    )
    return meeting


def notice_shortfall_days(meeting: Meeting, sent_at=None) -> int | None:
    """Days short of the required owner notice, or None if fine/unknown."""
    required = rule("meeting.owner_notice_days")
    if required is None or meeting.kind == MeetingKind.EXECUTIVE:
        return None
    sent_at = sent_at or timezone.now()
    given = (meeting.starts_at.date() - timezone.localtime(sent_at).date()).days
    return max(0, required - given) or None


def send_notice(meeting: Meeting, *, actor, request=None) -> dict:
    """Email owners who consented to electronic notice; list those who need paper."""
    require(actor, "meeting.manage")
    if meeting.kind == MeetingKind.EXECUTIVE:
        raise GovernanceError("Executive sessions are not noticed to owners.")
    if meeting.status != MeetingStatus.SCHEDULED:
        raise GovernanceError("Only scheduled meetings can be noticed.")
    link = settings.PORTAL_BASE_URL + reverse("meeting_detail", args=[meeting.pk])
    when = timezone.localtime(meeting.starts_at).strftime("%A %B %-d, %Y at %-I:%M %p")
    body = (
        f"Notice of {meeting.get_kind_display().lower()}: {meeting.title}\n\n"
        f"When: {when}\nWhere: {meeting.location or 'see the portal'}\n\n"
        f"Agenda and details (sign-in required):\n{link}\n\n-- \n{settings.SITE_NAME}"
    )
    emailed, paper = 0, []
    with system_access("meeting notice recipients"):
        owners = Person.objects.filter(
            pk__in=Ownership.objects.active().values("person_id")
        ).select_related("user")
        consent = set(
            ContactProfile.objects.filter(electronic_notice_consent=True).values_list(
                "person_id", flat=True
            )
        )
        for person in owners:
            if person.pk in consent and (person.email or person.user_id):
                send_mail(
                    f"[{settings.SITE_NAME}] Meeting notice: {meeting.title}",
                    body,
                    None,
                    [person.user.email if person.user_id else person.email],
                )
                emailed += 1
            else:
                paper.append(person)
    shortfall = notice_shortfall_days(meeting)
    Meeting.objects.filter(pk=meeting.pk).update(notice_sent_at=timezone.now())
    record_event(
        "meeting.noticed",
        actor=actor,
        request=request,
        target=meeting,
        metadata={"emailed": emailed, "paper": len(paper), "short_days": shortfall or 0},
    )
    return {"emailed": emailed, "paper": paper, "short_days": shortfall}


def set_meeting_status(meeting, status, *, actor, request=None):
    require(actor, "meeting.manage")
    if meeting.status != MeetingStatus.SCHEDULED or status not in (
        MeetingStatus.HELD,
        MeetingStatus.CANCELLED,
    ):
        raise GovernanceError("Only a scheduled meeting can be marked held or cancelled.")
    Meeting.objects.filter(pk=meeting.pk).update(status=status)
    record_event(
        "meeting.status_changed",
        actor=actor,
        request=request,
        target=meeting,
        metadata={"status": status},
    )


def attach_minutes(meeting, document, *, actor, request=None):
    require(actor, "meeting.manage")
    with transaction.atomic():
        current = Meeting.objects.select_for_update().get(pk=meeting.pk)
        if current.minutes_document_id and current.minutes_document_id != document.pk:
            # Review D6: corrections are new versions of the same document, so
            # the record of which minutes were adopted never silently changes.
            raise GovernanceError(
                "Minutes are already attached. Publish a corrected version of that "
                "document instead."
            )
        Meeting.objects.filter(pk=meeting.pk).update(minutes_document=document)
    record_event(
        "meeting.minutes_attached",
        actor=actor,
        request=request,
        target=meeting,
        metadata={"document_id": document.pk},
    )


def record_attendance(meeting, person, *, present=True, by_proxy=False, actor, request=None):
    require(actor, "meeting.manage")
    Attendance.objects.update_or_create(
        meeting=meeting, person=person, defaults={"present": present, "by_proxy": by_proxy}
    )
    record_event(
        "meeting.attendance",
        actor=actor,
        request=request,
        target=meeting,
        metadata={"person_id": person.pk, "present": present},
    )


# --- Resolutions and policies --------------------------------------------------------


def next_resolution_number(year: int) -> str:
    count = Resolution.objects.filter(number__startswith=f"{year}-").count()
    return f"{year}-{count + 1:02d}"


def record_resolution(
    *,
    kind,
    title,
    text,
    adopted_on,
    votes_for=0,
    votes_against=0,
    abstentions=0,
    meeting=None,
    supersedes=None,
    citation="",
    actor,
    request=None,
) -> Resolution:
    require(actor, "resolution.record")
    if not title.strip() or not text.strip():
        raise GovernanceError("A resolution needs a title and its text.")
    if supersedes is not None and supersedes.superseded_by.exists():
        raise GovernanceError("That resolution has already been superseded.")
    with transaction.atomic():
        if supersedes is not None:
            # Lock the old resolution so two can't supersede it at once (D10);
            # the unique constraint is the backstop.
            Resolution.objects.select_for_update().filter(pk=supersedes.pk).first()
            if Resolution.objects.filter(supersedes=supersedes).exists():
                raise GovernanceError("That resolution has already been superseded.")
        resolution = Resolution.objects.create(
            number=next_resolution_number(adopted_on.year),
            kind=kind,
            title=title.strip()[:200],
            text=text.strip(),
            adopted_on=adopted_on,
            meeting=meeting,
            votes_for=votes_for,
            votes_against=votes_against,
            abstentions=abstentions,
            supersedes=supersedes,
            citation=citation[:200],
            recorded_by=actor,
        )
        record_event(
            "resolution.recorded",
            actor=actor,
            request=request,
            target=resolution,
            metadata={
                "number": resolution.number,
                "kind": kind,
                "supersedes": supersedes.number if supersedes else None,
            },
        )
    return resolution


def in_force(queryset=None):
    queryset = queryset if queryset is not None else Resolution.objects.all()
    return queryset.filter(superseded_by__isnull=True)


# --- Records requests -------------------------------------------------------------------


def _own_request(obj, actor):
    if actor is SYSTEM:
        return None
    person = Person.objects.unscoped("own request check").filter(user=actor).first()
    if person is None or person.pk != obj.requester_id:
        return "Only the person who made the request can withdraw it."
    return None


RECORDS = Workflow(
    "records_request",
    [
        Transition(
            "acknowledge", {"submitted"}, "in_progress", "records_request.manage", "Acknowledge"
        ),
        Transition(
            "fulfil",
            {"submitted", "in_progress"},
            "fulfilled",
            "records_request.manage",
            "Mark fulfilled",
            note_required=True,
        ),
        Transition(
            "deny",
            {"submitted", "in_progress"},
            "denied",
            "records_request.manage",
            "Deny (explain why)",
            note_required=True,
        ),
        Transition(
            "withdraw",
            {"submitted", "in_progress"},
            "withdrawn",
            "records_request.submit",
            "Withdraw",
            guard=_own_request,
        ),
    ],
)


def submit_records_request(*, description: str, actor, request=None) -> RecordsRequest:
    require(actor, "records_request.submit")
    if not description.strip():
        raise GovernanceError("Describe the records you would like to see.")
    with system_access("submit records request"):
        person = Person.objects.get(user=actor)
    days = rule("records.response_days")
    due = timezone.localdate() + datetime.timedelta(days=days) if days else None
    obj = RecordsRequest.objects.create(
        requester=person, description=description.strip(), due_on=due
    )
    record_event(
        "records_request.submitted",
        actor=actor,
        request=request,
        target=obj,
        metadata={"due_on": due.isoformat() if due else None},
    )
    return obj


# --- Votes ---------------------------------------------------------------------------------


def create_vote(
    *,
    title,
    kind,
    options: list[str],
    description="",
    secret=False,
    closes_at=None,
    actor,
    request=None,
) -> Vote:
    require(actor, "vote.manage")
    options = [o.strip() for o in options if o and o.strip()]
    if kind not in VoteKind.values or not title.strip():
        raise GovernanceError("A vote needs a title and a type.")
    if len(options) < 2 or len(set(options)) != len(options):
        raise GovernanceError("Give at least two different options.")
    with transaction.atomic():
        vote = Vote.objects.create(
            title=title.strip()[:200],
            kind=kind,
            description=description,
            secret=secret,
            closes_at=closes_at,
            created_by=actor,
        )
        for i, label in enumerate(options):
            VoteOption.objects.create(vote=vote, label=label[:200], position=i)
        record_event(
            "vote.created",
            actor=actor,
            request=request,
            target=vote,
            metadata={"kind": kind, "options": len(options)},
        )
    return vote


def open_vote(vote: Vote, *, actor, request=None) -> Vote:
    require(actor, "vote.manage")
    if vote.status != VoteStatus.DRAFT:
        raise GovernanceError("Only a draft vote can be opened.")
    fields = {"status": VoteStatus.OPEN, "opened_at": timezone.now()}
    if vote.kind == VoteKind.FORMAL:
        quorum, q_cite = rule_with_citation("vote.quorum_percent")
        passing, p_cite = rule_with_citation("vote.pass_percent")
        if quorum is None or passing is None:
            raise GovernanceError(
                "Set the quorum and pass rules (with citations) in "
                "Community settings before opening a formal vote."
            )
        with system_access("eligible lots"):
            eligible = Lot.objects.count()
        fields.update(
            quorum_percent=quorum,
            pass_percent=passing,
            eligible_lots=eligible,
            rules_citation=f"Quorum: {q_cite or 'no citation'}; pass: {p_cite or 'no citation'}",
        )
    Vote.objects.filter(pk=vote.pk).update(**fields)
    record_event(
        "vote.opened", actor=actor, request=request, target=vote, metadata={"kind": vote.kind}
    )
    vote.refresh_from_db()
    return vote


def close_vote(vote: Vote, *, actor, request=None) -> Vote:
    if actor is not SYSTEM:
        require(actor, "vote.manage")
    with transaction.atomic():
        # Lock the vote so no ballot can slip in while it closes (review D3).
        locked = Vote.objects.select_for_update().get(pk=vote.pk)
        if locked.status != VoteStatus.OPEN:
            raise GovernanceError("Only an open vote can be closed.")
        Vote.objects.filter(pk=vote.pk).update(status=VoteStatus.CLOSED, closed_at=timezone.now())
    record_event(
        "vote.closed", actor=None if actor is SYSTEM else actor, request=request, target=vote
    )
    vote.refresh_from_db()
    return vote


def ballot_choices_for(vote: Vote, user) -> dict:
    """What this user may still vote for: lots (formal) or themselves (informal)."""
    from core import policy

    if vote.status != VoteStatus.OPEN or not policy.can(user, "vote.cast"):
        return {"can_vote": False, "lots": []}
    if vote.closes_at and vote.closes_at <= timezone.now():
        return {"can_vote": False, "lots": []}
    roles = policy.roles_for(user)
    if vote.kind == VoteKind.INFORMAL:
        voted = Ballot.objects.filter(vote=vote, person_id=roles.person_id).exists()
        return {"can_vote": not voted, "lots": []}
    voted_lots = set(Ballot.objects.filter(vote=vote).values_list("lot_id", flat=True))
    with system_access("lots for ballot"):
        lots = list(Lot.objects.filter(pk__in=roles.owned_lot_ids - voted_lots))
    return {"can_vote": bool(lots), "lots": lots}


def cast_ballot(
    vote: Vote, *, option_id: int, lot_id: int | None = None, actor, request=None
) -> Ballot:
    require(actor, "vote.cast")
    from core import policy

    try:
        with transaction.atomic():
            # Re-read the vote under a row lock: a page loaded before the vote
            # closed must not be able to add a ballot afterwards (review D3).
            vote = Vote.objects.select_for_update().get(pk=vote.pk)
            choices = ballot_choices_for(vote, actor)
            if not choices["can_vote"]:
                raise GovernanceError(
                    "You cannot vote on this (already voted, closed, or not eligible)."
                )
            option = VoteOption.objects.filter(vote=vote, pk=option_id).first()
            if option is None:
                raise GovernanceError("Choose one of the options.")
            person_id = policy.roles_for(actor).person_id
            lot = None
            if vote.kind == VoteKind.FORMAL:
                lot = next((lot for lot in choices["lots"] if lot.pk == lot_id), None)
                if lot is None:
                    raise GovernanceError("Choose which of your lots this ballot is for.")
            ballot = Ballot.objects.create(
                vote=vote, option=option, person_id=person_id, lot=lot, cast_by=actor
            )
    except IntegrityError as exc:
        raise GovernanceError("A ballot was already cast for this lot.") from exc
    # Secret ballots: the audit trail records that a ballot was cast, not the choice.
    record_event(
        "vote.ballot_cast",
        actor=actor,
        request=request,
        target=vote,
        metadata={"lot_id": lot.pk if lot else None},
    )
    return ballot


def results(vote: Vote) -> dict:
    counts = dict(
        Ballot.objects.filter(vote=vote)
        .values_list("option")
        .annotate(n=Count("id"))
        .values_list("option", "n")
    )
    total = sum(counts.values())
    rows = [
        {
            "option": o,
            "count": counts.get(o.pk, 0),
            "percent": round(100 * counts.get(o.pk, 0) / total, 1) if total else 0,
        }
        for o in vote.options.all()
    ]
    outcome = None
    if vote.kind == VoteKind.FORMAL and vote.eligible_lots:
        turnout = round(100 * total / vote.eligible_lots, 1)
        # Exact integer comparisons, never rounded percentages (review D4).
        quorum_met = total * 100 >= (vote.quorum_percent or 0) * vote.eligible_lots
        top = max((r["count"] for r in rows), default=0)
        leaders = [r for r in rows if r["count"] == top and top > 0]
        # A tie has no leading option and passes nothing.
        leader = leaders[0] if len(leaders) == 1 else None
        passed = bool(
            quorum_met
            and leader
            and vote.pass_percent is not None
            and leader["count"] * 100 >= vote.pass_percent * total
        )
        outcome = {
            "turnout": turnout,
            "quorum_met": quorum_met,
            "leading": leader["option"].label if leader else ("tie" if len(leaders) > 1 else None),
            "meets_threshold": passed,
        }
    return {"rows": rows, "total": total, "outcome": outcome}


def can_see_voters(vote: Vote, user) -> bool:
    return not vote.secret and can(user, "vote.manage")
