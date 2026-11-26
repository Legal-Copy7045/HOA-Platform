"""Voting windows, automatic closing, reminders and live turnout (D61).

Every formal vote and every election has an opening and a closing time when
it opens (``check_window``); the closing time is at least the rule
``vote.min_open_days`` after the opening. A periodic task
(``governance.tasks.close_due_votes``) closes whatever has passed its
deadline through the normal close services as SYSTEM, which writes any
pooled secret choices first (D56) and so makes the results final. The board
may close early only when the rule ``vote.allow_early_close`` is on, with a
reason; it may extend a deadline once, before it passes, with a reason, and
never shorten it (a database trigger backs this up).

Turnout while voting is open: counts only. "N of M homes have voted" for
formal votes and elections, per eligible person for old informal polls and
surveys; the quorum needed and whether it is reached so far; for formal votes
and elections the list of homes that have voted (addresses, never choices:
participation is not secret, choices may be). Nobody sees running per-option
tallies while voting is open, for any kind; results appear once it closes.
The board and the manager see turnout (``vote.turnout``); owners see it only
when ``vote.show_turnout_to_owners`` is on.
"""

from __future__ import annotations

import csv
import datetime

from django.conf import settings
from django.db import transaction
from django.http import HttpResponse
from django.urls import reverse
from django.utils import timezone

from audit.services import record_event
from community.rules import rule
from core import policy
from core.lists import safe_cell
from core.policy import SYSTEM, can, require
from core.scoping import system_access
from core.text import single_line
from governance.models import (
    Ballot,
    Election,
    ElectionParticipation,
    ElectionState,
    Survey,
    SurveyParticipation,
    SurveyStatus,
    Vote,
    VoteKind,
    VoteStatus,
)

REMINDER_HOURS = 48
DEFAULT_MIN_DAYS = 7
REASON_MAX = 300


class WindowError(ValueError):
    pass


def min_open_days() -> int:
    value = rule("vote.min_open_days")
    return DEFAULT_MIN_DAYS if value is None else max(0, int(value))


def early_close_allowed() -> bool:
    return bool(rule("vote.allow_early_close"))


def owners_see_turnout() -> bool:
    return bool(rule("vote.show_turnout_to_owners"))


def when(value) -> str:
    """A deadline as people read it: "Dec 3, 2026 5:00 PM" (site time)."""
    if value is None:
        return ""
    local = timezone.localtime(value)
    return f"{local:%b} {local.day}, {local.year} {local:%-I:%M %p}"


# --- The window -----------------------------------------------------------------------


def check_window(opens_at, closes_at, *, now=None):
    """The effective opening time for a window that is about to open, or a
    WindowError saying what is wrong. Opening "now" is fine: an opening time
    already past becomes the moment voting opens, and the minimum period is
    counted from then."""
    now = now or timezone.now()
    if opens_at is None or closes_at is None:
        raise WindowError("Set when voting opens and when it closes.")
    if closes_at <= opens_at:
        raise WindowError("Voting must close after it opens.")
    start = max(opens_at, now)
    days = min_open_days()
    if closes_at - start < datetime.timedelta(days=days):
        raise WindowError(
            f"Voting must stay open at least {days} day{'s' if days != 1 else ''} "
            f"(rule: shortest voting period). Choose a later closing time."
        )
    return start


def window(target) -> tuple:
    """(opens_at, closes_at) of a vote, an election or a survey (a survey's
    dates run from the start of its first day to the end of its last)."""
    if isinstance(target, Vote):
        return target.opens_at, target.closes_at
    if isinstance(target, Election):
        return target.voting_opens_at, target.voting_closes_at
    if isinstance(target, Survey):
        tz = timezone.get_current_timezone()
        start = datetime.datetime.combine(target.opens_on, datetime.time.min, tzinfo=tz)
        end = datetime.datetime.combine(
            target.closes_on + datetime.timedelta(days=1), datetime.time.min, tzinfo=tz
        )
        return start, end
    raise TypeError(type(target))


