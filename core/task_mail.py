"""Emails about server tasks (decision D98).

Every server task (a backup, the restore drills, the off-site lock check,
host security, the health checks, a reseed, a restart, a restore) and every
portal update emails the person who asked and the sysadmins when it ends,
whether it succeeded or failed: the task's name, the result, the short
summary, how long it took and a link to its page. A restore also goes to the
board, before it starts and when it ends.

The emails carry no log lines (the tail stays on the page) and no addresses
or keys: the summary is the agent's one cleaned line, with anything shaped
like an email address or a long token removed again here. Sending goes
through the portal's normal mail path (``core.email``, the SMTP settings from
Keys and connections); a failure to send is logged by class name and never
fails the task or its audit record.
"""

from __future__ import annotations

import logging
import re

from django.conf import settings
from django.core.mail import send_mail
from django.urls import reverse

logger = logging.getLogger(__name__)

_EMAIL = re.compile(r"[^\s@]+@[^\s@]+")
_TOKEN = re.compile(r"[A-Za-z0-9_+/=-]{32,}")


def safe_summary(text: str) -> str:
    from core.host_tasks import clean

    text = clean(text, 480)
    text = _EMAIL.sub("[address]", text)
    return _TOKEN.sub("[hidden]", text)


def duration_text(start, end) -> str:
    if not start or not end or end < start:
        return "unknown"
    seconds = int((end - start).total_seconds())
    if seconds < 60:
        return f"{seconds} second{'s' if seconds != 1 else ''}"
    minutes, seconds = divmod(seconds, 60)
    return f"{minutes} min {seconds:02d} s"


def _sysadmins() -> list[str]:
    from core.health import sysadmin_emails

    return sysadmin_emails()


def _board() -> list[str]:
    from community.services import board_emails

    return board_emails()


def _active_email(user) -> str:
    if user is None or not getattr(user, "is_active", False):
        return ""
    return user.email or ""


def _send(recipients, subject: str, body: str) -> int:
    """One message per address; never raises."""
    from core.email import BOARD, mail_kind

    sent = 0
    with mail_kind(BOARD):
        for address in sorted({a for a in recipients if a}):
            try:
                sent += send_mail(subject, body, None, [address], fail_silently=False)
            except Exception as exc:  # the task's result stands either way
                logger.warning("task_mail.failed", extra={"error": type(exc).__name__})
    return sent


def _url(name: str) -> str:
    return settings.PORTAL_BASE_URL + reverse(name)


def task_finished(task) -> int:
    """Email the result of one finished server task. Returns how many went."""
    from core.host_tasks import BY_KIND
    from core.models import HostTask

    spec = BY_KIND.get(task.kind)
    label = spec.label if spec else "A server task"
    outcome = {
        HostTask.State.SUCCEEDED: "succeeded",
        HostTask.State.FAILED: "failed",
        HostTask.State.REFUSED: "was refused",
    }.get(task.state, task.state)
    restore = task.kind == HostTask.Kind.RESTORE_BACKUP
    page = "backup_restore" if restore else "server_tasks"
    lines = [
        f"{label} {outcome}.",
        "",
        f"Result: {safe_summary(task.summary) or '(no summary)'}",
        f"Took: {duration_text(task.started_at or task.requested_at, task.finished_at)}",
    ]
    if restore and task.backup:
        from core.backup_points import label as point_label

        lines.append(f"Backup point: {point_label(task.backup)}")
    if restore and task.state == HostTask.State.SUCCEEDED:
        lines += [
            "",
            "The portal now holds the data as it was at that backup. Everyone signs in "
            "again. Anything entered after the backup is in the safety backup named in "
            "the result.",
        ]
    lines += ["", f"Details: {_url(page)}", "", "-- ", settings.SITE_NAME]
    recipients = [_active_email(task.requested_by), *_sysadmins()]
    if restore:
        recipients += _board()
    subject = f"[{settings.SITE_NAME}] Server task {outcome}: {label}"
    return _send(recipients, subject, "\n".join(lines))


def restore_requested(task) -> int:
    """Before a restore starts: the board and the sysadmins are told who asked,
    which backup point, and what happens (D98)."""
    from core.backup_points import label as point_label

    who = _active_email(task.requested_by) or "a sysadmin"
    body = "\n".join(
        [
            f"{who} asked the server to restore the portal from a backup.",
            "",
            f"Backup point: {point_label(task.backup)}",
            "",
            "Within a minute the server takes a safety backup of the portal as it is now, "
            "stops it, replaces the database and files with that backup, and starts it "
            "again. The portal is unavailable for several minutes, and everyone signs in "
            "again afterwards. You get another email when it has finished.",
            "",
            f"Details: {_url('backup_restore')}",
            "",
            "-- ",
            settings.SITE_NAME,
        ]
    )
    recipients = [_active_email(task.requested_by), *_sysadmins(), *_board()]
    subject = f"[{settings.SITE_NAME}] Restore from a backup requested"
    return _send(recipients, subject, body)


def update_finished(status) -> int:
    """Email the result of an Install update (System status)."""
    ok = status.state == "succeeded"
    lines = [
        f"Installing the portal update {'succeeded' if ok else 'failed'}.",
        "",
        f"Result: {safe_summary(status.message) or '(no summary)'}",
        f"Took: {duration_text(status.run_started_at, status.state_changed_at)}",
        "",
        f"Details: {_url('system_status')}",
        "",
        "-- ",
        settings.SITE_NAME,
    ]
    recipients = [_active_email(status.requested_by), *_sysadmins()]
    subject = (
        f"[{settings.SITE_NAME}] Server task {'succeeded' if ok else 'failed'}: Install update"
    )
    return _send(recipients, subject, "\n".join(lines))
