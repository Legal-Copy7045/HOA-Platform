"""Health checks and alerts (Milestone 4).

Each check returns (ok, detail). Details are short and never contain personal
data, URLs, secrets or exception messages. ``alert()`` emails the sysadmins
when a check starts failing, again every few hours while it keeps failing,
and once when it recovers.
"""

from __future__ import annotations

import contextlib
import datetime
import shutil
import socket
import uuid
from dataclasses import dataclass

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.mail import send_mail
from django.db import connection
from django.utils import timezone

REALERT_HOURS = 6
WORKER_STALE_MINUTES = 20
SIGNATURES_STALE_DAYS = 3
ERRORS_PER_HOUR = 20
DISK_FULL_PERCENT = 90
BACKUP_STALE_HOURS = 36
# A successful restore drill (restore-drill.sh records it) proves the backups
# and the key still work; production needs one at least this often (D57).
RESTORE_DRILL_BEAT = "restore_drill"
RESTORE_DRILL_STALE_DAYS = 90
# Files waiting for a malware scan (D57): "stuck" on System status after
# STUCK, a failing health check after ALERT.
SCAN_STUCK_MINUTES = 15
SCAN_ALERT_MINUTES = 30


@dataclass
class Result:
    key: str
    label: str
    ok: bool
    detail: str


def _database():
    with connection.cursor() as cur:
        cur.execute("SELECT 1")
    return True, "Responding"


