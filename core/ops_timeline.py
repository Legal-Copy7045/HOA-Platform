"""The operational incident timeline (D74): one list, newest first, of what
happened to the portal as a system.

Sources, merged when the page is read:

* ``OpsEvent`` rows (append-only): health checks that started failing or
  recovered (written by ``core.health.alert``), alert emails sent, backups
  and restore drills (``backup_heartbeat``), updates installed (the VM
  agent), host-security alerts (``host_alert``), bursts of bounced email;
* server error signatures, once each, at the time first seen (D45);
* server tasks that finished (D36).

Operations words and counts only. Nothing here carries a name, an email
address, a home, a URL, a query or an exception message, so the page is the
sysadmin's (``system.view_status``) like System status itself.
"""

from __future__ import annotations

import contextlib
import datetime
import logging
from dataclasses import dataclass

from django.utils import timezone

from core.models import OpsEvent, OpsEventKind

logger = logging.getLogger(__name__)

PER_PAGE = 25
WINDOW_DAYS = 90

# Health checks shown under a more specific filter than "Health checks".
CHECK_KINDS = {
    "backups": OpsEventKind.BACKUPS,
    "scan_queue": OpsEventKind.SCANS,
    "signatures": OpsEventKind.SCANS,
    "payments": OpsEventKind.PAYMENTS,
    "payment_failures": OpsEventKind.PAYMENTS,
    "email": OpsEventKind.EMAIL,
    "bounce_mailbox": OpsEventKind.EMAIL,
    "jobs": OpsEventKind.JOBS,
    "worker": OpsEventKind.JOBS,
    "disk": OpsEventKind.STORAGE,
    "storage_growth": OpsEventKind.STORAGE,
    "connections": OpsEventKind.STORAGE,
    "storage": OpsEventKind.STORAGE,
}

# Filters on the page: the stored kinds plus the merged sources.
ERRORS = "errors"
TASKS = "tasks"
FILTERS = [(k.value, k.label) for k in OpsEventKind] + [
    (ERRORS, "Server errors"),
    (TASKS, "Server tasks"),
]
FILTER_KEYS = {k for k, _ in FILTERS}


def record(kind: str, message: str, *, key: str = "", state: str = "info", count: int = 0):
    """Add one line. Never fails the caller (monitoring must not break the
    thing it watches); the message is cut to fit."""
    from django.db import transaction

    try:
        with transaction.atomic():  # a savepoint: a failure never spoils the caller's work
            return OpsEvent.objects.create(
                kind=kind,
                key=key[:40],
                state=state[:10],
                message=" ".join(str(message).split())[:300],
                count=max(0, int(count)),
            )
    except Exception as exc:  # the type only: never the message
        logger.warning("ops_timeline.not_recorded", extra={"exc_type": type(exc).__name__})
        return None


def check_kind(key: str) -> str:
    return CHECK_KINDS.get(key, OpsEventKind.HEALTH)


@dataclass
class Entry:
    at: datetime.datetime
    kind: str
    label: str  # the filter's name
    state: str  # problem / recovered / info
    title: str
    detail: str = ""


_LABELS = dict(FILTERS)


def _ops_entries(kind: str | None, since, limit: int) -> list[Entry]:
    rows = OpsEvent.objects.filter(at__gte=since)
    if kind:
        rows = rows.filter(kind=kind)
    out = []
    for e in rows.order_by("-at", "-id")[:limit]:
        out.append(Entry(e.at, e.kind, _LABELS.get(e.kind, e.kind), e.state, e.message, _count(e)))
    return out


def _count(e: OpsEvent) -> str:
    if e.kind == OpsEventKind.ALERTS and e.count:
        return f"Sent to {e.count} sysadmin{'s' if e.count != 1 else ''}"
    return ""


def _error_entries(since, limit: int) -> list[Entry]:
    """Each kind of server error once, when it was first seen (D45)."""
    from django.db.models import Min, Sum

    from core.error_words import sentence
    from core.models import ErrorSignature

    rows = (
        ErrorSignature.objects.filter(first_seen__gte=since)
        .values("fingerprint", "exc_type", "route", "status")
        .annotate(first=Min("first_seen"), total=Sum("count"))
        .order_by("-first")[:limit]
    )
    return [
        Entry(
            r["first"],
            ERRORS,
            _LABELS[ERRORS],
            "problem",
            "First seen: " + sentence(r),
            f"{r['total']} time{'s' if r['total'] != 1 else ''} since",
        )
        for r in rows
    ]


