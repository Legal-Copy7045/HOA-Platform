"""Owners' finance summary (D84, optional feature "owner_finance_dashboard").

With the feature on, the owners' Budget and reserves page (owner_budget)
shows just two things: operating vs reserve money (with the reserve against
the adopted plan's fully funded figure) and the budget against what actually
came in and went out this fiscal year, by budget line. Association totals
only: never an owner, a home, an account, a delinquency or a collection.
Both figures come through one adapter each, to switch at merge.
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
    """Switch to claude/books-and-close at merge: operating and reserve fund
    balances at the last closed month (``closed=True``). Interim: today's cash
    book (operating) and the reserve plan's balance, marked not closed."""
    from ledger import cashflow, reserve_plan

    today = today or timezone.localdate()
    opening = cashflow.opening_balances(today)
    snapshot = reserve_plan.latest_snapshot()
    target = snapshot.fully_funded_cents if snapshot else None
    percent = None
    if target:
        percent = max(0, opening.reserve) * 100 // target
    return Funds(
        operating=opening.operating,
        reserve=opening.reserve,
        as_of=opening.as_of,
        closed=opening.closed,
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

    def total(self, kind: str, attr: str) -> int:
        return sum(getattr(line, attr) for line in self.lines if line.kind == kind)


def budget_vs_actual_ytd(today: datetime.date | None = None) -> BudgetVsActual:
    """Switch to claude/books-and-close at merge: budget vs actual from the
    financial statements of closed months. Interim: the adopted budget's lines
    against ledger.services.budget_vs_actual, counting only the months of the
    fiscal year that have ended."""
    from ledger import services
    from ledger.models import Budget, BudgetStatus

    today = today or timezone.localdate()
    budget = Budget.objects.filter(status=BudgetStatus.ADOPTED).order_by("-fiscal_year").first()
    if budget is None:
        return BudgetVsActual(None, None, 0, ())
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


def context(request) -> dict:
    return {"funds": fund_balances(), "bva": budget_vs_actual_ytd()}