def is_open(target) -> bool:
    if isinstance(target, Vote):
        return target.status == VoteStatus.OPEN
    if isinstance(target, Election):
        return target.state == ElectionState.VOTING
    return target.status == SurveyStatus.OPEN


def casting_open(target, now=None) -> bool:
    """Open, and inside its window (an old vote with no window: open)."""
    if not is_open(target):
        return False
    now = now or timezone.now()
    opens_at, closes_at = window(target)
    if opens_at and now < opens_at:
        return False
    return not (closes_at and now >= closes_at)


def past_deadline(target, now=None) -> bool:
    closes_at = window(target)[1]
    return bool(closes_at and (now or timezone.now()) >= closes_at)


def clean_reason(reason) -> str:
    return single_line(reason or "")[:REASON_MAX]


def require_early_close(target, reason, *, now=None) -> str:
    """Before the deadline, a formal vote or an election closes only if the
    rule allows it, and with a reason. Returns how it is closing: "board"
    (after the deadline, or a vote without one) or "early"."""
    if isinstance(target, Vote) and target.kind != VoteKind.FORMAL:
        return "board"  # old informal polls: not binding
    if window(target)[1] is None or past_deadline(target, now):
        return "board"
    if not early_close_allowed():
        raise WindowError(
            "Voting stays open until its closing time "
            f"({when(window(target)[1])}). The board can extend the deadline once, "
            "but not close early, unless Community settings allow it."
        )
    if not clean_reason(reason):
        raise WindowError("Say why voting is closing before its deadline.")
    return "early"


# --- Extending ------------------------------------------------------------------------


