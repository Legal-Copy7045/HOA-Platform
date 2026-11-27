"""Closed periods (D70): which months and fiscal years are closed, the lock
on posting into them, closing, and the two-person reopen.

A month is closed when its latest ``PeriodClose`` has no approved reopen.
Nothing may be posted, backdated or moved into a closed month: services
refuse (``refuse_closed``), automatic corrections post in the open month
(``open_date``), and a database trigger refuses any row dated in a closed
month on the money tables (ledger entries, GL entries, deposits, owner
checks, vendor payments) as a backstop.
"""

from __future__ import annotations

import calendar
import datetime

from django.db import transaction
from django.utils import timezone

from audit.services import record_event
from core.policy import SYSTEM, require
from ledger.books.models import (
    CloseHow,
    PeriodClose,
    PeriodKind,
    PeriodReopenDecision,
    PeriodReopenRequest,
)


class PeriodError(ValueError):
    """A refusal with a message safe to show."""


def _user(actor):
    return None if actor is SYSTEM else actor


# --- Months and fiscal years --------------------------------------------------------------


def month_key(day: datetime.date) -> str:
    return f"{day:%Y-%m}"


def month_bounds(key: str) -> tuple[datetime.date, datetime.date]:
    try:
        year, month = (int(x) for x in key.split("-"))
        start = datetime.date(year, month, 1)
    except (ValueError, TypeError) as exc:
        raise PeriodError("Choose a month.") from exc
    return start, start.replace(day=calendar.monthrange(year, month)[1])


def next_month(day: datetime.date) -> datetime.date:
    return (day.replace(day=28) + datetime.timedelta(days=4)).replace(day=1)


def months_between(first: datetime.date, last: datetime.date) -> list[str]:
    out, day = [], first.replace(day=1)
    while day <= last:
        out.append(month_key(day))
        day = next_month(day)
    return out


def fiscal_start_month() -> int:
    from community.models import CommunityProfile

    return CommunityProfile.get().fiscal_year_start_month or 1


def fiscal_year_of(day: datetime.date) -> int:
    start = fiscal_start_month()
    return day.year if day.month >= start else day.year - 1


def year_key(fiscal_year: int) -> str:
    return f"FY{fiscal_year}"


def year_bounds(fiscal_year: int) -> tuple[datetime.date, datetime.date]:
    start = datetime.date(fiscal_year, fiscal_start_month(), 1)
    return start, start.replace(year=start.year + 1) - datetime.timedelta(days=1)


def year_months(fiscal_year: int) -> list[str]:
    start, end = year_bounds(fiscal_year)
    return months_between(start, end)


# --- What is closed -----------------------------------------------------------------------


def _approved_reopens() -> set[int]:
    return set(
        PeriodReopenDecision.objects.filter(approved=True).values_list(
            "request__close_id", flat=True
        )
    )


def standing_closes(kind: str = PeriodKind.MONTH) -> dict[str, PeriodClose]:
    """period key -> its close, for every period closed now."""
    latest: dict[str, PeriodClose] = {}
    for close in PeriodClose.objects.filter(kind=kind).order_by("id"):
        latest[close.period] = close
    reopened = _approved_reopens()
    return {k: c for k, c in latest.items() if c.pk not in reopened}


def is_closed_period(key: str, kind: str = PeriodKind.MONTH) -> bool:
    return key in standing_closes(kind)


def is_closed(day: datetime.date) -> bool:
    """Is ``day`` in a closed month?"""
    if day is None:
        return False
    return month_key(day) in standing_closes()


def any_closed_after(day: datetime.date) -> bool:
    return any(c.ends_on > day for c in standing_closes().values())


def last_closed_month() -> str | None:
    keys = sorted(standing_closes())
    return keys[-1] if keys else None


def refuse_closed(day, *, error=PeriodError, what: str = "This date") -> None:
    """Services call this before posting anything dated ``day``."""
    if day is not None and is_closed(day):
        raise error(
            f"{what} ({day:%B %-d, %Y}) is in a closed month ({day:%B %Y}). Post it in the "
            "open month with a reference to the original date, or ask the board to reopen "
            "the month."
        )


def open_date(day: datetime.date) -> datetime.date:
    """For automatic corrections (a Stripe payment confirmed late, entries
    moved after a backdated sale): the day itself when its month is open,
    else today (always in an open month: months close only after they end)."""
    if day is not None and is_closed(day):
        return timezone.localdate()
    return day


# --- Closing ------------------------------------------------------------------------------


