"""Server tasks (Admin -> Server tasks, decision D36).

Reusable host operations (a backup, a restore drill, the off-site lock check,
re-applying host security, the health checks) are started in the portal and
run by the VM agent from a fixed allowlist. The web app never touches the
host: it only inserts a ``core.HostTask`` row naming one of the kinds below.
The agent (infra/poc/portal-agent.sh with infra/poc/host-tasks.sh) maps the
kind to a fixed command with a bash ``case``; nothing else from the database
reaches a command line, except the dataset of a demo reseed (D53), which the
agent accepts only as a plain name ([a-z_]) the portal's own seed_demo lists,
and the backup point of a real restore (D98), which the agent accepts only as
``local:<stamp>`` or ``offsite:<stamp>`` naming a backup that really exists.
Results come back as a state and a short summary; the requester and the
sysadmins are emailed each result (``core.task_mail``).
"""

from __future__ import annotations

import datetime
import logging
import re
from dataclasses import dataclass

from django.db import IntegrityError, transaction
from django.utils import timezone

from core import policy
from core.models import HostTask

logger = logging.getLogger(__name__)

Kind = HostTask.Kind
State = HostTask.State

# How long the agent may take to notice a request (it runs every minute).
AGENT_STALE = datetime.timedelta(minutes=5)

# Reseeding (D53): the word typed to confirm, and what the person is told.
RESEED_WORD = "RESEED"
RESEED_CONFIRM_TEXT = (
    "Everything in the portal is replaced by fictional data. A backup is taken first. "
    "Real logins (yours and other non-demo logins) are kept. The portal is unavailable "
    "for about a minute."
)


# Restarting the portal (D91): started only from its own confirmation page
# (System status -> Restart the portal), which sends this fixed value; at most
# once every RESTART_COOLDOWN, and like every task one at a time.
RESTART_CONFIRM_VALUE = "restart"
RESTART_COOLDOWN = datetime.timedelta(minutes=10)
RESTART_CONFIRM_TEXT = (
    "The portal's web server and background worker are stopped and started again "
    "(with both compose files, so the tunnel keeps reaching the site). Everyone is "
    "unavailable for up to a minute; anything being typed into a form at that moment "
    "may need to be sent again. The database, files, scanner and tunnel keep running."
)


# Restoring from a backup (D98): started only from Backups -> Restore from a
# backup, with a listed backup point, this word typed exactly, and a fresh
# passkey or authenticator check (core/fresh_mfa.py, never a password or an
# emailed code).
RESTORE_WORD = "RESTORE"
RESTORE_CONFIRM_TEXT = (
    "First a safety backup of the portal as it is now is taken (kept, not removed "
    "after 14 days). Then the portal stops, its database and files are replaced by "
    "the chosen backup, it is brought up to the installed release, and it starts "
    "again: several minutes without the portal. Everything entered after that backup "
    "(payments recorded, votes, documents, the audit log) is no longer in the portal; "
    "it stays in the safety backup. Logins, passkeys and authenticator apps are the "
    "ones in the backup, and everyone signs in again. Service keys on Keys and "
    "connections go back to what they were then. If anything fails, the server puts "
    "the safety backup back."
)


class HostTaskRefused(Exception):
    """A request the portal will not pass on (shown to the person as is)."""


@dataclass(frozen=True)
class TaskSpec:
    kind: str
    label: str
    description: str
    confirm: str
    offsite_only: bool = False
    # reseed_demo (D53): offered only where demo data is allowed, takes a
    # dataset from the registry and the typed confirmation word.
    demo_only: bool = False
    # Started from its own page (a URL name) after a confirmation step, never
    # with the plain Start button (restart_app, D91).
    own_page: str = ""