def _worker():
    from core.models import Heartbeat

    beat = Heartbeat.objects.filter(name="worker").first()
    if beat is None:
        return False, "The background worker has not reported yet"
    age = timezone.now() - beat.at
    minutes = int(age.total_seconds() // 60)
    if age > datetime.timedelta(minutes=WORKER_STALE_MINUTES):
        return False, f"No report from the background worker for {minutes} minutes"
    return True, f"Last report {minutes} min ago"


def _disk():
    usage = shutil.disk_usage("/")
    percent = int(usage.used * 100 / usage.total)
    return percent < DISK_FULL_PERCENT, f"{percent}% used"


def _storage():
    name = f"healthcheck/{uuid.uuid4().hex}.txt"
    try:
        saved = default_storage.save(name, ContentFile(b"ok"))
        default_storage.delete(saved)
    except Exception as exc:  # report the store's code only
        response = getattr(exc, "response", None) or {}
        code = (response.get("Error") or {}).get("Code") or type(exc).__name__
        return False, f"Write failed ({code})"
    return True, "Write and delete work"


def scanner_version() -> dict:
    """What the ClamAV daemon says about itself: {"answering", "version",
    "signatures_at" (naive, the daemon's clock)}. Empty values when off or
    silent. Used by the health check and the System status card."""
    out = {"answering": False, "version": "", "signatures_at": None}
    if settings.DOCUMENT_SCANNER != "clamd":
        return out
    host, _, port = settings.DOCUMENT_CLAMD_ADDRESS.partition(":")
    try:
        with socket.create_connection((host, int(port or 3310)), timeout=10) as sock:
            sock.sendall(b"zVERSION\0")
            reply = sock.recv(512).decode(errors="replace").strip("\0\n ")
    except (OSError, ValueError):
        return out
    out["answering"] = True
    # "ClamAV 1.4.2/27850/Tue Nov 25 08:42:01 2026"
    parts = reply.split("/")
    out["version"] = parts[0].replace("ClamAV", "").strip()[:20]
    if len(parts) >= 3:
        with contextlib.suppress(ValueError):
            out["signatures_at"] = datetime.datetime.strptime(
                parts[2].strip(), "%a %b %d %H:%M:%S %Y"
            )
    return out


def _signatures():
    if settings.DOCUMENT_SCANNER != "clamd":
        return False, "Malware scanning is off"
    info = scanner_version()
    if not info["answering"]:
        return False, "The scanner is not answering"
    stamp = info["signatures_at"]
    if stamp is None:
        return True, "Answering (signature date unknown)"
    age = (timezone.now().replace(tzinfo=None) - stamp).days
    if age > SIGNATURES_STALE_DAYS:
        return False, f"Signatures are {age} days old"
    return True, f"Signatures {age} day(s) old"


def _scan_queue():
    """Files waiting for a malware scan (D57). Only while scanning is on."""
    if settings.DOCUMENT_SCANNER != "clamd":
        return None
    from documents import scan_queue

    files = scan_queue.waiting_files()
    if not files:
        return True, "None waiting"
    oldest = scan_queue.oldest_wait_minutes(files)
    count = f"{len(files)} file{'s' if len(files) != 1 else ''}"
    if oldest > SCAN_ALERT_MINUTES:
        return False, (
            f"{count} cannot be downloaded until scanned; the oldest has waited {oldest} "
            "minutes. Check the Malware scanner line above, then use Scan waiting files now "
            "on System status"
        )
    return True, f"{count} waiting, the oldest for {oldest} minutes (retried every 10 minutes)"


def _errors():
    from core.errors import window_start
    from core.models import ErrorTally

    total = sum(ErrorTally.objects.filter(hour__gte=window_start()).values_list("count", flat=True))
    return total < ERRORS_PER_HOUR, f"{total} server error(s) in the last hour"


def _email():
    from django.core.cache import cache

    from core.email import FAILURE_CACHE_KEY

    failed = cache.get(FAILURE_CACHE_KEY)
    if not failed:
        return True, "No send failures in the last day"
    when, kind = failed
    return (
        False,
        f"Sending failed at {when[:16].replace('T', ' ')} UTC ({kind}): check EMAIL_* settings",
    )


def _backups():
    from core import offsite
    from core.models import Heartbeat

    beats = dict(Heartbeat.objects.filter(name__startswith="backup_").values_list("name", "at"))
    beats.update(
        Heartbeat.objects.filter(name__in=["offsite_lock", RESTORE_DRILL_BEAT]).values_list(
            "name", "at"
        )
    )
    now = timezone.now()
    production = settings.DEPLOYMENT_STAGE == "production"

    def age(name):
        at = beats.get(name)
        return None if at is None else int((now - at).total_seconds() // 3600)

    drill_at = beats.get(RESTORE_DRILL_BEAT)
    drill_days = None if drill_at is None else (now - drill_at).days
    drill_late = drill_days is None or drill_days > RESTORE_DRILL_STALE_DAYS
    drill = (
        "restore drill never run" if drill_days is None else f"restore drill {drill_days} days ago"
    )

    local = age("backup_local")
    if local is None:
        return False, "No backup has run yet (Admin → Server tasks → Take a backup)"
    if local > BACKUP_STALE_HOURS:
        return False, f"Last backup {local} hours ago"
    if not offsite.configured():
        if production:
            return False, (
                f"Last backup {local} h ago, but no off-site copy is set up (needed in "
                f"production: run setup-offsite-backups.sh); {drill}"
            )
        return True, f"Last backup {local} h ago. Not production-ready: no off-site copy; {drill}"
    remote = age("backup_offsite")
    if remote is None or remote > BACKUP_STALE_HOURS:
        when = "never" if remote is None else f"{remote} hours ago"
        return False, f"Last backup {local} h ago, but off-site copy {when}"
    lock = beats.get("offsite_lock")
    locked = f"lock verified {timezone.localdate(lock):%b %-d}" if lock else "lock not verified"
    detail = f"Last backup {local} h ago; off-site {remote} h ago, {locked}; {drill}"
    if production and drill_late:
        return False, (
            f"{detail}: a restore drill is needed at least every {RESTORE_DRILL_STALE_DAYS} "
            "days in production (Admin → Server tasks → Restore drill)"
        )
    if drill_late:
        detail += f". Not production-ready: no restore drill in {RESTORE_DRILL_STALE_DAYS} days"
    return True, detail


def _bank_feed():
    """Only shown once a Teller account is connected (bank_feeds feature)."""
    from ledger.bankfeeds.services import connections, feature_on, needs_attention
    from ledger.models import BankConnectionStatus, BankProvider

    teller = list(connections().filter(provider=BankProvider.TELLER))
    if not teller or not feature_on():
        return None
    if any(c.status == BankConnectionStatus.NEEDS_REAUTH for c in teller):
        return False, "A bank account needs reconnecting"
    if any(needs_attention(c) for c in teller):
        return False, "Not synced for more than 3 days"
    return True, "Synced in the last 3 days"


def _demo_data():
    """Only shown if demo seeding is on where it must never be (D53)."""
    if settings.SEED_DEMO_ALLOWED and settings.DEPLOYMENT_STAGE == "production":
        return False, "Demo data loading is switched on in production: switch it off now"
    return None


def _usps_tracking():
    from comms.usps_tracking import health

    return health()


def _payments():
    from payments.sync import health

    return health()


def _assessment_posting():
    """Automatic monthly posting (D48): only shown while it is switched on."""
    from ledger.autopost import health

    return health()


LOW_MEMORY_MINUTES = 10


def resources() -> dict | None:
    """The server's memory, swap and load as the VM agent last reported them
    (D39), with figures ready to show; None when there is no recent report."""
    from core.models import UpdateStatus

    status = UpdateStatus.objects.filter(pk=1).first()
    data = (status.host_resources if status else None) or {}
    if not data.get("mem_total_kb") or not status.agent_seen_at:
        return None
    if status.agent_seen_at < timezone.now() - datetime.timedelta(minutes=5):
        return None
    gib = 1024 * 1024
    total, avail = data["mem_total_kb"], data.get("mem_available_kb", 0)
    swap_used = max(0, data.get("swap_total_kb", 0) - data.get("swap_free_kb", 0))
    low_since = None
    if data.get("low_since"):
        try:
            low_since = datetime.datetime.fromisoformat(str(data["low_since"]))
        except ValueError:
            low_since = None
    return {
        "used_gb": round((total - avail) / gib, 1),
        "total_gb": round(total / gib, 1),
        "available_percent": int(avail * 100 / total),
        "swap_used_mb": swap_used // 1024,
        "swap_total_mb": data.get("swap_total_kb", 0) // 1024,
        "load": [data.get("load1", 0), data.get("load5", 0), data.get("load15", 0)],
        "cpus": data.get("cpus") or 1,
        "low_since": low_since,
    }


def _memory():
    res = resources()
    if res is None:
        return None  # no agent report (not on the VM, or the agent is down)
    detail = (
        f"{res['used_gb']} of {res['total_gb']} GB in use, swap {res['swap_used_mb']} MB, "
        f"load {res['load'][0]} ({res['cpus']} cores)"
    )
    low_since = res["low_since"]
    if low_since is not None:
        minutes = int((timezone.now() - low_since).total_seconds() // 60)
        if minutes >= LOW_MEMORY_MINUTES:
            return False, f"Less than 10% of memory free for {minutes} minutes: {detail}"
    return True, detail


CHECKS = [
    ("database", "Database", _database),
    ("worker", "Background worker", _worker),
    ("disk", "Disk space", _disk),
    ("storage", "File store", _storage),
    ("signatures", "Malware scanner", _signatures),
    ("scan_queue", "Files waiting for a malware scan", _scan_queue),
    ("errors", "Server errors", _errors),
    ("email", "Outgoing email", _email),
    ("backups", "Backups", _backups),
    ("memory", "Server memory", _memory),
    ("bank_feed", "Bank feed", _bank_feed),
    ("payments", "Online payments", _payments),
    ("assessment_posting", "Monthly assessment posting", _assessment_posting),
    ("demo_data", "Demo data", _demo_data),
    ("usps_tracking", "USPS tracking", _usps_tracking),
]


# Checks that remember past failures; a sysadmin can clear them once fixed.
CLEARABLE = {"email", "errors"}


def clear(key: str) -> None:
    """Forget a sticky failure (System status "Clear"); the next run decides
    again from fresh data. The caller audits it."""
    if key not in CLEARABLE:
        raise ValueError("That check cannot be cleared.")
    if key == "email":
        from core.email import clear_failure

        clear_failure()
    else:
        from core.models import ErrorSignature, ErrorTally

        # Forget the last hours' counts and signatures (no URLs, messages or
        # users in either), so the check and the next email start afresh.
        since = timezone.now() - datetime.timedelta(hours=2)
        ErrorTally.objects.filter(hour__gte=since).delete()
        ErrorSignature.objects.filter(hour__gte=since).delete()


def run_checks() -> list[Result]:
    out = []
    for key, label, fn in CHECKS:
        try:
            result = fn()
            if result is None:  # not applicable here (e.g. nothing connected)
                continue
            ok, detail = result
        except Exception as exc:  # a broken check is itself a failure
            ok, detail = False, f"Check failed ({type(exc).__name__})"
        out.append(Result(key, label, ok, detail))
    return out


def sysadmin_emails() -> list[str]:
    from accounts.models import User
    from core.policy import is_sysadmin

    return [u.email for u in User.objects.filter(is_active=True) if is_sysadmin(u)]


def _error_lines(now) -> list[str]:
    """What the server errors were (D45): the top kinds in the last hour.
    Class names, route names and code locations only."""
    from core.errors import alert_lines

    try:
        return alert_lines(now)
    except Exception as exc:  # the alert itself must still go out
        return [f"  (error list unavailable: {type(exc).__name__})"]


def alert(results: list[Result], *, now=None) -> int:
    """Email the sysadmins about changes. Returns the number of emails sent."""
    from core.models import AlertState

    now = now or timezone.now()
    news = []
    for r in results:
        state, _ = AlertState.objects.get_or_create(name=r.key)
        if not r.ok:
            due = state.last_alerted_at is None or (
                now - state.last_alerted_at > datetime.timedelta(hours=REALERT_HOURS)
            )
            if not state.failing or due:
                news.append(f"PROBLEM  {r.label}: {r.detail}")
                if r.key == "errors":
                    news.extend(_error_lines(now))
                state.last_alerted_at = now
            if not state.failing:
                state.since = now
            state.failing = True
        elif state.failing:
            news.append(f"RECOVERED  {r.label}: {r.detail}")
            state.failing, state.since = False, None
        state.save()
    if not news:
        return 0
    recipients = sysadmin_emails()
    body = (
        "Portal health report:\n\n"
        + "\n".join(news)
        + f"\n\nDetails: {settings.PORTAL_BASE_URL}/ops/status/\n\n-- \n{settings.SITE_NAME}"
    )
    for address in recipients:
        send_mail(
            f"[{settings.SITE_NAME}] Health: {news[0].split(':')[0].strip()}",
            body,
            None,
            [address],
            fail_silently=True,
        )
    return len(recipients)


def record_server_error(now=None) -> None:
    from django.db.models import F

    from core.models import ErrorTally

    hour = (now or timezone.now()).replace(minute=0, second=0, microsecond=0)
    updated = ErrorTally.objects.filter(hour=hour).update(count=F("count") + 1)
    if not updated:
        ErrorTally.objects.get_or_create(hour=hour, defaults={"count": 1})
