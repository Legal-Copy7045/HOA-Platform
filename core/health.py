"""Health checks and alerts (Milestone 4).

Each check returns (ok, detail). Details are short and never contain personal
data, URLs, secrets or exception messages. ``alert()`` emails the sysadmins
when a check starts failing, again every few hours while it keeps failing,
and once when it recovers.
"""

from __future__ import annotations

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


def _signatures():
    if settings.DOCUMENT_SCANNER != "clamd":
        return False, "Malware scanning is off"
    host, _, port = settings.DOCUMENT_CLAMD_ADDRESS.partition(":")
    try:
        with socket.create_connection((host, int(port or 3310)), timeout=10) as sock:
            sock.sendall(b"zVERSION\0")
            reply = sock.recv(512).decode(errors="replace").strip("\0\n ")
    except (OSError, ValueError):
        return False, "The scanner is not answering"
    # "ClamAV 1.4.2/27850/Tue Nov 25 08:42:01 2026"
    parts = reply.split("/")
    if len(parts) < 3:
        return True, "Answering (signature date unknown)"
    try:
        stamp = datetime.datetime.strptime(parts[2].strip(), "%a %b %d %H:%M:%S %Y")
    except ValueError:
        return True, "Answering (signature date unknown)"
    age = (timezone.now().replace(tzinfo=None) - stamp).days
    if age > SIGNATURES_STALE_DAYS:
        return False, f"Signatures are {age} days old"
    return True, f"Signatures {age} day(s) old"


def _errors():
    from core.models import ErrorTally

    since = timezone.now() - datetime.timedelta(hours=1)
    total = sum(
        ErrorTally.objects.filter(
            hour__gte=since.replace(minute=0, second=0, microsecond=0)
        ).values_list("count", flat=True)
    )
    return total < ERRORS_PER_HOUR, f"{total} server error(s) in the last hour"


CHECKS = [
    ("database", "Database", _database),
    ("worker", "Background worker", _worker),
    ("disk", "Disk space", _disk),
    ("storage", "File store", _storage),
    ("signatures", "Malware scanner", _signatures),
    ("errors", "Server errors", _errors),
]


def run_checks() -> list[Result]:
    out = []
    for key, label, fn in CHECKS:
        try:
            ok, detail = fn()
        except Exception as exc:  # a broken check is itself a failure
            ok, detail = False, f"Check failed ({type(exc).__name__})"
        out.append(Result(key, label, ok, detail))
    return out


def sysadmin_emails() -> list[str]:
    from accounts.models import User
    from core.policy import is_sysadmin

    return [u.email for u in User.objects.filter(is_active=True) if is_sysadmin(u)]


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
