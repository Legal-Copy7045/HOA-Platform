"""Cash-flow forecast (D81, optional feature "cashflow_forecast").

The next 12 months by month and by fund (operating, reserve). ``project`` is
pure integer-cents arithmetic on ``Inputs`` (unit tested); ``build`` gathers
the inputs from what the portal holds through small adapters:

* opening cash: ``opening_balances`` (switch to claude/books-and-close fund
  balances at merge; interim: the cash book for operating, the reserve
  plan's balance for reserve);
* assessments expected: the monthly {term} x homes billed, times the
  collection rate (rule ``cashflow.collection_rate_percent``, or worked out
  from the last 12 months);
* special assessment installments: ``special_installments`` (switch to
  claude/finance-gaps at merge; interim none);
* interest on overdue balances (D80): the last three months' average;
* operating outflows: approved invoices by due date and active contracts'
  amounts by their schedule;
* the reserve contribution moved from operating to reserve each month, the
  reserve plan's interest, and the reserve plan's replacements of each year
  spread over its months;
* warnings: operating closing cash below rule ``cashflow.min_operating_cents``,
  or reserve below the plan's path for that year.

Scenario changes (collection rate, extra monthly spending, a one-off cost)
come from the page's query string and are never saved.

Rounding: each money figure is rounded half up to the cent where it is made
(collections, interest, spreads use largest remainder so a year's total is
exact).
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field, replace
from fractions import Fraction

from django.db.models import Sum
from django.utils import timezone

from audit import actions as audit_actions

FEATURE = "cashflow_forecast"
MONTHS = 12

audit_actions.register(
    audit_actions.ActionSpec(
        "cashflow.exported", "Cash-flow forecast downloaded", frozenset({"format", "scenario"})
    )
)


def round_half_up(value: Fraction) -> int:
    if value < 0:
        return -round_half_up(-value)
    return int((value.numerator * 2 + value.denominator) // (2 * value.denominator))


def spread(total: int, parts: int) -> list[int]:
    """``total`` cents over ``parts`` months, the remainder in the first ones."""
    if parts <= 0:
        return []
    base, extra = divmod(total, parts)
    return [base + (1 if i < extra else 0) for i in range(parts)]


def add_months(day: datetime.date, n: int) -> datetime.date:
    index = day.year * 12 + day.month - 1 + n
    return datetime.date(index // 12, index % 12 + 1, 1)


# --- Pure calculation -----------------------------------------------------------------


@dataclass(frozen=True)
class Inputs:
    start: datetime.date  # first day of the first month
    opening_operating: int
    opening_reserve: int
    billed: tuple[int, ...]  # assessments billed each month
    collection_rate_bp: int  # 10000 = everything billed is collected
    special: tuple[int, ...] = ()  # special assessment installments (to reserve)
    interest_income: tuple[int, ...] = ()  # owner interest collected (operating)
    operating_out: tuple[int, ...] = ()
    reserve_out: tuple[int, ...] = ()
    reserve_transfer: tuple[int, ...] = ()  # operating -> reserve
    reserve_interest_bp: int = 0  # yearly, on the month's opening reserve
    # The reserve plan's interest by month, when known (used instead of the
    # rate, so the forecast follows the plan's own arithmetic).
    reserve_interest: tuple[int, ...] = ()
    min_operating: int = 0
    reserve_target: tuple[int | None, ...] = ()
    months: int = MONTHS

    def at(self, values: tuple, i: int, default=0):
        return values[i] if i < len(values) else default


@dataclass(frozen=True)
class Row:
    start: datetime.date
    operating_open: int
    collections: int
    interest_income: int
    operating_out: int
    transfer: int
    operating_close: int
    reserve_open: int
    special: int
    reserve_interest: int
    reserve_out: int
    reserve_close: int
    reserve_target: int | None
    operating_low: bool
    reserve_low: bool

    @property
    def warning(self) -> bool:
        return self.operating_low or self.reserve_low


@dataclass(frozen=True)
class Forecast:
    inputs: Inputs
    rows: tuple[Row, ...]
    notes: tuple[str, ...] = field(default=())

    @property
    def warnings(self) -> list[Row]:
        return [r for r in self.rows if r.warning]


def project(inputs: Inputs) -> list[Row]:
    if not 0 <= inputs.collection_rate_bp <= 10000:
        raise ValueError("The collection rate is between 0 and 100%.")
    rows = []
    op, res = inputs.opening_operating, inputs.opening_reserve
    for i in range(inputs.months):
        start = add_months(inputs.start, i)
        collected = round_half_up(
            Fraction(inputs.at(inputs.billed, i) * inputs.collection_rate_bp, 10000)
        )
        interest_in = inputs.at(inputs.interest_income, i)
        out = inputs.at(inputs.operating_out, i)
        transfer = inputs.at(inputs.reserve_transfer, i)
        op_close = op + collected + interest_in - out - transfer
        special = inputs.at(inputs.special, i)
        if inputs.reserve_interest:
            res_interest = inputs.at(inputs.reserve_interest, i)
        elif res > 0:
            res_interest = round_half_up(Fraction(res * inputs.reserve_interest_bp, 10000 * 12))
        else:
            res_interest = 0
        res_out = inputs.at(inputs.reserve_out, i)
        res_close = res + transfer + special + res_interest - res_out
        target = inputs.at(inputs.reserve_target, i, None)
        rows.append(
            Row(
                start=start,
                operating_open=op,
                collections=collected,
                interest_income=interest_in,
                operating_out=out,
                transfer=transfer,
                operating_close=op_close,
                reserve_open=res,
                special=special,
                reserve_interest=res_interest,
                reserve_out=res_out,
                reserve_close=res_close,
                reserve_target=target,
                operating_low=op_close < inputs.min_operating,
                reserve_low=target is not None and res_close < target,
            )
        )
        op, res = op_close, res_close
    return rows


@dataclass(frozen=True)
class Scenario:
    """What-if changes from the page, never saved."""

    rate_change_pct: int = 0  # e.g. -10: ten points less collected
    extra_monthly_cents: int = 0  # more operating spending every month
    one_off_cents: int = 0  # a one-off operating cost in the first month

    @property
    def active(self) -> bool:
        return bool(self.rate_change_pct or self.extra_monthly_cents or self.one_off_cents)

    def describe(self) -> str:
        parts = []
        if self.rate_change_pct:
            parts.append(f"rate{self.rate_change_pct:+d}")
        if self.extra_monthly_cents:
            parts.append("extra-monthly")
        if self.one_off_cents:
            parts.append("one-off")
        return ",".join(parts) or "none"


def apply_scenario(inputs: Inputs, scenario: Scenario) -> Inputs:
    rate = min(max(inputs.collection_rate_bp + scenario.rate_change_pct * 100, 0), 10000)
    out = [inputs.at(inputs.operating_out, i) for i in range(inputs.months)]
    out = [v + scenario.extra_monthly_cents for v in out]
    if out:
        out[0] += scenario.one_off_cents
    return replace(inputs, collection_rate_bp=rate, operating_out=tuple(out))


# --- Adapters (switch at merge) ---------------------------------------------------------


@dataclass(frozen=True)
class Opening:
    operating: int
    reserve: int
    as_of: datetime.date
    closed: bool  # from a closed month (books branch) or today's open figures
    source: str


def opening_balances(today: datetime.date) -> Opening:
    """Switch to claude/books-and-close at merge: the operating and reserve fund
    balances at the last closed month. Interim, today's open figures: for
    operating, the cash book (ledger.accounting.book_balance) plus owner
    payments received and not yet deposited; for reserve, the reserve plan's
    own path (its starting balance moved by the plan's contribution, interest
    and replacements month by month to the end of this month), or its starting
    balance when the plan is not in use."""
    from ledger import accounting, reserve_plan

    undeposited = -sum(e.amount_cents for e in accounting.undeposited_payments())
    operating = accounting.book_balance(today) + undeposited
    reserve = reserve_plan.starting_balance().cents
    source = "cash book and reserve plan"
    rows = _plan_rows()
    row = rows.get(today.year) if rows else None
    if row is not None:
        reserve = row.begin_cents + sum(
            sum(part[m] for part in _plan_parts(row)) for m in range(today.month)
        )
    return Opening(operating, reserve, today, False, source)


def _plan_rows() -> dict:
    """The reserve plan's projection by year, while the reserve plan is in use."""
    from core import features
    from ledger import reserve_plan

    if not features.enabled("reserves"):
        return {}
    projection, _balance = reserve_plan.current_projection()
    return {r.year: r for r in projection.rows}


