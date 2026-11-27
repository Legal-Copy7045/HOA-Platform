"""Synthetic monitoring data for the demo portal (D74): a month of daily
storage figures, a few incident timeline lines, and bounced email (one hard
bounce of an official notice, one soft bounce) for two demo owners.

Called once from the oak_creek dataset (``community/demo/oak_creek.py``).
Everything is invented; the addresses are the seeded example.com ones,
stored only as keyed hashes like real bounces.
"""

from __future__ import annotations

import datetime

from django.utils import timezone

from core.scoping import system_access

GIB = 1024**3


def seed(now=None) -> dict:
    now = now or timezone.now()
    return {
        "snapshots": _snapshots(now),
        "events": _events(now),
        "bounces": _bounces(now),
    }


def _snapshots(now) -> int:
    """30 days up to yesterday: the database grows ~2 MB a day, the disk
    ~150 MB a day on a 40 GB disk (so the estimate says about 4 months)."""
    from core.models import GrowthSnapshot

    today = timezone.localdate(now)
    made = 0
    for back in range(30, 0, -1):
        day = today - datetime.timedelta(days=back)
        if GrowthSnapshot.objects.filter(taken_on=day).exists():
            continue
        n = 30 - back
        used = int(20 * GIB + n * 150 * 1024**2)
        GrowthSnapshot.objects.create(
            taken_on=day,
            taken_at=now - datetime.timedelta(days=back),
            database_bytes=180 * 1024**2 + n * 2 * 1024**2,
            largest_tables=[
                ["audit_auditevent", 60 * 1024**2 + n * 900_000],
                ["ledger_ledgerentry", 24 * 1024**2 + n * 200_000],
                ["procrastinate_events", 9 * 1024**2],
            ],
            connections=6 + n % 4,
            max_connections=100,
            files_bytes=900 * 1024**2 + n * 5 * 1024**2,
            files_count=410 + n * 2,
            disk_total_bytes=40 * GIB,
            disk_free_bytes=40 * GIB - used,
            disk_source="agent",
        )
        made += 1
    return made


# (hours ago, kind, key, state, message, count): spread over the last fortnight.
EVENTS = [
    (
        290,
        "backups",
        "local",
        "info",
        "Backup taken: an encrypted copy of the database and the files on the server",
        0,
    ),
    (289, "backups", "offsite", "info", "Off-site copy uploaded to the locked bucket", 0),
    (
        216,
        "jobs",
        "worker",
        "problem",
        "Background worker: Started failing. No report from the background worker for 25 minutes",
        0,
    ),
    (216, "alerts", "health", "info", "Health email: PROBLEM  Background worker", 2),
    (214, "jobs", "worker", "recovered", "Background worker: Recovered. Last report 1 min ago", 0),
    (144, "updates", "update", "info", "Update installed: Running demo123", 0),
    (
        72,
        "email",
        "bounces",
        "problem",
        "5 hard bounces read in one check of the bounce mailbox (addresses that do not accept "
        "mail): see People and My items",
        5,
    ),
    (
        50,
        "backups",
        "restore_drill",
        "info",
        "Restore drill passed: the signed backup verified and restored into a throwaway copy",
        0,
    ),
]


def _events(now) -> int:
    from core.models import OpsEvent

    if OpsEvent.objects.exists():
        return 0
    for hours, kind, key, state, message, count in EVENTS:
        OpsEvent.objects.create(
            at=now - datetime.timedelta(hours=hours),
            kind=kind,
            key=key,
            state=state,
            message=message,
            count=count,
        )
    return len(EVENTS)


def _bounces(now) -> int:
    from comms import bounces
    from comms.models import EmailBounce
    from community.models import Ownership

    with system_access("demo bounces"):
        if EmailBounce.objects.exists():
            return 0
        owners = list(
            Ownership.objects.active()
            .filter(person__email__isnull=False)
            .exclude(person__email="")
            .select_related("person")
            .order_by("lot__number")[:12]
        )
        if len(owners) < 12:
            return 0
        made = 0
        for own, kind, mail_kind, status, days in (
            (owners[10], "hard", "notice", "5.1.1", 3),
            (owners[11], "soft", "statement", "4.2.2", 5),
        ):
            sent = now - datetime.timedelta(days=days, hours=1)
            EmailBounce.objects.create(
                person=own.person,
                address_hash=bounces.address_hash(own.person.email),
                kind=kind,
                mail_kind=mail_kind,
                status=status,
                sent_at=sent,
                received_at=now - datetime.timedelta(days=days),
                dedupe=f"demo-{own.person.pk}-{mail_kind}",
            )
            made += 1
    return made
