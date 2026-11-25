"""Date helpers."""

from __future__ import annotations

import datetime


def add_business_days(start: datetime.date, days: int) -> datetime.date:
    """``days`` working days (Monday to Friday) after ``start``. Public
    holidays are not counted out; targets are guidance, not legal deadlines."""
    current = start
    remaining = max(0, int(days))
    while remaining:
        current += datetime.timedelta(days=1)
        if current.weekday() < 5:
            remaining -= 1
    return current
