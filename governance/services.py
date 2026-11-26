from __future__ import annotations

import datetime

from django.conf import settings
from django.core.mail import send_mail
from django.db import IntegrityError, transaction
from django.db.models import Count
from django.urls import reverse
from django.utils import timezone

from audit.services import record_event
from community import services as community_services
from community.models import ContactProfile, Lot, Ownership, Person
from community.rules import rule, rule_with_citation
from core.policy import SYSTEM, can, require
from core.policy import invalidate_roles as _invalidate_roles
from core.scoping import system_access
from core.text import single_line
from core.workflow import Transition, Workflow
from governance.models import (
    Attendance,
    Ballot,
    Meeting,
    MeetingKind,
    MeetingStatus,
    RecordsRequest,
    Resolution,
    SecretChoice,
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
    if kind not in MeetingKind.values or not single_line(title):
        raise GovernanceError("A meeting needs a title and a type.")
    with transaction.atomic():
        meeting = Meeting.objects.create(
            title=single_line(title)[:200],
            kind=kind,
            starts_at=starts_at,
            location=location[:200],
            agenda=agenda,
            created_by=actor,
        )
        record_event(
            "meeting.created", actor=actor, request=request, target=meeting, metadata={"kind": kind}
        )
        _add_to_calendar(meeting, actor=actor, request=request)
    return meeting


def _add_to_calendar(meeting: Meeting, *, actor, request=None) -> None:
    """Put the meeting on the community calendar. Executive sessions never go
    on it: owners must not learn of them from the calendar."""
    if meeting.kind == MeetingKind.EXECUTIVE:
        return
    from comms import services as comms_services

    event = comms_services.create_event(
        title=meeting.title,
        starts_at=meeting.starts_at,
        location=meeting.location,
        description=(
            f"{meeting.get_kind_display()}. The agenda and minutes are on the meeting page."
        ),
        actor=actor,
        request=request,
    )
    Meeting.objects.filter(pk=meeting.pk).update(calendar_event=event)
    meeting.calendar_event = event


def meeting_event_ids() -> set[int]:
    """Calendar events that belong to a meeting (the calendar sends people to
    the meeting page and leaves cancelling to it)."""
    return set(
        Meeting.objects.filter(calendar_event__isnull=False).values_list(
            "calendar_event_id", flat=True
        )
    )


def meeting_for_event(event, user) -> Meeting | None:
    """The meeting behind a calendar event, if this person may open it."""
    return Meeting.objects.for_user(user).filter(calendar_event=event).first()


def notice_shortfall_days(meeting: Meeting, sent_at=None) -> int | None:
    """Days short of the required owner notice, or None if fine/unknown."""
    required = rule("meeting.owner_notice_days")
    if required is None or meeting.kind == MeetingKind.EXECUTIVE:
        return None
    sent_at = sent_at or timezone.now()
    given = (timezone.localdate(meeting.starts_at) - timezone.localdate(sent_at)).days
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
                to = [person.user.email if person.user_id else person.email]
                to += [e for e in community_services.notice_emails(person) if e not in to]
                send_mail(
                    f"[{settings.SITE_NAME}] Meeting notice: {single_line(meeting.title)}",
                    body,
                    None,
                    to,
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


def notice_deadlines(meeting: Meeting) -> tuple[list, int | None]:
    """Recurring deadlines (Board tasks) that sending this meeting's notice can
    mark done: the active "Meetings and elections" items. For an annual
    meeting, the annual-meeting item is offered first."""
    if meeting.kind == MeetingKind.EXECUTIVE or meeting.status != MeetingStatus.SCHEDULED:
        return [], None
    from operations.models import ComplianceCategory, ComplianceItem

    items = list(ComplianceItem.objects.filter(active=True, category=ComplianceCategory.GOVERNANCE))
    default = None
    if meeting.kind == MeetingKind.ANNUAL:
        default = next((i.pk for i in items if "annual" in i.title.lower()), None)
    return items, default


def complete_notice_deadline(meeting: Meeting, item_id: int, *, actor, request=None):
    """Mark the chosen recurring deadline done once the notice went out
    (through the operations service, which checks the permission)."""
    from operations import services as operations_services
    from operations.models import ComplianceCategory, ComplianceItem

    require(actor, "meeting.manage")
    if meeting.kind == MeetingKind.EXECUTIVE:
        raise GovernanceError("Executive sessions are not noticed to owners.")
    item = ComplianceItem.objects.filter(
        active=True, category=ComplianceCategory.GOVERNANCE, pk=item_id
    ).first()
    if item is None:
        raise GovernanceError("That deadline is not open.")
    try:
        return operations_services.complete_compliance_item(item, actor=actor, request=request)
    except operations_services.OperationsError as exc:
        raise GovernanceError(str(exc)) from exc


def set_meeting_status(meeting, status, *, actor, request=None):
    require(actor, "meeting.manage")
    if meeting.status != MeetingStatus.SCHEDULED or status not in (
        MeetingStatus.HELD,
        MeetingStatus.CANCELLED,
    ):
        raise GovernanceError("Only a scheduled meeting can be marked held or cancelled.")
    with transaction.atomic():
        Meeting.objects.filter(pk=meeting.pk).update(status=status)
        record_event(
            "meeting.status_changed",
            actor=actor,
            request=request,
            target=meeting,
            metadata={"status": status},
        )
        if status == MeetingStatus.CANCELLED and meeting.calendar_event_id:
            from comms import services as comms_services
            from comms.models import Event

            event = Event.objects.get(pk=meeting.calendar_event_id)
            if not event.cancelled:
                comms_services.cancel_event(event, actor=actor, request=request)
    meeting.status = status


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


def upload_minutes(meeting, *, file, title="", actor, request=None):
    """Upload the minutes straight from the meeting page: a new document in
    Meeting minutes (through the documents service, with its file checks),
    attached to the meeting. Executive-session minutes are board only."""
    from documents import services as document_services
    from documents.models import Category, Visibility

    require(actor, "meeting.manage")
    require(actor, "document.manage")
    if Meeting.objects.filter(pk=meeting.pk, minutes_document__isnull=False).exists():
        raise GovernanceError(
            "Minutes are already attached. Publish a corrected version of that document instead."
        )
    when = timezone.localtime(meeting.starts_at).strftime("%B %-d, %Y")
    try:
        with transaction.atomic():
            document = document_services.create_document(
                title=single_line(title)[:200] or f"Minutes: {meeting.title} ({when})"[:200],
                category=Category.MINUTES,
                visibility=Visibility.BOARD
                if meeting.kind == MeetingKind.EXECUTIVE
                else Visibility.OWNERS,
                file=file,
                actor=actor,
                request=request,
            )
            attach_minutes(meeting, document, actor=actor, request=request)
    except document_services.DocumentError as exc:
        raise GovernanceError(str(exc)) from exc
    return document


def minutes_approval(meeting):
    """The board's approval of the meeting's minutes as they stand now (the
    attached document's current version), or None while they are a draft. A
    corrected version published after approval is a draft again."""
    from governance.models import MinutesApproval

    if not meeting.minutes_document_id:
        return None
    with system_access("minutes approval status"):
        from documents.models import Document

        current = (
            Document.objects.filter(pk=meeting.minutes_document_id)
            .values_list("current_version_id", flat=True)
            .first()
        )
    if current is None:
        return None
    return (
        MinutesApproval.objects.filter(
            meeting=meeting, document_id=meeting.minutes_document_id, version_id=current
        )
        .select_related("recorded_by")
        .first()
    )


def approve_minutes(meeting, *, approved_on=None, note="", version_id=None, actor, request=None):
    """Record that the board approved the meeting's attached minutes: the
    version the board member read (``version_id``, from the form), refused
    if another version has been published since (fixres BUG-006); internal
    callers may leave it out for the current version. A board decision: the
    manager may attach drafts but not approve them (SEC-003). Append-only
    and audited."""
    from governance.models import MinutesApproval

    require(actor, "minutes.adopt")
    approved_on = approved_on or timezone.localdate()
    with transaction.atomic():
        current = Meeting.objects.select_for_update().get(pk=meeting.pk)
        if not current.minutes_document_id:
            raise GovernanceError("Attach the minutes before marking them approved.")
        if current.status != MeetingStatus.HELD:
            raise GovernanceError("Only minutes of a held meeting can be approved.")
        if approved_on > timezone.localdate():
            raise GovernanceError("The approval date can't be in the future.")
        if approved_on < timezone.localdate(current.starts_at):
            raise GovernanceError("The approval date can't be before the meeting.")
        with system_access("approve minutes"):
            from documents.models import Document

            document = Document.objects.select_related("current_version").get(
                pk=current.minutes_document_id
            )
        version = document.current_version
        if version is None:
            raise GovernanceError("The minutes document has no file yet.")
        if version_id is not None and version.pk != version_id:
            raise GovernanceError(
                f"A new version of the minutes (version {version.number}) was published "
                "after you opened this page. Read it, then mark it approved if it is right."
            )
        if MinutesApproval.objects.filter(meeting=current, version=version).exists():
            raise GovernanceError("These minutes are already marked approved.")
        approval = MinutesApproval.objects.create(
            meeting=current,
            document=document,
            version=version,
            approved_on=approved_on,
            note=single_line(note)[:200],
            recorded_by=None if actor is SYSTEM else actor,
        )
    record_event(
        "meeting.minutes_approved",
        actor=None if actor is SYSTEM else actor,
        request=request,
        target=meeting,
        metadata={"document_id": document.pk, "version": version.number},
    )
    return approval


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
    if not single_line(title) or not text.strip():
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
            title=single_line(title)[:200],
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
    meeting=None,
    actor,
    request=None,
) -> Vote:
    require(actor, "vote.manage")
    options = [o.strip() for o in options if o and o.strip()]
    if kind == VoteKind.INFORMAL:
        # Owner decision 2026-11-26: informal polls are one-question surveys.
        raise GovernanceError("Informal polls are now surveys: create a survey instead.")
    if kind not in VoteKind.values or not single_line(title):
        raise GovernanceError("A vote needs a title and a type.")
    if meeting is not None and (
        meeting.kind not in (MeetingKind.ANNUAL, MeetingKind.SPECIAL)
        or meeting.status != MeetingStatus.SCHEDULED
    ):
        raise GovernanceError("A vote can belong only to a coming owners' meeting.")
    if len(options) < 2 or len(set(options)) != len(options):
        raise GovernanceError("Give at least two different options.")
    with transaction.atomic():
        vote = Vote.objects.create(
            title=single_line(title)[:200],
            kind=kind,
            description=description,
            secret=secret,
            closes_at=closes_at,
            meeting=meeting,
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
    if vote.kind == VoteKind.FORMAL:
        from governance import ballot_links

        ballot_links.issue(vote)  # personal, single-use, signed-in links (D24)
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


def my_ballots(vote: Vote, user) -> list[Ballot]:
    """Ballots already cast that this person may be told about: their own
    (informal), or their lots' (formal). Never the choice on a secret vote,
    which the ballot row does not hold."""
    from core import policy

    roles = policy.roles_for(user)
    if vote.kind == VoteKind.INFORMAL:
        if not roles.person_id:
            return []
        return list(Ballot.objects.filter(vote=vote, person_id=roles.person_id, lot__isnull=True))
    if not roles.owned_lot_ids:
        return []
    # Only ballots cast while this person has owned the lot: a buyer must not
    # see how the seller voted (review R2-001).
    since = dict(roles.owned_since)
    with system_access("my lots' ballots"):
        rows = (
            Ballot.objects.filter(vote=vote, lot_id__in=roles.owned_lot_ids)
            .select_related("lot", "option")
            .order_by("lot__number")
        )
        return [
            b
            for b in rows
            if b.lot_id in since and timezone.localdate(b.cast_at) >= since[b.lot_id]
        ]


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
                    raise GovernanceError("Choose which of your homes this ballot is for.")
            ballot = Ballot.objects.create(
                vote=vote,
                option=None if vote.secret else option,
                person_id=person_id,
                lot=lot,
                cast_by=actor,
            )
            if vote.secret:
                # Stored apart from the ballot, with nothing that links them.
                SecretChoice.objects.create(vote=vote, option=option)
    except IntegrityError as exc:
        raise GovernanceError("A ballot was already cast for this home.") from exc
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
    source = SecretChoice if vote.secret else Ballot
    counts = dict(
        source.objects.filter(vote=vote)
        .values_list("option")
        .annotate(n=Count("pk"))
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


# --- Committees (Milestone 3) -----------------------------------------------------------

from django.core.exceptions import PermissionDenied  # noqa: E402

from core import policy as _policy  # noqa: E402
from governance.models import (  # noqa: E402
    Committee,
    CommitteeMembership,
    CommitteeRole,
    CommitteeTopic,
)


def is_committee_member(user, committee) -> bool:
    if user is SYSTEM:
        return True
    return committee.pk in _policy.roles_for(user).committee_ids


def can_view_committee(user, committee) -> bool:
    return user is SYSTEM or can(user, "committee.view_all") or is_committee_member(user, committee)


def can_work_in_committee(user, committee) -> bool:
    """Post topics, add tasks and events: its members, and the board."""
    if user is SYSTEM:
        return True
    if not committee.active:
        return False
    return is_committee_member(user, committee) or can(user, "committee.manage")


def require_committee_member(user, committee) -> None:
    if not can_work_in_committee(user, committee):
        raise PermissionDenied("committee")


def committees_for(user):
    """Committees this person may open: all for the board, manager and
    auditor; their own for members."""
    if can(user, "committee.view_all"):
        return Committee.objects.all()
    return Committee.objects.filter(pk__in=_policy.roles_for(user).committee_ids)


def committees_public(*, actor) -> list[dict]:
    """Board and committees page: each active committee's name and purpose
    and its current members' names and roles (chair or member), nothing else,
    for everyone who sees the community pages."""
    require(actor, "board_committees.view")
    rows = []
    for committee in Committee.objects.filter(active=True):
        members = [
            {"name": m.person.display_name, "role": m.get_role_display()}
            for m in committee_members(committee)
        ]
        rows.append(
            {
                "committee": committee,
                "members": members,
                "can_open": can_view_committee(actor, committee),
            }
        )
    return rows


def create_committee(*, name: str, purpose: str = "", citation: str = "", actor, request=None):
    require(actor, "committee.manage")
    name = single_line(name)[:120]
    if not name:
        raise GovernanceError("A committee needs a name.")
    try:
        with transaction.atomic():
            committee = Committee.objects.create(
                name=name,
                purpose=purpose.strip(),
                citation=single_line(citation)[:200],
                created_by=actor,
            )
            from documents.services import committee_folder

            committee_folder(committee, actor=SYSTEM)
    except IntegrityError as exc:
        raise GovernanceError("A committee with that name already exists.") from exc
    record_event("committee.created", actor=actor, request=request, target=committee)
    return committee


def set_committee_active(committee, *, active: bool, actor, request=None):
    require(actor, "committee.manage")
    Committee.objects.filter(pk=committee.pk).update(active=active)
    committee.active = active
    record_event(
        "committee.retired" if not active else "committee.reactivated",
        actor=actor,
        request=request,
        target=committee,
    )
    return committee


def appoint_committee_member(
    committee, *, person, role: str = CommitteeRole.MEMBER, actor, request=None
):
    """The board appoints (no self-appointment). A committee has one chair:
    appointing a new chair makes the old one a member from today."""
    require(actor, "committee.manage")
    if role not in CommitteeRole.values:
        raise GovernanceError("Choose chair or member.")
    if not committee.active:
        raise GovernanceError("This committee is retired.")
    if actor is not SYSTEM and person.user_id == actor.pk:
        raise GovernanceError("Ask another board member to appoint you.")
    today_ = timezone.localdate()
    with transaction.atomic(), system_access("appoint committee member"):
        if not (
            Ownership.objects.active().filter(person=person).exists()
            or community_services.has_board_seat(person)
        ):
            raise GovernanceError("Committee members must be current owners or board members.")
        open_rows = CommitteeMembership.objects.filter(
            committee=committee, end_date__isnull=True
        ).select_for_update()
        if open_rows.filter(person=person).exists():
            raise GovernanceError("That person is already on this committee.")
        if role == CommitteeRole.CHAIR:
            for old in open_rows.filter(role=CommitteeRole.CHAIR):
                _end_membership(old, today_)
                CommitteeMembership.objects.create(
                    committee=committee,
                    person_id=old.person_id,
                    role=CommitteeRole.MEMBER,
                    start_date=today_,
                    appointed_by=None if actor is SYSTEM else actor,
                )
        membership = CommitteeMembership.objects.create(
            committee=committee,
            person=person,
            role=role,
            start_date=today_,
            appointed_by=None if actor is SYSTEM else actor,
        )
    record_event(
        "committee.member_appointed",
        actor=actor,
        request=request,
        target=committee,
        metadata={"person_id": person.pk, "role": role},
    )
    return membership


def _end_membership(membership, on) -> None:
    CommitteeMembership.objects.unscoped("end membership").filter(
        pk=membership.pk, end_date__isnull=True
    ).update(end_date=max(on, membership.start_date))
    _invalidate_roles()


def end_committee_membership(membership, *, actor, request=None):
    require(actor, "committee.manage")
    with system_access("end committee membership"):
        _end_membership(membership, timezone.localdate())
    record_event(
        "committee.member_ended",
        actor=actor,
        request=request,
        target=membership.committee,
        metadata={"person_id": membership.person_id, "role": membership.role},
    )


def committee_members(committee, on=None):
    """Current members (chair first), for the space and the directory."""
    with system_access("committee members"):
        return list(
            CommitteeMembership.objects.filter(committee=committee)
            .active(on)
            .select_related("person")
            .order_by("role", "person__display_name")
        )


def start_topic(committee, *, title: str, body: str, actor, request=None):
    require_committee_member(actor, committee)
    title = single_line(title)[:200]
    if not title or not body.strip():
        raise GovernanceError("A topic needs a title and a first message.")
    from comms import threads

    with transaction.atomic():
        topic = CommitteeTopic.objects.create(committee=committee, title=title, created_by=actor)
        record_event("committee.topic_started", actor=actor, request=request, target=committee)
        threads.post("committee_topic", topic, body=body, actor=actor, request=request)
    return topic


def add_committee_task(committee, *, title: str, due_date=None, assignee=None, actor, request=None):
    require_committee_member(actor, committee)
    from operations.services import create_committee_task

    return create_committee_task(
        committee_id=committee.pk,
        title=title,
        due_date=due_date,
        assignee=assignee,
        actor=actor,
        request=request,
    )


def close_committee_task(committee, task, *, status: str, actor, request=None):
    require_committee_member(actor, committee)
    from operations.services import close_committee_task as close

    return close(task, committee_id=committee.pk, status=status, actor=actor, request=request)


def committee_directory():
    """Active committees and their members' names, for the directory."""
    return [
        {
            "name": c.name,
            "purpose": c.purpose,
            "members": [
                {"name": m.person.display_name, "chair": m.role == CommitteeRole.CHAIR}
                for m in committee_members(c)
            ],
        }
        for c in Committee.objects.filter(active=True)
    ]