def _plan_parts(row) -> tuple[list[int], list[int], list[int]]:
    """One plan year by month: contribution, interest, minus replacements."""
    return (
        spread(row.contribution_cents, 12),
        spread(row.interest_cents, 12),
        [-v for v in spread(row.expense_cents, 12)],
    )


def special_installments(months: list[datetime.date]) -> list[int]:
    """Switch to claude/finance-gaps at merge: special assessment installments
    due in each month. Interim: none recorded on this base."""
    return [0 for _ in months]


# --- Gathering the inputs ----------------------------------------------------------------


def collection_rate_bp(today: datetime.date) -> tuple[int, str]:
    """The rule's rate, or the last 12 months' payments over assessments billed."""
    from community.rules import rule
    from core.scoping import system_access
    from ledger.models import EntryKind, LedgerEntry

    typed = rule("cashflow.collection_rate_percent")
    if typed is not None:
        return int(typed) * 100, "rule"
    since = today - datetime.timedelta(days=365)
    with system_access("cash-flow collection rate"):
        window = LedgerEntry.objects.filter(posted_on__gt=since, posted_on__lte=today)
        billed = window.filter(kind=EntryKind.ASSESSMENT).aggregate(s=Sum("amount_cents"))["s"] or 0
        paid = -(window.filter(kind=EntryKind.PAYMENT).aggregate(s=Sum("amount_cents"))["s"] or 0)
    if billed <= 0:
        return 10000, "none billed yet"
    return min(round_half_up(Fraction(paid * 10000, billed)), 10000), "last 12 months"