def _task_entries(since, limit: int) -> list[Entry]:
    from core.host_tasks import BY_KIND
    from core.models import HostTask

    out = []
    for t in HostTask.objects.filter(finished_at__gte=since).order_by("-finished_at")[:limit]:
        label = BY_KIND[t.kind].label if t.kind in BY_KIND else "A server task"
        state = "problem" if t.state in (HostTask.State.FAILED, HostTask.State.REFUSED) else "info"
        out.append(
            Entry(
                t.finished_at,
                TASKS,
                _LABELS[TASKS],
                state,
                f"{label}: {t.get_state_display().lower()}",
                "Started from Server tasks",
            )
        )
    return out


def entries(kind: str | None = None, *, page: int = 1, now=None) -> tuple[list[Entry], bool]:
    """One page of the timeline, newest first, and whether there is more.
    ``kind`` is one of FILTER_KEYS or None for everything."""
    if kind not in FILTER_KEYS:
        kind = None
    page = max(1, int(page))
    now = now or timezone.now()
    since = now - datetime.timedelta(days=WINDOW_DAYS)
    want = page * PER_PAGE + 1  # one more than shown, to know about a next page
    merged: list[Entry] = []
    if kind is None or kind in {k.value for k in OpsEventKind}:
        merged += _ops_entries(kind, since, want)
    if kind in (None, ERRORS):
        with contextlib.suppress(Exception):  # a broken source never hides the rest
            merged += _error_entries(since, want)
    if kind in (None, TASKS):
        with contextlib.suppress(Exception):
            merged += _task_entries(since, want)
    merged.sort(key=lambda e: e.at, reverse=True)
    start = (page - 1) * PER_PAGE
    return merged[start : start + PER_PAGE], len(merged) > start + PER_PAGE


def recent(limit: int = 8) -> list[Entry]:
    """The newest few, for the System status card."""
    rows, _ = entries(None)
    return rows[:limit]


# --- Hourly tallies (OpsTally) -------------------------------------------------------------


def bump(name: str, code: str = "", *, now=None) -> None:
    """Count one occurrence of ``name`` in the current hour (e.g. Stripe not
    answering). ``code`` is a short machine word, never a message. Never
    fails the caller."""
    from django.db import IntegrityError, transaction
    from django.db.models import F

    from core.models import OpsTally

    hour = (now or timezone.now()).replace(minute=0, second=0, microsecond=0)
    code = "".join(ch for ch in str(code) if ch.isalnum() or ch in "_-.")[:40]
    try:
        with transaction.atomic():
            rows = OpsTally.objects.filter(name=name, hour=hour)
            if not rows.update(count=F("count") + 1, last_code=code):
                try:
                    with transaction.atomic():
                        OpsTally.objects.create(name=name, hour=hour, count=1, last_code=code)
                except IntegrityError:  # another process created it first
                    rows.update(count=F("count") + 1, last_code=code)
    except Exception as exc:  # monitoring never breaks the caller
        logger.warning("ops_tally.not_recorded", extra={"exc_type": type(exc).__name__})


def tally(name: str, *, hours: int, now=None) -> tuple[int, str]:
    """(count in the last ``hours`` whole hours, the latest code)."""
    from core.models import OpsTally

    now = now or timezone.now()
    since = (now - datetime.timedelta(hours=hours)).replace(minute=0, second=0, microsecond=0)
    rows = list(OpsTally.objects.filter(name=name, hour__gte=since).order_by("-hour"))
    return sum(r.count for r in rows), (rows[0].last_code if rows else "")


def purge_tallies(days: int = 30, now=None) -> int:
    from core.models import OpsTally

    cutoff = (now or timezone.now()) - datetime.timedelta(days=days)
    removed, _ = OpsTally.objects.filter(hour__lt=cutoff).delete()
    return removed