def extend(target, *, closes_at, reason: str, actor, request=None):
    """Move the deadline later, once, before it passes, with a reason; the
    people who can vote are emailed at once (never held for a digest). Never
    shorter."""
    require(actor, "vote.manage")
    reason = clean_reason(reason)
    if not reason:
        raise WindowError("Say why the deadline is being extended.")
    model = type(target)
    with transaction.atomic():
        locked = model.objects.select_for_update().get(pk=target.pk)
        if not is_open(locked):
            raise WindowError("Only open voting can be extended.")
        old = window(locked)[1]
        if old is None:
            raise WindowError("This vote has no closing time to extend.")
        if locked.extended_at is not None:
            raise WindowError("The deadline has already been extended once.")
        now = timezone.now()
        if now >= old:
            raise WindowError("The deadline has passed: voting is closing.")
        if closes_at is None or closes_at <= old:
            raise WindowError(
                f"Choose a time after the current deadline ({when(old)}). "
                "A deadline is never shortened."
            )
        field = "closes_at" if model is Vote else "voting_closes_at"
        model.objects.filter(pk=locked.pk).update(
            **{field: closes_at, "extended_at": now, "extension_reason": reason}
        )
    hours = int((closes_at - old).total_seconds() // 3600)
    record_event(
        "vote.deadline_extended",
        actor=actor,
        request=request,
        target=target,
        metadata={"kind": _kind(target), "hours_added": hours},
    )
    target.refresh_from_db()
    _notify_extension(target, old=old, reason=reason)
    return target


def _kind(target) -> str:
    if isinstance(target, Vote):
        return "vote"
    if isinstance(target, Election):
        return "election"
    return "survey"


def _detail_url(target) -> str:
    name = {"vote": "vote_detail", "election": "election_detail", "survey": "survey_detail"}
    return settings.PORTAL_BASE_URL + reverse(name[_kind(target)], args=[target.pk])


def _voter_emails() -> dict[int, str]:
    """Person id -> email of current owners with an active login."""
    from community.models import Ownership

    with system_access("voters to notify"):
        rows = Ownership.objects.active().select_related("person__user")
        out = {}
        for o in rows:
            user = o.person.user
            if user is not None and user.is_active and user.email:
                out[o.person_id] = user.email
    return out


def _notify_extension(target, *, old, reason) -> None:
    """Deadline-bound: sent at once to everyone, never held for a weekly
    digest. The email carries only the signed-in page's link."""
    from comms.notify import send_now

    link = _detail_url(target)
    new = window(target)[1]
    send_now(
        set(_voter_emails().values()),
        subject=f"[{settings.SITE_NAME}] Voting extended: {target.title}"[:200],
        body=(
            f"Voting on {target.title} now closes {when(new)} (it was {when(old)}).\n\n"
            f"Why: {reason}\n\n"
            "Ballots already cast stay as they are. If your home has not voted yet, you can "
            f"vote until the new deadline (sign-in required):\n{link}\n\n-- \n{settings.SITE_NAME}"
        ),
    )


# --- Automatic closing and reminders (periodic task) ----------------------------------


def close_due(now=None) -> int:
    """Close every vote, election and survey whose deadline has passed,
    through the normal close services as SYSTEM."""
    from governance import elections, services, surveys

    now = now or timezone.now()
    closed = 0
    for vote in Vote.objects.filter(status=VoteStatus.OPEN, closes_at__lte=now):
        try:
            services.close_vote(vote, actor=SYSTEM, at_deadline=True)
            closed += 1
        except services.GovernanceError:
            continue  # closed meanwhile
    for election in Election.objects.filter(state=ElectionState.VOTING, voting_closes_at__lte=now):
        try:
            elections.close_voting(election, actor=SYSTEM, at_deadline=True)
            closed += 1
        except elections.ElectionError:
            continue
    today = timezone.localdate(now)
    for survey in Survey.objects.filter(status=SurveyStatus.OPEN, closes_on__lt=today):
        try:
            surveys.close_survey(survey, actor=SYSTEM, at_deadline=True)
            closed += 1
        except surveys.SurveyError:
            continue
    return closed


def _not_voted_people(target) -> dict[int, str]:
    """Person id -> email of current owners (with a login) of homes that
    have not voted yet."""
    from community.models import Ownership

    if isinstance(target, Vote):
        voted = set(Ballot.objects.filter(vote=target).values_list("lot_id", flat=True))
    else:
        with system_access("voted homes"):
            voted = set(
                ElectionParticipation.objects.filter(election=target).values_list(
                    "lot_id", flat=True
                )
            )
    emails = _voter_emails()
    with system_access("homes not voted"):
        people = set(
            Ownership.objects.active().exclude(lot_id__in=voted).values_list("person_id", flat=True)
        )
    return {pid: emails[pid] for pid in sorted(people) if pid in emails}


def send_reminders(now=None) -> int:
    """Once per vote or election, about 48 hours before it closes: remind
    the owners of homes that have not voted, each with a personal link
    (D24), sent at once whatever their digest choice."""
    now = now or timezone.now()
    soon = now + datetime.timedelta(hours=REMINDER_HOURS)
    sent = 0
    targets = list(
        Vote.objects.filter(
            status=VoteStatus.OPEN,
            kind=VoteKind.FORMAL,
            reminder_sent_at__isnull=True,
            closes_at__gt=now,
            closes_at__lte=soon,
        )
    ) + list(
        Election.objects.filter(
            state=ElectionState.VOTING,
            reminder_sent_at__isnull=True,
            voting_closes_at__gt=now,
            voting_closes_at__lte=soon,
        )
    )
    for target in targets:
        if not casting_open(target, now):
            continue
        model = type(target)
        with transaction.atomic():
            claimed = model.objects.filter(pk=target.pk, reminder_sent_at__isnull=True).update(
                reminder_sent_at=now
            )
            if not claimed:
                continue  # another worker took it
            count = _remind(target)
        record_event(
            "vote.reminders_sent",
            actor=None,
            target=target,
            metadata={"kind": _kind(target), "count": count},
        )
        sent += count
    return sent


def _remind(target) -> int:
    """Deadline-bound: sent at once whatever the person's digest choice, so
    the personal single-use link (D24) only ever travels in this email and
    is created only for an email that is sent; nothing goes to a digest."""
    from comms.notify import send_now
    from governance import ballot_links

    people = _not_voted_people(target)
    if not people:
        return 0
    deadline = when(window(target)[1])
    for person_id, email in people.items():
        link = ballot_links.new_link(target, person_id)
        send_now(
            {email},
            subject=f"[{settings.SITE_NAME}] Vote by {deadline}: {target.title}"[:200],
            body=(
                f"Voting on {target.title} closes {deadline}, and your home has not voted "
                "yet.\n\nThis link is for you alone. Sign in to cast your ballot; you will be "
                "asked to confirm it is you before it is recorded. The link works once, so "
                f"please do not forward it.\n\n{link}\n\n"
                f"You can also vote from the portal menu until the deadline.\n\n-- \n"
                f"{settings.SITE_NAME}"
            ),
        )
    return len(people)


# --- Turnout --------------------------------------------------------------------------


def can_view_turnout(user) -> bool:
    return can(user, "vote.turnout")


def _quorum(eligible: int, voted: int, percent) -> dict:
    if percent is None or not eligible:
        return {"quorum_percent": percent, "quorum_needed": None, "quorum_met": None}
    needed = -(-eligible * int(percent) // 100)  # ceiling
    return {
        "quorum_percent": int(percent),
        "quorum_needed": needed,
        # Exact integer comparison, as in the results (review D4).
        "quorum_met": voted * 100 >= int(percent) * eligible,
    }


def turnout(target) -> dict:
    """Counts only, never choices: who may vote, how many have, and the
    quorum so far. ``unit`` is "homes" or "people"."""
    from governance import mixing

    pending = 0
    if isinstance(target, Vote):
        voted = Ballot.objects.filter(vote=target).count()
        if target.kind == VoteKind.FORMAL:
            unit = "homes"
            eligible = target.eligible_lots or _lot_count()
            quorum = target.quorum_percent
        else:
            unit = "people"
            eligible = _owner_people_count()
            quorum = None
        if target.secret:
            pending = mixing.pending_count(mixing.VOTE, target.pk)
    elif isinstance(target, Election):
        with system_access("election turnout"):
            voted = ElectionParticipation.objects.filter(election=target).count()
        unit = "homes"
        eligible = target.eligible_lots or _lot_count()
        quorum = rule("vote.quorum_percent")
        pending = mixing.pending_count(mixing.ELECTION, target.pk)
    else:
        from governance import surveys

        with system_access("survey turnout"):
            voted = SurveyParticipation.objects.filter(survey=target).count()
        unit = "people"
        eligible = len(surveys.audience_people(target))
        quorum = None
        if target.anonymous:
            pending = mixing.pending_count(mixing.SURVEY, target.pk)
    percent = round(100 * voted / eligible) if eligible else 0
    out = {
        "voted": voted,
        "eligible": eligible,
        "percent": min(percent, 100),
        "unit": unit,
        "pending": pending,
        "open": is_open(target),
    }
    out.update(_quorum(eligible, voted, quorum))
    return out


def turnout_line(t: dict) -> str:
    noun = "homes have" if t["unit"] == "homes" else "people have"
    if t["unit"] == "people" and t["eligible"] == 1:
        noun = "person has"
    elif t["unit"] == "homes" and t["eligible"] == 1:
        noun = "home has"
    verb = "voted" if t["unit"] == "homes" else "responded"
    return f"{t['voted']} of {t['eligible']} {noun} {verb} ({t['percent']}%)"


def _lot_count() -> int:
    from community.models import Lot

    with system_access("eligible homes"):
        return Lot.objects.count()


def _owner_people_count() -> int:
    from community.models import Ownership

    with system_access("eligible owners"):
        return Ownership.objects.active().values("person_id").distinct().count()


def who_has_voted(target) -> list[dict]:
    """Homes that have voted, in address order: the address (and "by proxy"
    for an election), never a choice. Formal votes and elections only."""
    from community.models import Lot

    if isinstance(target, Vote):
        if target.kind != VoteKind.FORMAL:
            return []
        lot_ids = set(
            Ballot.objects.filter(vote=target, lot__isnull=False).values_list("lot_id", flat=True)
        )
        proxy = set()
    elif isinstance(target, Election):
        with system_access("election participation"):
            rows = list(
                ElectionParticipation.objects.filter(election=target).values_list(
                    "lot_id", "by_proxy"
                )
            )
        lot_ids = {lot_id for lot_id, _ in rows}
        proxy = {lot_id for lot_id, by_proxy in rows if by_proxy}
    else:
        return []
    with system_access("homes that voted"):
        lots = sorted(Lot.objects.filter(pk__in=lot_ids), key=lambda lot: lot.sort_key)
    return [{"lot": lot, "by_proxy": lot.pk in proxy} for lot in lots]


def export_who_has_voted(target, *, actor, request=None) -> HttpResponse:
    """The homes that have voted as CSV (address first, D40), audited."""
    require(actor, "vote.turnout")
    rows = who_has_voted(target)
    record_event(
        "vote.turnout_exported",
        actor=actor,
        request=request,
        target=target,
        metadata={"kind": _kind(target), "rows": len(rows)},
    )
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    stamp = timezone.localdate().isoformat()
    response["Content-Disposition"] = (
        f'attachment; filename="who-has-voted-{_kind(target)}-{target.pk}-{stamp}.csv"'
    )
    writer = csv.writer(response)
    header = ["Address", "Voted"]
    if isinstance(target, Election):
        header.append("By proxy")
    writer.writerow(header)
    for row in rows:
        line = [safe_cell(row["lot"].label), "Yes"]
        if isinstance(target, Election):
            line.append("Yes" if row["by_proxy"] else "No")
        writer.writerow(line)
    return response


def page_context(target, user) -> dict:
    """What a vote, election or survey page shows about the window and the
    turnout to this person."""
    staff = can_view_turnout(user)
    opens_at, closes_at = window(target)
    open_now = is_open(target)
    show_turnout = open_now and (staff or (owners_see_turnout() and _is_voter(user)))
    t = turnout(target) if (show_turnout or (staff and not open_now)) else None
    return {
        "window_opens": opens_at,
        "window_closes": closes_at,
        "window_not_started": bool(open_now and opens_at and timezone.now() < opens_at),
        "turnout": t,
        "turnout_line": turnout_line(t) if t else "",
        "turnout_staff": staff,
        "who_voted": who_has_voted(target) if staff and t else [],
        "can_extend": bool(
            open_now
            and can(user, "vote.manage")
            and closes_at
            and getattr(target, "extended_at", None) is None
            and not past_deadline(target)
            and not isinstance(target, Survey)
        ),
        "early_close_allowed": early_close_allowed(),
        "past_deadline": past_deadline(target),
    }


def _is_voter(user) -> bool:
    return can(user, "vote.cast") or can(user, "survey.respond")


# --- My items for the board -----------------------------------------------------------


def closing_soon_items(user, now=None) -> list[dict]:
    """Votes and elections closing within 48 hours, with their turnout, for
    people who see turnout (My items: "Vote closes in 48 hours: 61% turnout,
    quorum met")."""
    if not can_view_turnout(user) or user is policy.SYSTEM:
        return []
    now = now or timezone.now()
    soon = now + datetime.timedelta(hours=REMINDER_HOURS)
    targets = list(
        Vote.objects.filter(
            status=VoteStatus.OPEN, kind=VoteKind.FORMAL, closes_at__gt=now, closes_at__lte=soon
        )
    ) + list(
        Election.objects.filter(
            state=ElectionState.VOTING, voting_closes_at__gt=now, voting_closes_at__lte=soon
        )
    )
    out = []
    for target in targets:
        t = turnout(target)
        hours = max(1, int((window(target)[1] - now).total_seconds() // 3600))
        label = "Election" if isinstance(target, Election) else "Vote"
        quorum = ""
        if t["quorum_met"] is not None:
            quorum = ", quorum met" if t["quorum_met"] else ", quorum not met yet"
        span = f"{hours} hour{'s' if hours != 1 else ''}"
        out.append(
            {
                "title": f"{label} closes in {span}: {t['percent']}% turnout{quorum}",
                "detail": f"{target.title} · closes {when(window(target)[1])}",
                "url": reverse(f"{_kind(target)}_detail", args=[target.pk]),
                "due": timezone.localdate(window(target)[1]),
                "quorum_met": t["quorum_met"],
            }
        )
    return out