TASKS = [
    TaskSpec(
        Kind.BACKUP,
        "Take a backup",
        "An encrypted copy of the database and every uploaded file, kept on the server "
        "and copied off-site when that is set up.",
        "Take a backup now? It takes about a minute; the site stays up.",
    ),
    TaskSpec(
        Kind.RESTORE_DRILL,
        "Restore drill",
        "Loads the newest backup into a throwaway copy of the portal and checks it. "
        "The live portal is not touched.",
        "Run a restore drill now? It uses extra memory on the server for a few minutes; "
        "the site stays up.",
    ),
    TaskSpec(
        Kind.RESTORE_DRILL_OFFSITE,
        "Restore drill from the off-site copy",
        "Downloads the newest off-site backup, checks it was not altered, and restores it "
        "into a throwaway copy.",
        "Run a restore drill from the off-site copy now? It downloads the backup and uses "
        "extra memory for a few minutes; the site stays up.",
        offsite_only=True,
    ),
    TaskSpec(
        Kind.OFFSITE_LOCK_CHECK,
        "Check the off-site lock",
        "Proves the off-site bucket refuses to delete backups (a tiny probe file stays "
        "there until the lock expires).",
        "Check the off-site lock now? It uploads a tiny probe file that cannot be deleted "
        "for 90 days.",
        offsite_only=True,
    ),
    TaskSpec(
        Kind.HARDEN_HOST,
        "Re-apply host security",
        "Re-runs the host hardening (security updates, audit log, file integrity check, "
        "Falco, the outbound firewall for the containers). Safe to repeat.",
        "Re-apply host security now? It installs any missing pieces and restarts those "
        "services; takes a few minutes; the site stays up.",
    ),
    TaskSpec(
        Kind.HEALTH_CHECK,
        "Run the health checks",
        "Runs every health check now and emails the sysadmins about anything that changed.",
        "Run the health checks now? Sysadmins are emailed about any change.",
    ),
    TaskSpec(
        Kind.RESEED_DEMO,
        "Reseed with demo data",
        "Replaces everything in the portal with a fictional demo dataset, after taking a "
        "backup. Real logins are kept. Only on a demo or test portal; started from "
        "Settings -> Demo data.",
        RESEED_CONFIRM_TEXT,
        demo_only=True,
    ),
    TaskSpec(
        Kind.RESTART_APP,
        "Restart the portal",
        "Stops and starts the web server and the background worker, for example after a "
        "setting changed on the server. Started from System status, after a confirmation.",
        RESTART_CONFIRM_TEXT,
        own_page="system_restart",
    ),
    TaskSpec(
        Kind.RESTORE_BACKUP,
        "Restore from a backup",
        "Replaces the portal's database and files with a backup point you choose, after "
        "a safety backup of the current state. Started from Backups, after you type "
        f"{RESTORE_WORD} and confirm with your passkey or authenticator app.",
        RESTORE_CONFIRM_TEXT,
        own_page="backup_restore",
    ),
]
BY_KIND = {spec.kind: spec for spec in TASKS}


def offered() -> list[TaskSpec]:
    """The tasks this server offers (off-site ones only once it is set up,
    the demo reseed only where demo data is allowed)."""
    from core import demo_reseed, offsite

    has_offsite = offsite.configured()
    demo = demo_reseed.allowed()
    return [
        spec
        for spec in TASKS
        if (has_offsite or not spec.offsite_only) and (demo or not spec.demo_only)
    ]


def current() -> HostTask | None:
    return HostTask.objects.filter(state__in=HostTask.ACTIVE).order_by("requested_at").first()


def agent_seen():
    """When the VM agent last ran, or None (not installed / no row yet)."""
    from core.models import UpdateStatus

    return UpdateStatus.objects.filter(pk=1).values_list("agent_seen_at", flat=True).first()


def agent_ok(now=None) -> bool:
    seen = agent_seen()
    return bool(seen and seen > (now or timezone.now()) - AGENT_STALE)


