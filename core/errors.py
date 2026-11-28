"""Server error signatures (decision D45).

Every response with status 500 or more is recorded as a *signature*: the
exception class (or a label for the portal's deliberate 503 pages, or
"HTTP 5xx response" when a view returned one without raising), the route
(the URL name or view path), the method, where in our own code it happened
("app/file.py:123 in func"), the status and the release, counted per hour.

Never recorded: the request path, query string or URL arguments, the
exception message, source lines, local variables, the user or the IP address.
Those can carry tokens or personal data; a class name and a code location
cannot.
"""

from __future__ import annotations

import datetime
import hashlib
import re
import traceback
import zoneinfo
from pathlib import Path

from django.conf import settings
from django.utils import timezone
from django.views.debug import SafeExceptionReporterFilter

# How long counts and signatures are kept (the nightly purge).
RETENTION_DAYS = 30

LABEL_ATTR = "_error_label"
CAPTURE_ATTR = "_error_capture"

HEALTHZ_DB_DOWN = "Health probe: database unavailable"
EMAIL_UNAVAILABLE = "Email sending unavailable page"

_METHODS = {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT"}


def label_error(request, label: str) -> None:
    """Mark a deliberate 5xx answer so the error list names it plainly."""
    setattr(request, LABEL_ATTR, label)


def _project_root() -> str:
    return str(Path(settings.BASE_DIR).resolve()) + "/"


def _is_app_frame(filename: str, root: str) -> bool:
    if not filename.startswith(root):
        return False
    rel = filename[len(root) :]
    return not (
        "site-packages/" in rel
        or "dist-packages/" in rel
        or rel.startswith((".venv/", "venv/", ".tox/"))
    )


def where_from_traceback(tb) -> str:
    """The innermost frame in the project's own code, as "app/file.py:123 in
    func"; the innermost frame of all (library path only) when none is ours.
    File, line number and function name only: never source or variables."""
    frames = traceback.extract_tb(tb)
    if not frames:
        return ""
    root = _project_root()
    for frame in reversed(frames):
        filename = str(frame.filename)
        if _is_app_frame(filename, root):
            return f"{filename[len(root) :]}:{frame.lineno} in {frame.name}"[:300]
    frame = frames[-1]
    filename = str(frame.filename)
    for marker in ("site-packages/", "dist-packages/"):
        if marker in filename:
            filename = filename.rsplit(marker, 1)[1]
            break
    else:
        filename = Path(filename).name
    return f"{filename}:{frame.lineno} in {frame.name}"[:300]


def capture_exception(request, exception) -> None:
    """Remember the class and location of an unhandled exception on the
    request (the response path records it). Keeps strings only, so the
    traceback and its frames are not held on to."""
    if request is None or getattr(request, CAPTURE_ATTR, None):
        return
    setattr(
        request,
        CAPTURE_ATTR,
        (type(exception).__name__[:120], where_from_traceback(exception.__traceback__)),
    )


def on_request_exception(sender, request=None, **kwargs) -> None:
    """got_request_exception receiver: also catches exceptions raised in
    middleware, which process_exception never sees."""
    import contextlib
    import sys

    with contextlib.suppress(Exception):  # monitoring must never break a response
        exc = sys.exc_info()[1]
        if exc is not None:
            capture_exception(request, exc)


def _route(request) -> str:
    match = getattr(request, "resolver_match", None)
    return (getattr(match, "view_name", "") or "")[:200]


def _method(request) -> str:
    method = str(getattr(request, "method", "") or "").upper()
    return method if method in _METHODS else "OTHER"


def describe_request(request, status: int) -> dict:
    """The signature fields for a 5xx answer to this request."""
    label = getattr(request, LABEL_ATTR, None)
    captured = getattr(request, CAPTURE_ATTR, None)
    if label:
        exc_type, where = label, ""
    elif captured:
        exc_type, where = captured
    else:
        exc_type, where = f"HTTP {int(status)} response", ""
    return {
        "exc_type": exc_type,
        "route": _route(request),
        "method": _method(request),
        "where": where,
        "status": int(status),
        "release": str(getattr(settings, "APP_RELEASE", "") or "")[:40],
    }


def fingerprint(fields: dict) -> str:
    key = "|".join(
        str(fields[k]) for k in ("exc_type", "route", "method", "where", "status", "release")
    )
    return hashlib.sha256(key.encode()).hexdigest()[:40]


def record(request, status: int, *, now=None) -> None:
    """Count one 5xx answer under its signature for the current hour."""
    from django.db import IntegrityError, transaction
    from django.db.models import F

    from core.models import ErrorSignature

    now = now or timezone.now()
    hour = now.replace(minute=0, second=0, microsecond=0)
    fields = describe_request(request, status)
    fp = fingerprint(fields)
    rows = ErrorSignature.objects.filter(hour=hour, fingerprint=fp)
    if rows.update(count=F("count") + 1, last_seen=now):
        return
    try:
        with transaction.atomic():
            ErrorSignature.objects.create(
                hour=hour, fingerprint=fp, count=1, first_seen=now, last_seen=now, **fields
            )
    except IntegrityError:  # another worker created it first
        rows.update(count=F("count") + 1, last_seen=now)


def window_start(now=None) -> datetime.datetime:
    """Start of "the last hour" as the Server errors check counts it."""
    since = (now or timezone.now()) - datetime.timedelta(hours=1)
    return since.replace(minute=0, second=0, microsecond=0)


def summary(since, *, order: str = "count", limit: int | None = None) -> list[dict]:
    """Signatures seen since ``since``, merged across hours: the fields plus
    total, first and last. ``order`` is "count" (most first) or "recent"."""
    from django.db.models import Max, Min, Sum

    from core.models import ErrorSignature

    rows = (
        ErrorSignature.objects.filter(hour__gte=since)
        .values("fingerprint", "exc_type", "route", "method", "where", "status", "release")
        .annotate(total=Sum("count"), first=Min("first_seen"), last=Max("last_seen"))
    )
    rows = rows.order_by("-total", "-last") if order == "count" else rows.order_by("-last")
    return list(rows[:limit] if limit else rows)


def site_tz() -> zoneinfo.ZoneInfo:
    return zoneinfo.ZoneInfo(settings.TIME_ZONE)


def technical(row: dict) -> str:
    """The maintainer's line: class, code location, method and route, status,
    release and the times seen (site time)."""
    tz = site_tz()
    first = row["first"].astimezone(tz)
    last = row["last"].astimezone(tz)
    where = f" in {row['where']}" if row["where"] else ""
    route = row["route"] or "no route"
    release = row["release"] or "unknown"
    return (
        f"{row['exc_type']}{where} ({row['method']} {route}), HTTP {row['status']}, "
        f"release {release}, {first:%H:%M}\u2013{last:%H:%M} ({settings.TIME_ZONE})"
    )


def with_words(rows: list[dict]) -> list[dict]:
    """Each row with its plain sentence (``plain``) added."""
    from core.error_words import sentence

    return [{**r, "plain": sentence(r)} for r in rows]


def alert_lines(now=None, *, top: int = 5) -> list[str]:
    """The top kinds of server error in the last hour, for the alert email:
    a plain sentence first, then the technical line for the maintainer."""
    rows = with_words(summary(window_start(now), order="count"))
    lines = []
    for r in rows[:top]:
        lines.append(f"  • {r['total']}\u00d7 {r['plain']}")
        lines.append(f"      Technical: {technical(r)}")
    if len(rows) > top:
        more = len(rows) - top
        lines.append(f"  • and {more} more kind{'s' if more != 1 else ''} (see System status)")
    return lines


def purge(now=None) -> int:
    """Remove counts and signatures older than RETENTION_DAYS."""
    from core.models import ErrorSignature, ErrorTally

    cutoff = (now or timezone.now()) - datetime.timedelta(days=RETENTION_DAYS)
    removed, _ = ErrorSignature.objects.filter(hour__lt=cutoff).delete()
    tallies, _ = ErrorTally.objects.filter(hour__lt=cutoff).delete()
    return removed + tallies


# --- Error reports: settings never shown in clear -------------------------------------------


class PortalExceptionReporterFilter(SafeExceptionReporterFilter):
    """Django's filter also hides CREDENTIALS_KEK and other key-encryption
    keys, which its default pattern (API|TOKEN|KEY|SECRET|PASS|SIGNATURE)
    misses because "KEK" is not "KEY" (review EXP-007, 0.44.0)."""

    hidden_settings = re.compile(
        "API|AUTH|TOKEN|KEY|KEK|SECRET|PASS|SIGNATURE|HTTP_COOKIE|DSN|CREDENTIAL", flags=re.I
    )