def _billed_per_month() -> int:
    from community.models import Account
    from community.rules import rule
    from core.scoping import system_access

    monthly = rule("dues.monthly_cents") or 0
    with system_access("cash-flow homes billed"):
        with_owner = (
            Account.objects.filter(closed_on__isnull=True).values("lot_id").distinct().count()
        )
    return monthly * with_owner


def _interest_income(today: datetime.date) -> int:
    from core import features
    from core.scoping import system_access
    from ledger.models import EntryKind, LedgerEntry

    if not features.enabled("overdue_interest"):
        return 0
    since = add_months(today.replace(day=1), -3)
    with system_access("cash-flow interest"):
        total = (
            LedgerEntry.objects.filter(
                kind=EntryKind.INTEREST,
                posted_on__gte=since,
                posted_on__lt=today.replace(day=1),
                reversed_by__isnull=True,
            ).aggregate(s=Sum("amount_cents"))["s"]
            or 0
        )
    return total // 3


def _operating_out(months: list[datetime.date]) -> list[int]:
    from operations.models import Contract, Invoice

    out = [0] * len(months)
    first, end = months[0], add_months(months[-1], 1)
    for inv in Invoice.objects.filter(state__in=["approved", "pending_approval"]):
        due = inv.due_date or inv.invoice_date
        if due >= end:
            continue
        i = 0 if due < first else (due.year - first.year) * 12 + due.month - first.month
        out[i] += inv.amount_cents
    for contract in Contract.objects.filter(state="active"):
        step = {"monthly": 1, "quarterly": 3, "annual": 12}.get(contract.schedule, 1)
        for i, month in enumerate(months):
            last_day = add_months(month, 1) - datetime.timedelta(days=1)
            if not (contract.starts_on <= last_day and (contract.ends_on or last_day) >= month):
                continue
            offset = (month.year - contract.starts_on.year) * 12 + (
                month.month - contract.starts_on.month
            )
            if offset % step == 0:
                out[i] += contract.amount_cap_cents
    return out


def _reserve_plan_parts(months: list[datetime.date]):
    """(transfer, interest, reserve_out, targets) by month from the reserve
    plan: its contribution moves from operating to reserve, its interest and
    replacements spread evenly over each year, and the target is the lower of
    the plan's balances at the start and end of that year."""
    n = len(months)
    by_year = _plan_rows()
    transfer, interest, out, targets = [0] * n, [0] * n, [0] * n, [None] * n
    for i, month in enumerate(months):
        row = by_year.get(month.year)
        if row is None:
            continue
        contribution, earned, spent = _plan_parts(row)
        transfer[i] = contribution[month.month - 1]
        interest[i] = earned[month.month - 1]
        out[i] = -spent[month.month - 1]
        targets[i] = min(row.begin_cents, row.end_cents)
    return transfer, interest, out, targets


