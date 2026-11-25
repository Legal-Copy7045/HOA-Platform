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


def short_date(value, today: datetime.date | None = None) -> str:
    """A short date: "Nov 3" in the current month, "Nov 3, 2027" otherwise,
    so a due date near the year boundary is never ambiguous (UX review)."""
    if value is None or value == "":
        return ""
    if isinstance(value, datetime.datetime):
        from django.utils import timezone

        value = timezone.localtime(value).date() if timezone.is_aware(value) else value.date()
    if today is None:
        from django.utils import timezone

        today = timezone.localdate()
    text = f"{value:%b} {value.day}"
    if (value.year, value.month) != (today.year, today.month):
        text += f", {value.year}"
    return text