def request_task(
    actor,
    kind: str,
    *,
    request=None,
    dataset: str = "",
    confirmation: str = "",
    attach_requester: bool = False,
    backup: str = "",
) -> HostTask:
    """Ask the VM agent to run one allowlisted task. Sysadmins only; one at a
    time. Audited (``system.task_requested`` with the kind, and the dataset
    for a reseed). A reseed needs demo data allowed here, a dataset from the
    registry and the word RESEED typed (D53)."""
    from audit.services import record_event
    from core import demo_mode, demo_reseed

    # Never while demo-switched (D97; review fixes 0.45.0, EXP-001): the
    # service refuses itself, behind the page and the policy.
    demo_mode.refuse_switched(request, error=HostTaskRefused)
    policy.require(actor, "system.run_task")
    if kind not in {spec.kind for spec in offered()}:
        raise HostTaskRefused("That task is not offered on this server.")
    if kind == Kind.RESEED_DEMO:
        demo_reseed.check_request(dataset, confirmation)
        attach_requester = attach_requester is True
    else:
        dataset, attach_requester = "", False
    if kind == Kind.RESTART_APP:
        check_restart(confirmation)
    if kind == Kind.RESTORE_BACKUP:
        check_restore(request, backup, confirmation)
    else:
        backup = ""
    waiting = current()
    if waiting is not None:
        raise HostTaskRefused(
            f"“{BY_KIND[waiting.kind].label if waiting.kind in BY_KIND else 'A task'}” is "
            "already waiting or running. Start another once it has finished."
        )
    try:
        with transaction.atomic():
            task = HostTask.objects.create(
                kind=kind,
                requested_by=actor,
                requested_by_user_id=getattr(actor, "pk", None),
                dataset=dataset,
                attach_requester=attach_requester,
                backup=backup,
            )
    except IntegrityError as exc:  # another request got in first
        raise HostTaskRefused(
            "Another server task is already waiting or running. Try again once it has finished."
        ) from exc
    metadata = {"kind": kind}
    if dataset:
        metadata["dataset"] = dataset
        metadata["attach_requester"] = attach_requester
    if backup:
        metadata["backup"] = backup
    record_event("system.task_requested", actor=actor, request=request, metadata=metadata)
    if kind == Kind.RESTORE_BACKUP:
        from core import task_mail

        task_mail.restore_requested(task)
    return task


def check_restore(request, backup: str, confirmation: str) -> None:
    """A restore needs a fresh passkey or authenticator check in this
    session, the word RESTORE typed exactly, and a backup point the server
    listed that this release can take (D98). The agent checks the point again
    against the backups that really exist."""
    from core import backup_points, demo_mode, fresh_mfa, preview

    demo_mode.refuse_switched(request, error=HostTaskRefused)
    if request is not None and preview.current_for(request.user) is not None:
        raise HostTaskRefused("Leave the homeowner view first.")
    if request is None or not fresh_mfa.is_fresh(request):
        raise HostTaskRefused(
            "Confirm it's you with your passkey or authenticator app first (within 5 minutes)."
        )
    if confirmation != RESTORE_WORD:
        raise HostTaskRefused(f"Type {RESTORE_WORD} in capitals to confirm the restore.")
    point = backup_points.find(backup)
    if point is None:
        raise HostTaskRefused("Choose one of the backup points listed on this page.")
    if point.refusal:
        raise HostTaskRefused(f"That backup cannot be restored here. {point.refusal}")


def last_restart() -> HostTask | None:
    return HostTask.objects.filter(kind=Kind.RESTART_APP).select_related("requested_by").first()


def restart_wait(now=None) -> datetime.timedelta | None:
    """How long until another restart may be asked for (None: now)."""
    last = last_restart()
    if last is None:
        return None
    left = last.requested_at + RESTART_COOLDOWN - (now or timezone.now())
    return left if left > datetime.timedelta(0) else None


