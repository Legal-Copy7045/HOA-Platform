"""Owners' finance summary (D84, optional feature "owner_finance_dashboard").

With the feature on, the owners' Budget and reserves page (owner_budget)
shows just two things: operating vs reserve money (with the reserve against
the adopted plan's fully funded figure) and the budget against what actually
came in and went out this fiscal year, by budget line. Association totals
only: never an owner, a home, an account, a delinquency or a collection.
Both figures come from the books (D69-D71) for closed months; while the
books are not in use or nothing is closed yet, from today's figures,
marked not closed.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from django.utils import timezone

FEATURE = "owner_finance_dashboard"


@dataclass(frozen=True)
class Funds:
    operating: int
    reserve: int
    as_of: datetime.date
    closed: bool  # True: figures of a closed month; False: today's, not closed
    reserve_target: int | None  # the adopted plan's fully funded balance
    percent_funded: int | None
    plan_adopted_on: datetime.date | None


def fund_balances(today: datetime.date | None = None) -> Funds:
    """Operating and reserve fund balances in the books at the end of the last
    closed month (``closed=True``, ``ledger.books.close.closed_fund_positions``).
    While the books are not in use or no month is closed: today's cash book
    (operating) and the reserve plan's balance, marked not closed."""
    from ledger import cashflow, reserve_plan
    from ledger.books import close

    today = today or timezone.localdate()
    closed = close.closed_fund_positions()
    if closed is not None:
        as_of, positions = closed
        operating = positions["operating"]["balance"]
        reserve = positions["reserve"]["balance"]
        is_closed = True
    else:
        opening = cashflow.opening_balances(today)
        operating, reserve, as_of, is_closed = (
            opening.operating,
            opening.reserve,
            opening.as_of,
            opening.closed,
        )
    snapshot = reserve_plan.latest_snapshot()
    target = snapshot.fully_funded_cents if snapshot else None
    percent = None
    if target:
        percent = max(0, reserve) * 100 // target
    return Funds(
        operating=operating,
        reserve=reserve,
        as_of=as_of,
        closed=is_closed,
        reserve_target=target,
        percent_funded=percent,
        plan_adopted_on=snapshot.adopted_on if snapshot else None,
    )


@dataclass(frozen=True)
class Line:
    name: str
    kind: str  # income / expense
    budget_ytd: int
    actual_ytd: int
    annual: int

    @property
    def percent(self) -> int:
        """Actual as a share of the year-to-date budget, for the bar (0-150)."""
        if self.budget_ytd <= 0:
            return 0
        return min(150, max(0, self.actual_ytd * 100 // self.budget_ytd))


@dataclass(frozen=True)
class BudgetVsActual:
    fiscal_year: int | None
    through: datetime.date | None  # last month counted
    closed_months: int
    lines: tuple[Line, ...]
    closed: bool = False  # True: from the books' closed months

    def total(self, kind: str, attr: str) -> int:
        return sum(getattr(line, attr) for line in self.lines if line.kind == kind)


def budget_vs_actual_ytd(today: datetime.date | None = None) -> BudgetVsActual:
    """Budget vs actual for the adopted budget's year. From the books'
    statements (``ledger.books.statements.budget_vs_actual``) through the last
    closed month of that year (``closed=True``); while the books are not in
    use or none of its months is closed, the adopted budget's lines against
    ledger.services.budget_vs_actual, counting only the months of the fiscal
    year that have ended (not closed)."""
    from ledger import services
    from ledger.models import Budget, BudgetStatus

    today = today or timezone.localdate()
    budget = Budget.objects.filter(status=BudgetStatus.ADOPTED).order_by("-fiscal_year").first()
    if budget is None:
        return BudgetVsActual(None, None, 0, ())
    from_books = _books_budget_vs_actual(budget)
    if from_books is not None:
        return from_books
    data = services.budget_vs_actual(budget)
    done = [
        i
        for i, (y, m) in enumerate(data["months"])
        if datetime.date(y, m, 1) < today.replace(day=1)
    ]
    lines = []
    for row in data["rows"]:
        line = row["line"]
        lines.append(
            Line(
                name=line.name,
                kind=line.kind,
                budget_ytd=sum(row["planned"][i] for i in done),
                actual_ytd=sum(row["actual"][i] for i in done),
                annual=row["planned_total"],
            )
        )
    through = None
    if done:
        y, m = data["months"][done[-1]]
        through = datetime.date(y, m, 1)
    return BudgetVsActual(budget.fiscal_year, through, len(done), tuple(lines))


def _books_budget_vs_actual(budget) -> BudgetVsActual | None:
    """The books' statements through the last closed month of the budget's
    fiscal year, or None when the books are not in use or none is closed."""
    from ledger.books import close, periods, statements

    if not close.books_in_use():
        return None
    closed = periods.standing_closes()
    # The run of closed months from the start of the year.
    done = []
    for k in periods.year_months(budget.fiscal_year):
        if k not in closed:
            break
        done.append(k)
    if not done:
        return None
    _start, through_end = periods.month_bounds(done[-1])
    data = statements.budget_vs_actual(budget.fiscal_year, through=through_end)
    n = len(done)
    # statements.budget_vs_actual lists the budget's lines in this order.
    budget_lines = list(budget.lines.select_related("category"))
    lines = tuple(
        Line(
            name=row["name"],
            kind=row["kind"],
            budget_ytd=sum(row["planned"][:n]),
            actual_ytd=sum(row["actual"][:n]),
            annual=line.annual_cents,
        )
        for row, line in zip(data["rows"], budget_lines, strict=True)
    )
    return BudgetVsActual(budget.fiscal_year, through_end.replace(day=1), n, lines, closed=True)


def context(request) -> dict:
    return {"funds": fund_balances(), "bva": budget_vs_actual_ytd()}
