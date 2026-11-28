"""The simulation's clock: time-machine moves Python's idea of "now".

``time_machine.travel`` patches ``datetime.now``, ``date.today``,
``time.time`` and friends at the C level, so Django's ``timezone.now()`` and
``timezone.localdate()`` follow it without any change to the portal. The
only clock it cannot move is Postgres's own ``now()``; the portal uses it in
no default, trigger or query (checked at 0.47.0: the one ``db_default=Now()``
has a Python ``default`` that wins), and Procrastinate's queue, which the
engine drains itself (``tasks.drain``), ignoring scheduled times.

Cron expressions are Procrastinate's (five fields, UTC). ``fire_times`` lists
when a periodic task fires during one UTC day; jobs that fire more often than
hourly are thinned to ``max_per_day`` evenly spread runs (an accelerated
simulation cannot afford 288 heartbeats a day, and the runs in between would
find nothing new to do: nothing else happens between them).
"""

from __future__ import annotations

import datetime
import zoneinfo

import time_machine

UTC = datetime.UTC


class Clock:
    def __init__(self, start: datetime.datetime):
        self._traveller = time_machine.travel(start, tick=False)
        self._shift = None
        self.now = start

    def __enter__(self):
        self._shift = self._traveller.start()
        return self

    def __exit__(self, *exc):
        self._traveller.stop()

    def move_to(self, when: datetime.datetime) -> None:
        if when < self.now:
            return  # time only moves forward
        self._shift.move_to(when)
        self.now = when


def _field(spec: str, low: int, high: int) -> set[int]:
    out: set[int] = set()
    for part in spec.split(","):
        step = 1
        if "/" in part:
            part, step_s = part.split("/", 1)
            step = int(step_s)
        if part in ("*", ""):
            a, b = low, high
        elif "-" in part:
            a_s, b_s = part.split("-", 1)
            a, b = int(a_s), int(b_s)
        else:
            a = int(part)
            b = high if step > 1 else a
        out.update(range(a, b + 1, step))
    return {v for v in out if low <= v <= high}


class Cron:
    def __init__(self, expr: str):
        minute, hour, dom, month, dow = expr.split()
        self.expr = expr
        self.minutes = sorted(_field(minute, 0, 59))
        self.hours = sorted(_field(hour, 0, 23))
        self.doms = _field(dom, 1, 31)
        self.months = _field(month, 1, 12)
        # cron: 0 and 7 are Sunday; Python: Monday is 0.
        self.dows = {(d - 1) % 7 for d in _field(dow, 0, 7)}
        self.any_dom, self.any_dow = dom == "*", dow == "*"

    def runs_on(self, day: datetime.date) -> bool:
        if day.month not in self.months:
            return False
        dom_ok, dow_ok = day.day in self.doms, day.weekday() in self.dows
        if self.any_dom and self.any_dow:
            return True
        if self.any_dom:
            return dow_ok
        if self.any_dow:
            return dom_ok
        return dom_ok or dow_ok  # cron's rule when both are restricted

    def per_day(self) -> int:
        return len(self.minutes) * len(self.hours)

    def fire_times(self, day: datetime.date, *, max_per_day: int = 24) -> list[datetime.datetime]:
        """The UTC times this cron fires on UTC ``day`` (thinned to ``max_per_day``)."""
        if not self.runs_on(day):
            return []
        times = [
            datetime.datetime(day.year, day.month, day.day, h, m, tzinfo=UTC)
            for h in self.hours
            for m in self.minutes
        ]
        if len(times) <= max_per_day:
            return times
        step = len(times) / max_per_day
        return [times[int(i * step)] for i in range(max_per_day)]


def local(day: datetime.date, hour: int, minute: int = 0, tz: str = "America/New_York"):
    """A wall-clock time on ``day`` in the community's time zone, as UTC."""
    return datetime.datetime(
        day.year, day.month, day.day, hour, minute, tzinfo=zoneinfo.ZoneInfo(tz)
    ).astimezone(UTC)