def check_restart(confirmation: str) -> None:
    """A restart needs its confirmation page's fixed value and no other
    restart asked for in the last RESTART_COOLDOWN (D91)."""
    if confirmation != RESTART_CONFIRM_VALUE:
        raise HostTaskRefused("Confirm the restart on its own page first.")
    left = restart_wait()
    if left is not None:
        minutes = max(1, -(-int(left.total_seconds()) // 60))
        raise HostTaskRefused(
            "The portal was restarted a few minutes ago. Another restart can be asked for in "
            f"{minutes} minute{'s' if minutes != 1 else ''}."
        )


_CONTROL = re.compile(r"[^\t\n\x20-\x7e]")


def clean(text: str, limit: int) -> str:
    """Agent text for display: printable characters only, capped. The agent
    already strips it; this is the second guard (it writes straight to SQL)."""
    return _CONTROL.sub("", text or "")[:limit]


def record_finished() -> int:
    """Audit each finished task once (``system.task_finished``: kind, state).
    Run by the agent through ``manage.py record_host_tasks``. Returns how many."""
    from audit.models import Channel
    from audit.services import record_event

    done = 0
    pending = HostTask.objects.filter(finished_at__isnull=False, audited_at__isnull=True)
    for task in pending.order_by("id"):
        with transaction.atomic():
            locked = (
                HostTask.objects.select_for_update(skip_locked=True)
                .filter(pk=task.pk, audited_at__isnull=True)
                .first()
            )
            if locked is None:
                continue
            kind = locked.kind if locked.kind in BY_KIND else "unknown"
            metadata = {"kind": kind, "state": locked.state}
            if kind == Kind.RESEED_DEMO and locked.dataset:
                metadata["dataset"] = locked.dataset
            if kind == Kind.RESTORE_BACKUP and locked.backup:
                metadata["backup"] = locked.backup
            # Who asked, by login id only: after a restore that login may not
            # exist in the restored data (BUG-003, 0.45.0).
            if locked.requested_by_id is None and locked.requested_by_user_id is not None:
                metadata["requested_by_user_id"] = locked.requested_by_user_id
            record_event(
                "system.task_finished",
                actor=None,
                channel=Channel.CLI,
                target=locked,
                metadata=metadata,
            )
            locked.audited_at = timezone.now()
            locked.save(update_fields=["audited_at"])
            done += 1
    notify_finished()
    return done


def notify_finished() -> int:
    """Email each finished task's result once, and an Install update's (D98).
    A mail failure is logged and never stops the audit or the next task."""
    from core import task_mail
    from core.models import UpdateStatus

    sent = 0
    pending = HostTask.objects.filter(finished_at__isnull=False, notified_at__isnull=True)
    for pk in list(pending.order_by("id").values_list("pk", flat=True)):
        with transaction.atomic():
            locked = (
                HostTask.objects.select_for_update(skip_locked=True, of=("self",))
                .select_related("requested_by")
                .filter(pk=pk, notified_at__isnull=True)
                .first()
            )
            if locked is None:
                continue
            locked.notified_at = timezone.now()
            locked.save(update_fields=["notified_at"])
        try:
            sent += task_mail.task_finished(locked)
        except Exception as exc:  # never fails the agent's run
            logger.warning("host_tasks.notify_failed", extra={"error": type(exc).__name__})
    with transaction.atomic():
        status = (
            UpdateStatus.objects.select_for_update(skip_locked=True, of=("self",))
            .select_related("requested_by")
            .filter(pk=1, state__in=["succeeded", "failed"], state_changed_at__isnull=False)
            .first()
        )
        due = status is not None and (
            status.result_notified_at is None or status.result_notified_at < status.state_changed_at
        )
        if due:
            status.result_notified_at = timezone.now()
            status.save(update_fields=["result_notified_at"])
    if due:
        try:
            sent += task_mail.update_finished(status)
        except Exception as exc:
            logger.warning("host_tasks.notify_failed", extra={"error": type(exc).__name__})
    return sent


def last_restore() -> HostTask | None:
    return HostTask.objects.filter(kind=Kind.RESTORE_BACKUP).select_related("requested_by").first()


def backups_overview() -> dict:
    """The Backups card: last local copy, last off-site copy, lock verified
    (the heartbeats backup.sh and check_offsite_lock record), plus the health
    check's own verdict."""
    from core import health, offsite
    from core.models import Heartbeat

    beats = dict(
        Heartbeat.objects.filter(
            name__in=["backup_local", "backup_offsite", "offsite_lock", health.RESTORE_DRILL_BEAT]
        ).values_list("name", "at")
    )
    try:
        ok, detail = health._backups()
    except Exception as exc:  # shown like a failed health check
        ok, detail = False, f"Check failed ({type(exc).__name__})"
    return {
        "local": beats.get("backup_local"),
        "offsite": beats.get("backup_offsite"),
        "lock": beats.get("offsite_lock"),
        "drill": beats.get(health.RESTORE_DRILL_BEAT),
        "offsite_configured": offsite.configured(),
        "ok": ok,
        "detail": detail,
    }
