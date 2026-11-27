"""Background jobs that failed, keep retrying, hang or wait too long (D74).

Read from Procrastinate's own tables (``procrastinate_jobs`` and
``procrastinate_events``). Only the task's name is ever read or shown,
never its arguments, which can hold ids or tokens. Each problem is said in
plain words first (what the job does, what it means, what to do), like
``core/error_words.py``, with the task name underneath for the maintainer.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from django.db import connection
from django.utils import timezone

FAILED_HOURS = 24
RETRY_LIMIT = 3  # more attempts than this is "retried repeatedly"
STUCK_MINUTES = 45  # a job "doing" longer than this has hung
BACKLOG_MINUTES = 20  # the oldest job due this long ago has not started
# System status "Clear" (core.health.clear): failures before this are known.
CLEARED_BEAT = "jobs_cleared"

# Task name (last part) -> what it does, in words people know.
WORDS = {
    "worker_heartbeat": "The background worker's pulse",
    "health_alerts": "The health checks and alert emails",
    "purge_server_errors": "Removing old server error counts",
    "purge_expired_ips": "Removing expired IP addresses from the audit log",
    "remove_old_jobs": "Removing old finished background jobs",
    "growth_snapshot": "The daily storage and database size record",
    "poll_bounces": "Reading bounced email from the bounce mailbox",
    "poll_open_checkouts": "Checking online payments with Stripe",
    "daily_sync": "The daily Stripe check (refunds, payouts, reconciliation)",
    "autopay": "Automatic payments",
    "bank_feed_sync": "The bank feed",
    "send_statement_emails": "Monthly statement emails",
    "monthly_assessments": "Posting the monthly assessment",
    "monthly_assessments_check": "The check that the monthly assessment was posted",
    "late_fees": "Late fees",
    "courtesy_reminders": "Courtesy payment reminders",
    "weekly_digest": "Weekly summary emails",
    "event_reminders": "Event reminder emails",
    "email_announcement": "Announcement emails",
    "notify_board": "Emails to the board",
    "send_invitation": "An invitation email",
    "send_recovery_link": "An account recovery email",
    "record_setup_link_request": "A request for a new setup link",
    "send_push": "Phone and browser notifications",
    "send_urgent_sms": "Urgent text messages",
    "scan_file": "Malware scanning of an upload",
    "scan_sweep": "Retrying malware scans of waiting files",
    "register_import_files": "Removing old register import files",
    "read_signed_form": "Reading a scanned acknowledgement form",
    "arc_deemed_approvals": "Architectural review deadlines",
    "compliance_reminders": "Recurring deadline reminders",
    "contact_retention": "Clearing contact details of people who left",
    "flush_secret_choices": "Recording secret ballots",  # nosec B105 (a task name)
    "sweep_secret_choices": "Recording secret ballots (hourly sweep)",  # nosec B105
    "close_due_votes": "Closing votes at their deadline",
    "vote_deadline_reminders": "Voting deadline reminders",
    "usps_tracking": "Reading USPS mail scans",
}


def short_name(task_name: str) -> str:
    return (task_name or "").rsplit(".", 1)[-1][:80]


def what(task_name: str) -> str:
    name = short_name(task_name)
    return WORDS.get(name, f"A background job ({name})")


@dataclass
class Problem:
    kind: str  # failed / retried / stuck / backlog
    task: str  # short task name
    count: int
    minutes: int = 0

    @property
    def plain(self) -> str:
        subject = what(self.task)
        n = self.count
        if self.kind == "failed":
            return (
                f"{subject} failed {n} time{'s' if n != 1 else ''} in the last day. "
                "Whatever it does did not happen; it runs again at its next time. "
                "If it keeps failing, tell the portal's maintainer (the web and worker logs "
                "have the error type)."
            )
        if self.kind == "retried":
            return (
                f"{subject} has been tried {n} times and has not succeeded yet. "
                "It will be tried again; if it keeps failing, tell the portal's maintainer."
            )
        if self.kind == "stuck":
            return (
                f"{subject} started {self.minutes} minutes ago and has not finished. "
                "Restarting the worker (or the server) frees it; if it happens again, tell "
                "the portal's maintainer."
            )
        return (
            f"{n} background job{'s' if n != 1 else ''} waiting, the oldest for "
            f"{self.minutes} minutes ({subject}). The worker is not keeping up or is not "
            "running: check Background worker above."
        )


def _cleared_at():
    from core.models import Heartbeat

    beat = Heartbeat.objects.filter(name=CLEARED_BEAT).first()
    return beat.at if beat else None


def problems(now=None) -> list[Problem]:
    now = now or timezone.now()
    since = now - datetime.timedelta(hours=FAILED_HOURS)
    cleared = _cleared_at()
    if cleared and cleared > since:
        since = cleared
    out: list[Problem] = []
    with connection.cursor() as cur:
        cur.execute(
            "SELECT j.task_name, count(DISTINCT j.id) FROM procrastinate_jobs j "
            "JOIN procrastinate_events e ON e.job_id = j.id "
            "WHERE j.status = 'failed' AND e.type = 'failed' AND e.at >= %s "
            "GROUP BY j.task_name ORDER BY count(DISTINCT j.id) DESC, j.task_name LIMIT 10",
            [since],
        )
        out += [Problem("failed", short_name(t), n) for t, n in cur.fetchall()]
        cur.execute(
            "SELECT task_name, max(attempts) FROM procrastinate_jobs "
            "WHERE status IN ('todo', 'doing') AND attempts > %s "
            "GROUP BY task_name ORDER BY max(attempts) DESC, task_name LIMIT 10",
            [RETRY_LIMIT],
        )
        out += [Problem("retried", short_name(t), n) for t, n in cur.fetchall()]
        cur.execute(
            "SELECT j.task_name, min(e.at) FROM procrastinate_jobs j "
            "JOIN procrastinate_events e ON e.job_id = j.id AND e.type = 'started' "
            "WHERE j.status = 'doing' GROUP BY j.id, j.task_name",
        )
        for task, started in cur.fetchall():
            if started is None:
                continue
            minutes = int((now - started).total_seconds() // 60)
            if minutes > STUCK_MINUTES:
                out.append(Problem("stuck", short_name(task), 1, minutes))
        cur.execute(
            "SELECT count(*), min(coalesce(j.scheduled_at, e.at)), "
            "(array_agg(j.task_name ORDER BY coalesce(j.scheduled_at, e.at)))[1] "
            "FROM procrastinate_jobs j "
            "LEFT JOIN procrastinate_events e ON e.job_id = j.id AND e.type = 'deferred' "
            "WHERE j.status = 'todo' AND (j.scheduled_at IS NULL OR j.scheduled_at <= %s)",
            [now],
        )
        waiting, oldest, task = cur.fetchone()
        if waiting and oldest is not None:
            minutes = int((now - oldest).total_seconds() // 60)
            if minutes > BACKLOG_MINUTES:
                out.append(Problem("backlog", short_name(task or ""), waiting, minutes))
    return out


def check():
    """For core.health: (ok, detail)."""
    found = problems()
    if not found:
        return True, "No failed, hanging or waiting jobs"
    first = found[0]
    more = len(found) - 1
    detail = first.plain
    if more:
        detail += f" (and {more} more problem{'s' if more != 1 else ''}: see System status)"
    return False, detail


def alert_lines(limit: int = 5) -> list[str]:
    """For the alert email: each problem in words, the task name under it."""
    lines = []
    try:
        found = problems()
    except Exception as exc:  # the alert itself must still go out
        return [f"  (job list unavailable: {type(exc).__name__})"]
    for p in found[:limit]:
        lines.append(f"  • {p.plain}")
        lines.append(f"      Technical: task {p.task}, {p.kind}")
    return lines


def clear(now=None) -> None:
    """Forget failures seen so far (System status "Clear"; the caller audits)."""
    from core.models import Heartbeat

    Heartbeat.objects.update_or_create(name=CLEARED_BEAT, defaults={"at": now or timezone.now()})