def close_period(
    *,
    kind: str,
    key: str,
    summary: dict,
    how: str,
    actor,
    request=None,
) -> PeriodClose:
    """Record the close (the checks were run by the caller, ledger.books.close)."""
    if actor is not SYSTEM:
        require(actor, "books.close")
    if kind == PeriodKind.MONTH:
        starts_on, ends_on = month_bounds(key)
    else:
        starts_on, ends_on = year_bounds(int(key[2:]))
    if ends_on >= timezone.localdate():
        raise PeriodError("A period can close only after it has ended.")
    with transaction.atomic():
        from ledger.models import AccountingSetup

        AccountingSetup.objects.select_for_update().get(pk=AccountingSetup.get().pk)
        if is_closed_period(key, kind):
            raise PeriodError("This period is already closed.")
        close = PeriodClose.objects.create(
            kind=kind,
            period=key,
            starts_on=starts_on,
            ends_on=ends_on,
            how=how,
            closed_by=_user(actor),
            summary=summary,
        )
    record_event(
        "books.period_closed",
        actor=_user(actor),
        request=request,
        target=close,
        metadata={"period": key, "kind": kind, "how": how},
    )
    return close


# --- Reopening (two board members) ---------------------------------------------------------


def open_request(close: PeriodClose) -> PeriodReopenRequest | None:
    return (
        PeriodReopenRequest.objects.filter(close=close, decision__isnull=True)
        .order_by("-id")
        .first()
    )


def pending_reopens():
    return list(
        PeriodReopenRequest.objects.filter(decision__isnull=True)
        .select_related("close", "requested_by")
        .order_by("requested_at")
    )


def request_reopen(close: PeriodClose, *, reason: str, actor, request=None) -> PeriodReopenRequest:
    require(actor, "books.reopen_request")
    reason = (reason or "").strip()
    if len(reason) < 5:
        raise PeriodError("Say why the period must be reopened.")
    standing = standing_closes(close.kind)
    if standing.get(close.period) is None or standing[close.period].pk != close.pk:
        raise PeriodError("This period is not closed.")
    if close.kind == PeriodKind.MONTH:
        fy = fiscal_year_of(close.starts_on)
        if is_closed_period(year_key(fy), PeriodKind.YEAR):
            raise PeriodError(f"The fiscal year {fy} is closed: reopen the year first.")
        later = [k for k in standing if k > close.period]
        if later:
            raise PeriodError(
                f"Later months are closed ({', '.join(sorted(later))}): reopen the latest first."
            )
    with transaction.atomic():
        PeriodClose.objects.select_for_update().filter(pk=close.pk).exists()
        if open_request(close) is not None:
            raise PeriodError("A reopen request for this period is already waiting.")
        req = PeriodReopenRequest.objects.create(
            close=close, reason=reason[:2000], requested_by=actor
        )
    record_event(
        "books.reopen_requested",
        actor=actor,
        request=request,
        target=req,
        metadata={"period": close.period, "kind": close.kind},
    )
    return req


def decide_reopen(
    req: PeriodReopenRequest, *, approve: bool, note: str = "", actor, request=None
) -> PeriodReopenDecision:
    """A board member other than the one who asked approves or declines."""
    require(actor, "books.reopen_approve")
    if req.requested_by_id == actor.pk:
        raise PeriodError("Another board member must decide a reopen you asked for.")
    note = (note or "").strip()
    if not approve and len(note) < 3:
        raise PeriodError("Say why the reopen is declined.")
    with transaction.atomic():
        req = PeriodReopenRequest.objects.select_for_update().get(pk=req.pk)
        if PeriodReopenDecision.objects.filter(request=req).exists():
            raise PeriodError("This reopen request is already decided.")
        decision = PeriodReopenDecision.objects.create(
            request=req, approved=bool(approve), note=note[:2000], decided_by=actor
        )
    record_event(
        "books.reopen_approved" if approve else "books.reopen_declined",
        actor=actor,
        request=request,
        target=req,
        metadata={"period": req.close.period, "kind": req.close.kind},
    )
    return decision


def history(key: str, kind: str = PeriodKind.MONTH) -> list[dict]:
    """Closes and reopens of one period, oldest first, for its page."""
    out = []
    for close in PeriodClose.objects.filter(kind=kind, period=key).order_by("id"):
        out.append({"what": "closed", "close": close})
        for req in close.reopen_requests.select_related("requested_by").order_by("id"):
            out.append(
                {"what": "reopen", "request": req, "decision": getattr(req, "decision", None)}
            )
    return out


__all__ = [
    "CloseHow",
    "PeriodError",
    "close_period",
    "decide_reopen",
    "is_closed",
    "open_date",
    "refuse_closed",
    "request_reopen",
]