def build(today: datetime.date | None = None, scenario: Scenario | None = None) -> Forecast:
    from community.rules import rule

    today = today or timezone.localdate()
    start = add_months(today.replace(day=1), 1)
    months = [add_months(start, i) for i in range(MONTHS)]
    opening = opening_balances(today)
    rate, rate_source = collection_rate_bp(today)
    transfer, reserve_interest, reserve_out, targets = _reserve_plan_parts(months)
    billed = _billed_per_month()
    interest = _interest_income(today)
    inputs = Inputs(
        start=start,
        opening_operating=opening.operating,
        opening_reserve=opening.reserve,
        billed=tuple([billed] * MONTHS),
        collection_rate_bp=rate,
        special=tuple(special_installments(months)),
        interest_income=tuple([interest] * MONTHS),
        operating_out=tuple(_operating_out(months)),
        reserve_out=tuple(reserve_out),
        reserve_transfer=tuple(transfer),
        reserve_interest=tuple(reserve_interest),
        min_operating=rule("cashflow.min_operating_cents") or 0,
        reserve_target=tuple(targets),
    )
    if scenario is not None and scenario.active:
        inputs = apply_scenario(inputs, scenario)
    notes = (
        f"Opening cash from the {opening.source} on {opening.as_of:%B} {opening.as_of.day}, "
        f"{opening.as_of.year} (not a closed month).",
        f"Collection rate {inputs.collection_rate_bp / 100:g}% ({rate_source}).",
    )
    return Forecast(inputs, tuple(project(inputs)), notes)


# --- Warnings for My items (computed at most every 10 minutes) ------------------------------

_cache: dict = {}


def warning_months(today: datetime.date | None = None) -> list[Row]:
    import time

    today = today or timezone.localdate()
    key = today.isoformat()
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < 600:
        return hit[1]
    rows = build(today).warnings
    _cache.clear()
    _cache[key] = (time.monotonic(), rows)
    return rows


def my_items(user) -> list:
    from django.urls import reverse

    from core import features
    from core.money import format_cents
    from core.my_items import Item
    from core.policy import can

    if not (features.enabled(FEATURE) and can(user, "finance.view")):
        return []
    rows = warning_months()
    if not rows:
        return []
    first = rows[0]
    what = (
        f"operating cash {format_cents(first.operating_close)}"
        if first.operating_low
        else f"reserve {format_cents(first.reserve_close)} below the plan"
    )
    return [
        Item(
            "Finance",
            f"Cash-flow forecast: low in {first.start:%B %Y}",
            f"{what}; {len(rows)} month{'s' if len(rows) != 1 else ''} flagged",
            reverse("cashflow"),
            "warning",
            nav="finance_more",
        )
    ]


# --- Exports ----------------------------------------------------------------------------------

HEADER = [
    "Month",
    "Operating opening",
    "Assessments collected",
    "Interest collected",
    "Operating spending",
    "To reserve",
    "Operating closing",
    "Reserve opening",
    "Special assessments",
    "Reserve interest",
    "Reserve spending",
    "Reserve closing",
    "Reserve plan path",
    "Warning",
]


def _dollars(cents: int | None) -> str:
    if cents is None:
        return ""
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}{cents // 100}.{cents % 100:02d}"


def table_rows(forecast: Forecast) -> list[list[str]]:
    out = []
    for r in forecast.rows:
        warn = []
        if r.operating_low:
            warn.append("operating low")
        if r.reserve_low:
            warn.append("reserve below plan")
        out.append(
            [
                f"{r.start:%Y-%m}",
                *(
                    _dollars(v)
                    for v in (
                        r.operating_open,
                        r.collections,
                        r.interest_income,
                        r.operating_out,
                        r.transfer,
                        r.operating_close,
                        r.reserve_open,
                        r.special,
                        r.reserve_interest,
                        r.reserve_out,
                        r.reserve_close,
                        r.reserve_target,
                    )
                ),
                "; ".join(warn),
            ]
        )
    return out


def pdf(forecast: Forecast, *, title: str) -> bytes:
    from core import pdf as pdf_

    paragraphs = [("title", title)]
    paragraphs += [("small", note) for note in forecast.notes]
    paragraphs.append(("gap", ""))
    paragraphs.append(
        ("rowhead", f"{'Month':<8}{'Op. close':>14}{'Reserve close':>16}{'Plan path':>14}  Flag")
    )
    for r in forecast.rows:
        flag = ("OP LOW " if r.operating_low else "") + ("RES LOW" if r.reserve_low else "")
        paragraphs.append(
            (
                "row",
                f"{r.start:%Y-%m}  {_dollars(r.operating_close):>14}"
                f"{_dollars(r.reserve_close):>16}{_dollars(r.reserve_target):>14}  {flag}",
            )
        )
    return pdf_.render(paragraphs, title=title)
