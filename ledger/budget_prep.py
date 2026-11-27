"""Budget preparation (D78): "Start next year's budget".

What already existed and is reused, not duplicated: budgets with income and
expense lines and their budget-versus-actual report, the proposed and
adopted owner notices with paper copies, adoption (ledger/services.py), and
the adopted reserve plan (ledger/reserve_plan.py). What this adds:

* ``prepare_next``: a draft budget for the next fiscal year, pre-filled from
  this year's lines and actuals by expense category (approved and paid
  invoices dated in the fiscal year, the same source as the budget report):
  each line from this year's budget, its actual, or the higher of the two,
  plus an optional increase (whole hundredths of a percent, rounded half
  up to the cent). Expense categories spent in this year without a line get
  one. The reserve contribution line comes from the adopted reserve plan
  when there is one. Other income lines are carried over; the {term} income
  line is the per-home amount below times the homes and twelve months.
* ``per_home``: the monthly {term} per home: the net need (expenses less
  other income) divided by the homes (equal per lot, the Declaration's
  allocation, 68 Pa.C.S. § 5314) and by twelve, rounded up to a multiple of
  rule ``budget.assessment_rounding_cents`` (with its citation), and the
  change from today's monthly {term} (rule ``dues.monthly_cents``).
* ``comparison``: this year's budget, this year's actuals and next year's
  draft side by side, by line.

Integer cents throughout (CLAUDE.md rule 7).
"""

from __future__ import annotations

from django.db import transaction

import ledger.finance_gaps_audit  # noqa: F401  (registers the audit actions)
from audit.services import record_event
from community.models import Lot
from community.rules import rule, rule_with_citation
from community.terms import term_text
from core.policy import require
from core.scoping import system_access
from ledger import services
from ledger.models import Budget, BudgetLine, EntryKind, IncomeSource, LedgerEntry, LineKind

BASES = {
    "budget": "This year's budget",
    "actual": "This year's actual spending",
    "higher": "The higher of the two",
}
ROUNDING_RULE = "budget.assessment_rounding_cents"
RESERVE_CODE = "RESERVE"
RESERVE_NAME = "Reserve contribution"
MAX_INCREASE_BP = 5000  # 50%: anything above is a typing mistake


class BudgetPrepError(ValueError):
    """A step the service refuses (shown on the page)."""


def grow(cents: int, increase_bp: int) -> int:
    """``cents`` increased by ``increase_bp`` hundredths of a percent, to the
    nearest cent (half up)."""
    return (cents * (10_000 + increase_bp) + 5_000) // 10_000


def _fiscal_range(year: int):
    months = services.fiscal_months(year)
    return months


def expense_actuals(fiscal_year: int) -> dict[int, int]:
    """Expense category id -> approved or paid invoices dated in the fiscal year."""
    from operations.models import Invoice

    totals: dict[int, int] = {}
    for year, month in _fiscal_range(fiscal_year):
        for cat_id, cents in Invoice.objects.filter(
            state__in=["approved", "paid"], invoice_date__year=year, invoice_date__month=month
        ).values_list("category_id", "amount_cents"):
            totals[cat_id] = totals.get(cat_id, 0) + cents
    return totals


def income_actuals(fiscal_year: int) -> dict[str, int]:
    kinds = {
        IncomeSource.ASSESSMENTS: [EntryKind.ASSESSMENT],
        IncomeSource.RESALE_FEES: [EntryKind.RESALE_FEE],
        IncomeSource.OTHER: [EntryKind.OTHER_CHARGE, EntryKind.LATE_FEE],
    }
    out = dict.fromkeys(kinds, 0)
    with system_access("budget preparation income"):
        for year, month in _fiscal_range(fiscal_year):
            for source, ks in kinds.items():
                out[source] += sum(
                    LedgerEntry.objects.filter(
                        kind__in=ks,
                        posted_on__year=year,
                        posted_on__month=month,
                        reversed_by__isnull=True,
                    ).values_list("amount_cents", flat=True)
                )
    return out


def homes() -> int:
    return Lot.objects.unscoped("budget homes").count()


