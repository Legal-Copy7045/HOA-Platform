"""Server tasks (Admin -> Server tasks, decision D36).

Reusable host operations (a backup, a restore drill, the off-site lock check,
re-applying host security, the health checks) are started in the portal and
run by the VM agent from a fixed allowlist. The web app never touches the
host: it only inserts a ``core.HostTask`` row naming one of the kinds below.
The agent (infra/poc/portal-agent.sh with infra/poc/host-tasks.sh) maps the
kind to a fixed command with a bash ``case``; nothing else from the database
reaches a command line, except the dataset of a demo reseed (D53), which the
agent accepts only as a plain name ([a-z_]) the portal's own seed_demo lists.
Results come back as a state and a short summary.
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass

from django.db import IntegrityError, transaction
from django.utils import timezone

from core import policy
from core.models import HostTask

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
        "Falco). Safe to repeat.",
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
) -> HostTask:
    """Ask the VM agent to run one allowlisted task. Sysadmins only; one at a
    time. Audited (``system.task_requested`` with the kind, and the dataset
    for a reseed). A reseed needs demo data allowed here, a dataset from the
    registry and the word RESEED typed (D53)."""
    from audit.services import record_event
    from core import demo_reseed

    policy.require(actor, "system.run_task")
    if kind not in {spec.kind for spec in offered()}:
        raise HostTaskRefused("That task is not offered on this server.")
    if kind == Kind.RESEED_DEMO:
        demo_reseed.check_request(dataset, confirmation)
        attach_requester = attach_requester is True
    else:
        dataset, attach_requester = "", False
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
                dataset=dataset,
                attach_requester=attach_requester,
            )
    except IntegrityError as exc:  # another request got in first
        raise HostTaskRefused(
            "Another server task is already waiting or running. Try again once it has finished."
        ) from exc
    metadata = {"kind": kind}
    if dataset:
        metadata["dataset"] = dataset
        metadata["attach_requester"] = attach_requester
    record_event("system.task_requested", actor=actor, request=request, metadata=metadata)
    return task


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
    return done


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
