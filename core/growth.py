"""Storage capacity and database growth (D74), kept light.

Once a day ``snapshot()`` records an append-only ``GrowthSnapshot``: the
database's size (``pg_database_size``), its ten largest tables, connections
in use against ``max_connections``, the uploaded files' total size (from the
sizes the portal already stores, no listing of the bucket) and the server's
disk (the VM agent's report, else what the container sees).

``days_until_full`` fits a straight line through the disk use of the last
90 days (least squares, whole bytes per day) and divides the free space by
it: "at this rate, the disk is full in N days". The board's rule
``monitoring.disk_full_warn_days`` decides when that becomes a failing
health check. ``connections()`` is its own check: above 80% of
``max_connections`` warns.
"""

from __future__ import annotations

import datetime
import shutil

from django.apps import apps
from django.db import IntegrityError, connection, transaction
from django.utils import timezone

TREND_DAYS = 90
MIN_POINTS = 7  # fewer days than this: no estimate yet
CONNECTIONS_WARN_PERCENT = 80
WARN_RULE = "monitoring.disk_full_warn_days"
WARN_DEFAULT_DAYS = 60


def database_bytes() -> int:
    with connection.cursor() as cur:
        cur.execute("SELECT pg_database_size(current_database())")
        return int(cur.fetchone()[0])


def largest_tables(limit: int = 10) -> list[list]:
    """[[table, bytes]] of the public schema's largest tables (with indexes
    and TOAST), largest first. Names only."""
    with connection.cursor() as cur:
        cur.execute(
            "SELECT c.relname, pg_total_relation_size(c.oid) FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') "
            "ORDER BY pg_total_relation_size(c.oid) DESC, c.relname LIMIT %s",
            [limit],
        )
        return [[name, int(size)] for name, size in cur.fetchall()]


def connections() -> tuple[int, int]:
    """(client connections now, max_connections)."""
    with connection.cursor() as cur:
        cur.execute("SELECT count(*) FROM pg_stat_activity WHERE backend_type = 'client backend'")
        used = int(cur.fetchone()[0])
        cur.execute("SHOW max_connections")
        maximum = int(cur.fetchone()[0])
    return used, maximum


def files_total() -> tuple[int | None, int | None]:
    """(bytes, count) of uploaded files, from the sizes recorded at upload.
    Kept files only; nothing is listed in the bucket."""
    from django.db.models import Count, Sum

    from core.scoping import system_access

    total = count = 0
    found = False
    with system_access("uploaded file sizes"):
        for label in ("documents.DocumentVersion", "documents.Attachment"):
            try:
                model = apps.get_model(label)
            except LookupError:
                continue
            found = True
            row = model.objects.aggregate(size=Sum("size"), n=Count("pk"))
            total += int(row["size"] or 0)
            count += int(row["n"] or 0)
    return (total, count) if found else (None, None)


def disk() -> tuple[int | None, int | None, str]:
    """(total bytes, free bytes, source): the VM's disk from the agent's
    last report when there is one (D39), else the container's root."""
    from core.models import UpdateStatus

    status = UpdateStatus.objects.filter(pk=1).first()
    data = (status.host_resources if status else None) or {}
    fresh = timezone.now() - datetime.timedelta(minutes=10)
    if data.get("disk_total_kb") and status.agent_seen_at and status.agent_seen_at >= fresh:
        return (
            int(data["disk_total_kb"]) * 1024,
            int(data.get("disk_avail_kb", 0)) * 1024,
            "agent",
        )
    usage = shutil.disk_usage("/")
    return usage.total, usage.free, "container"


def snapshot(now=None):
    """Record today's figures once (later runs the same day do nothing).
    Returns the snapshot, or None when today's already exists."""
    from core.models import GrowthSnapshot

    now = now or timezone.now()
    day = timezone.localdate(now)
    if GrowthSnapshot.objects.filter(taken_on=day).exists():
        return None
    used, maximum = connections()
    files, count = files_total()
    total, free, source = disk()
    try:
        with transaction.atomic():
            return GrowthSnapshot.objects.create(
                taken_on=day,
                taken_at=now,
                database_bytes=database_bytes(),
                largest_tables=largest_tables(),
                connections=used,
                max_connections=maximum,
                files_bytes=files,
                files_count=count,
                disk_total_bytes=total,
                disk_free_bytes=free,
                disk_source=source,
            )
    except IntegrityError:  # another run got there first
        return None