def reserve_contribution() -> tuple[int | None, object]:
    """(annual contribution of the latest adopted reserve plan, snapshot)."""
    from ledger import reserve_plan

    snapshot = reserve_plan.latest_snapshot()
    if snapshot is None:
        return None, None
    return snapshot.annual_contribution_cents, snapshot


def _pick(budget_cents: int, actual_cents: int, basis: str) -> int:
    if basis == "actual":
        return actual_cents
    if basis == "higher":
        return max(budget_cents, actual_cents)
    return budget_cents


def per_home(budget: Budget, *, lines=None) -> dict:
    """The monthly {term} per home this budget needs."""
    lines = list(budget.lines.all()) if lines is None else lines
    expense = sum(line.annual_cents for line in lines if line.kind == LineKind.EXPENSE)
    other = sum(
        line.annual_cents
        for line in lines
        if line.kind == LineKind.INCOME and line.income_source != IncomeSource.ASSESSMENTS
    )
    count = homes()
    need = max(expense - other, 0)
    step, citation = rule_with_citation(ROUNDING_RULE)
    step = max(int(step or 1), 1)
    exact = -(-need // (count * 12)) if count else 0  # ceiling, whole cents
    monthly = -(-exact // step) * step
    current = rule("dues.monthly_cents")
    change = monthly - current if current is not None else None
    return {
        "expense_cents": expense,
        "other_income_cents": other,
        "need_cents": need,
        "homes": count,
        "exact_cents": exact,
        "monthly_cents": monthly,
        "rounding_cents": step,
        "citation": citation,
        "annual_total_cents": monthly * count * 12,
        "current_cents": current,
        "change_cents": change,
        # Basis points of the change, for "+4.2%" (None without a current amount).
        "change_bp": (change * 10_000 // current) if current else None,
    }


def prepare_next(
    source: Budget,
    *,
    basis: str = "budget",
    increase_bp: int = 0,
    include_reserve: bool = True,
    actor,
    request=None,
) -> Budget:
    """A draft budget for the year after ``source``, pre-filled (see module)."""
    from operations import services as ops
    from operations.models import ExpenseCategory

    require(actor, "budget.manage")
    if basis not in BASES:
        raise BudgetPrepError("Choose what to start from.")
    if not isinstance(increase_bp, int) or not 0 <= increase_bp <= MAX_INCREASE_BP:
        raise BudgetPrepError("The increase is 0 to 50%.")
    year = source.fiscal_year + 1
    if Budget.objects.filter(fiscal_year=year).exists():
        raise BudgetPrepError(f"FY{year} already has a budget.")
    actuals = expense_actuals(source.fiscal_year)
    income = income_actuals(source.fiscal_year)
    reserve_cents, _snapshot = reserve_contribution() if include_reserve else (None, None)
    reserve_cat = None
    if reserve_cents is not None:
        reserve_cat = ExpenseCategory.objects.filter(code=RESERVE_CODE).first()
        if reserve_cat is None:
            reserve_cat = ops.save_category(
                code=RESERVE_CODE, name=RESERVE_NAME, actor=actor, request=request
            )
    plan: list[dict] = []
    seen: set[int] = set()
    for line in source.lines.select_related("category").order_by("kind", "name"):
        if line.kind == LineKind.EXPENSE:
            seen.add(line.category_id)
            if reserve_cat is not None and line.category_id == reserve_cat.pk:
                continue  # replaced by the adopted reserve plan's figure below
            cents = _pick(line.annual_cents, actuals.get(line.category_id, 0), basis)
            plan.append(
                {
                    "kind": LineKind.EXPENSE,
                    "name": line.name,
                    "category": line.category,
                    "annual_cents": grow(cents, increase_bp),
                }
            )
        elif line.income_source != IncomeSource.ASSESSMENTS:
            cents = _pick(line.annual_cents, income.get(line.income_source, 0), basis)
            plan.append(
                {
                    "kind": LineKind.INCOME,
                    "name": line.name,
                    "income_source": line.income_source,
                    "annual_cents": cents,
                }
            )
    if basis != "budget":
        for cat in ExpenseCategory.objects.filter(pk__in=set(actuals) - seen).order_by("code"):
            if reserve_cat is not None and cat.pk == reserve_cat.pk:
                continue
            plan.append(
                {
                    "kind": LineKind.EXPENSE,
                    "name": cat.name,
                    "category": cat,
                    "annual_cents": grow(actuals[cat.pk], increase_bp),
                }
            )
    if reserve_cat is not None:
        plan.append(
            {
                "kind": LineKind.EXPENSE,
                "name": RESERVE_NAME,
                "category": reserve_cat,
                "annual_cents": reserve_cents,
            }
        )
    fake = [
        BudgetLine(
            kind=p["kind"],
            name=p["name"],
            annual_cents=p["annual_cents"],
            income_source=p.get("income_source", ""),
        )
        for p in plan
    ]
    figures = per_home(source, lines=fake)
    plan.append(
        {
            "kind": LineKind.INCOME,
            "name": term_text("{Terms}"),
            "income_source": IncomeSource.ASSESSMENTS,
            "annual_cents": figures["annual_total_cents"],
        }
    )
    with transaction.atomic():
        budget = services.create_budget(fiscal_year=year, actor=actor, request=request)
        for p in plan:
            if p["annual_cents"] <= 0 and p["kind"] == LineKind.INCOME:
                continue
            services.add_line(
                budget,
                kind=p["kind"],
                name=p["name"],
                annual_cents=p["annual_cents"],
                category=p.get("category"),
                income_source=p.get("income_source", ""),
                actor=actor,
                request=request,
            )
        increase = f", plus {increase_bp / 100:g}%" if increase_bp else ""
        Budget.objects.filter(pk=budget.pk).update(
            notes=f"Prepared from the FY{source.fiscal_year} budget: {BASES[basis].lower()}"
            f"{increase}."
        )
    record_event(
        "budget.prepared",
        actor=actor,
        request=request,
        target=budget,
        metadata={
            "fiscal_year": year,
            "source_year": source.fiscal_year,
            "lines": len(plan),
            "basis": basis,
        },
    )
    budget.refresh_from_db()
    return budget


def comparison(budget: Budget) -> dict:
    """This draft beside the year before: last year's budget and actuals by
    line (matched by expense category or income source), with the change."""
    prior = Budget.objects.filter(fiscal_year=budget.fiscal_year - 1).first()
    actuals = expense_actuals(budget.fiscal_year - 1)
    income = income_actuals(budget.fiscal_year - 1)
    prior_lines = list(prior.lines.select_related("category")) if prior else []

    def key(line):
        if line.kind == LineKind.EXPENSE:
            return ("expense", line.category_id)
        return ("income", line.income_source)

    prior_by_key: dict = {}
    for line in prior_lines:
        prior_by_key[key(line)] = prior_by_key.get(key(line), 0) + line.annual_cents
    rows, used = [], set()
    for line in budget.lines.select_related("category").order_by("kind", "name"):
        k = key(line)
        first = k not in used
        used.add(k)
        actual = (
            actuals.get(line.category_id, 0)
            if line.kind == LineKind.EXPENSE
            else income.get(line.income_source, 0)
        )
        before = prior_by_key.get(k, 0) if first else 0
        rows.append(
            {
                "line": line,
                "prior_cents": before if first else None,
                "actual_cents": actual if first else None,
                "next_cents": line.annual_cents,
                "change_cents": line.annual_cents - before if first else None,
            }
        )
    for line in prior_lines:
        if key(line) not in used:
            used.add(key(line))
            rows.append(
                {
                    "line": line,
                    "prior_cents": prior_by_key[key(line)],
                    "actual_cents": actuals.get(line.category_id, 0)
                    if line.kind == LineKind.EXPENSE
                    else income.get(line.income_source, 0),
                    "next_cents": 0,
                    "change_cents": -prior_by_key[key(line)],
                    "dropped": True,
                }
            )
    reserve_cents, snapshot = reserve_contribution()
    return {
        "prior": prior,
        "rows": rows,
        "per_home": per_home(budget),
        "reserve_cents": reserve_cents,
        "snapshot": snapshot,
    }
