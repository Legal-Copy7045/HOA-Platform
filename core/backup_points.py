"""The backup points a real restore can use (Backups -> Restore from a
backup, decision D98).

The web app never sees the server's disk. The VM agent writes what is there
into ``UpdateStatus.backup_points`` every minute (local backups: stamp, size,
the release that made it, whether it is a kept safety backup), and
``manage.py backup_inventory --offsite`` adds the complete backups in the
off-site bucket every 15 minutes and after each task. A backup point is named
``local:<stamp>`` or ``offsite:<stamp>``; the agent checks the chosen one
against the backups that really exist before anything runs, and the restore
script checks it again. Nothing here is ever a path.
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass

from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime

STAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{4}Z")
POINT_RE = re.compile(r"(local|offsite):(\d{4}-\d{2}-\d{2}T\d{4}Z)")
# backup.sh writes APP_VERSION as "<VERSION>-<short commit>", e.g. 0.44.0-be6a62d.
VERSION_RE = re.compile(r"(\d{1,6})\.(\d{1,6})\.(\d{1,6})(?:-[a-z0-9]{1,40})?")
# How old the agent's local list may be before the page says so.
LOCAL_STALE = datetime.timedelta(minutes=5)
MAX_POINTS = 60


@dataclass(frozen=True)
class BackupPoint:
    where: str  # "local" or "offsite"
    stamp: str
    size_kb: int | None
    version: str  # as backup.sh labelled it, cleaned; "" when unknown
    kept: bool = False  # a safety backup taken before a restore

    @property
    def ident(self) -> str:
        return f"{self.where}:{self.stamp}"

    @property
    def made_at(self) -> datetime.datetime | None:
        return stamp_time(self.stamp)

    @property
    def release(self) -> tuple[int, int, int] | None:
        return parse_release(self.version)

    @property
    def release_text(self) -> str:
        rel = self.release
        return ".".join(str(n) for n in rel) if rel else ""

    @property
    def size_text(self) -> str:
        if self.size_kb is None:
            return "size unknown"
        if self.size_kb >= 1024 * 1024:
            return f"{self.size_kb / 1024 / 1024:.1f} GB"
        if self.size_kb >= 1024:
            return f"{self.size_kb / 1024:.1f} MB"
        return f"{self.size_kb} KB"

    @property
    def refusal(self) -> str:
        """Why this point cannot be restored here, or ''."""
        rel = self.release
        if rel is None:
            return "It does not say which release made it, so it cannot be checked."
        installed = installed_release()
        if installed is not None and rel > installed:
            return (
                f"It was made by release {self.release_text}, newer than the installed "
                f"{'.'.join(map(str, installed))}: install that release first."
            )
        return ""

    @property
    def older(self) -> bool:
        installed = installed_release()
        rel = self.release
        return bool(rel and installed and rel < installed)


def stamp_time(stamp: str) -> datetime.datetime | None:
    if not STAMP_RE.fullmatch(stamp or ""):
        return None
    try:
        return datetime.datetime.strptime(stamp, "%Y-%m-%dT%H%MZ").replace(tzinfo=datetime.UTC)
    except ValueError:
        return None


def parse_release(text: str) -> tuple[int, int, int] | None:
    match = VERSION_RE.fullmatch(text or "")
    if not match:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def installed_release() -> tuple[int, int, int] | None:
    return parse_release(str(getattr(settings, "APP_RELEASE", "") or ""))


def _clean_version(value) -> str:
    text = re.sub(r"[^a-z0-9.-]", "", str(value or ""))[:48]
    return text if VERSION_RE.fullmatch(text) else ""


def _points(rows, where: str) -> list[BackupPoint]:
    out: dict[str, BackupPoint] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        stamp = str(row.get("stamp") or "")
        if not STAMP_RE.fullmatch(stamp) or stamp_time(stamp) is None:
            continue
        size = row.get("size_kb")
        size_kb = size if isinstance(size, int) and size >= 0 else None
        out[stamp] = BackupPoint(
            where, stamp, size_kb, _clean_version(row.get("version")), row.get("kept") is True
        )
    return sorted(out.values(), key=lambda p: p.stamp, reverse=True)[:MAX_POINTS]


def _stored() -> dict:
    from core.models import UpdateStatus

    value = UpdateStatus.objects.filter(pk=1).values_list("backup_points", flat=True).first()
    return value if isinstance(value, dict) else {}


def inventory() -> dict:
    """What the restore page shows: both lists (newest first) and when each
    was last read."""
    data = _stored()
    local_at = parse_datetime(str(data.get("at") or "")) if data.get("at") else None
    offsite_at = (
        parse_datetime(str(data.get("offsite_at") or "")) if data.get("offsite_at") else None
    )
    return {
        "local": _points(data.get("local"), "local"),
        "offsite": _points(data.get("offsite"), "offsite"),
        "local_at": local_at,
        "local_stale": local_at is None or local_at < timezone.now() - LOCAL_STALE,
        "offsite_at": offsite_at,
        "offsite_error": str(data.get("offsite_error") or "")[:120],
    }


def find(ident: str) -> BackupPoint | None:
    """The listed point with this identifier, or None (also for anything not
    shaped like ``local:<stamp>`` / ``offsite:<stamp>``)."""
    match = POINT_RE.fullmatch(ident or "")
    if not match:
        return None
    where, stamp = match.groups()
    for point in inventory()[where]:
        if point.stamp == stamp:
            return point
    return None


def label(ident: str) -> str:
    """A point as people read it: "Nov 26, 2026 18:30 UTC, on the server"."""
    match = POINT_RE.fullmatch(ident or "")
    if not match:
        return ident or ""
    where, stamp = match.groups()
    at = stamp_time(stamp)
    when = at.strftime("%b %-d, %Y %H:%M UTC") if at else stamp
    return f"{when}, {'on the server' if where == 'local' else 'off-site copy'}"


def save_offsite(rows: list[dict] | None, error: str = "") -> None:
    """Record the off-site list (``backup_inventory --offsite``). On an error
    the last good list stays, with the error's code shown beside it."""
    from core.models import UpdateStatus

    status = UpdateStatus.get()
    data = status.backup_points if isinstance(status.backup_points, dict) else {}
    if rows is not None:
        data["offsite"] = [
            {"stamp": p.stamp, "size_kb": p.size_kb, "version": p.version}
            for p in _points(rows, "offsite")
        ]
        data["offsite_at"] = timezone.now().isoformat()
    data["offsite_error"] = error[:120]
    status.backup_points = data
    status.save(update_fields=["backup_points"])


def known_offsite_versions() -> dict[str, str]:
    return {p.stamp: p.version for p in inventory()["offsite"] if p.version}