def slope_per_day(points: list[tuple[datetime.date, int]]) -> int | None:
    """Least-squares growth in whole bytes per day through (day, value)
    points; None with fewer than MIN_POINTS days or no spread in days.
    Integer arithmetic throughout (rounded half away from zero)."""
    if len(points) < MIN_POINTS:
        return None
    first = min(d for d, _ in points)
    xs = [(d - first).days for d, _ in points]
    ys = [int(v) for _, v in points]
    n = len(points)
    sx, sy = sum(xs), sum(ys)
    sxx = sum(x * x for x in xs)
    sxy = sum(x * y for x, y in zip(xs, ys, strict=True))
    den = n * sxx - sx * sx
    if den == 0:
        return None
    num = n * sxy - sx * sy
    q, r = divmod(abs(num), den)
    if 2 * r >= den:
        q += 1
    return q if num >= 0 else -q


def days_until_full(points: list[tuple[datetime.date, int]], free_bytes: int) -> int | None:
    """At the fitted rate, whole days until ``free_bytes`` are used up;
    None when there is no estimate or use is not growing."""
    rate = slope_per_day(points)
    if rate is None or rate <= 0 or free_bytes is None:
        return None
    return max(0, free_bytes // rate)


def recent(days: int = TREND_DAYS, now=None) -> list:
    from core.models import GrowthSnapshot

    since = timezone.localdate(now or timezone.now()) - datetime.timedelta(days=days)
    return list(GrowthSnapshot.objects.filter(taken_on__gte=since).order_by("taken_on"))


def warn_days() -> int:
    try:
        from community.rules import rule

        value = rule(WARN_RULE)
        return int(value) if value not in (None, "") else WARN_DEFAULT_DAYS
    except Exception:  # an unreadable rule never hides the check
        return WARN_DEFAULT_DAYS


def estimate(rows=None) -> dict:
    """The figures for System status and the health check."""
    rows = recent() if rows is None else rows
    disk_rows = [r for r in rows if r.disk_total_bytes and r.disk_free_bytes is not None]
    points = [(r.taken_on, r.disk_total_bytes - r.disk_free_bytes) for r in disk_rows]
    latest = rows[-1] if rows else None
    free = disk_rows[-1].disk_free_bytes if disk_rows else None
    db_points = [(r.taken_on, r.database_bytes) for r in rows]
    return {
        "latest": latest,
        "days_left": days_until_full(points, free) if free is not None else None,
        "disk_per_day": slope_per_day(points),
        "db_per_day": slope_per_day(db_points),
        "points": len(rows),
        "warn_days": warn_days(),
    }


def sparkline(values: list[int], width: int = 240, height: int = 40) -> str:
    """SVG polyline points for a small trend line (no library): x across the
    width, y scaled between the smallest and largest value."""
    if len(values) < 2:
        return ""
    low, high = min(values), max(values)
    span = (high - low) or 1
    step = width / (len(values) - 1)
    pts = []
    for i, v in enumerate(values):
        y = height - 2 - (v - low) * (height - 4) / span
        pts.append(f"{i * step:.1f},{y:.1f}")
    return " ".join(pts)


def trend() -> dict:
    """System status: the 90-day lines and the estimate."""
    rows = recent()
    est = estimate(rows)
    disk_used = [
        r.disk_total_bytes - r.disk_free_bytes
        for r in rows
        if r.disk_total_bytes and r.disk_free_bytes is not None
    ]
    return {
        **est,
        "db_line": sparkline([r.database_bytes for r in rows]),
        "disk_line": sparkline(disk_used),
        "first_day": rows[0].taken_on if rows else None,
    }


# --- Health checks -----------------------------------------------------------------------


def growth_check():
    """Fails when, at this rate, the disk fills within the board's warning
    days. Not shown until a week of snapshots exists."""
    est = estimate()
    latest = est["latest"]
    if latest is None:
        return None
    days = est["days_left"]
    if days is None:
        if est["points"] < MIN_POINTS:
            return True, (
                f"Measuring: {est['points']} day(s) recorded, an estimate after {MIN_POINTS}"
            )
        return True, "Disk use is not growing"
    if days < est["warn_days"]:
        return False, (
            f"At this rate the server's disk is full in about {days} days (warning at "
            f"{est['warn_days']}). Free space on the server, or move uploads to R2, before then"
        )
    return True, f"At this rate the disk is full in about {days} days"


def connections_check():
    used, maximum = connections()
    percent = int(used * 100 / maximum) if maximum else 0
    detail = f"{used} of {maximum} in use ({percent}%)"
    if percent > CONNECTIONS_WARN_PERCENT:
        return False, (
            f"{detail}: close to the limit, so new pages may fail to open. Restart the web "
            "and worker containers; if it comes back, tell the portal's maintainer"
        )
    return True, detail
