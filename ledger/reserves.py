"""Reserve plan: a simple in-house reserve study (pure functions, no database).

The board lists the things the association will one day replace (roads,
ponds, signs ...) with today's replacement cost, how long each lasts and how
many years it has left. ``analyse()`` projects the reserve fund year by year
and works out how well funded it is today. Everything is integer cents
(CLAUDE.md rule 7); rates are basis points (1% = 100).

How each plan year is worked out (year 0 is the plan's first calendar year,
and the starting balance is the balance at its start):

* contribution: the annual contribution, the same every year (not inflated);
* interest: the interest rate times the balance at the start of the year,
  rounded to the nearest cent (half a cent rounds up). A balance below zero
  earns nothing and is charged nothing;
* expenses: every component due that year, at today's cost inflated by the
  inflation rate for each year from year 0 (cost x (1 + rate)^year, worked
  exactly and rounded to the nearest cent once per component per year). A
  component with R years left is due in year R, then every useful life after;
* ending balance = start + contribution + interest - expenses; it is the
  next year's starting balance.

Percent funded compares the starting balance with the fully funded balance:
the sum over active components of cost x age / useful life, where age =
useful life - remaining life (kept between 0 and the useful life), summed
exactly and rounded once to the nearest cent. The percent is rounded to the
nearest whole percent.

The needed contribution is the smallest level annual contribution, to the
cent, that keeps every year's ending balance at zero or above (found by
binary search; more contribution never lowers a later balance).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction

BP = 10_000  # basis points in 100%


def round_half_up(value: Fraction) -> int:
    """Nearest integer; exact halves round away from zero."""
    if value < 0:
        return -round_half_up(-value)
    return int((value * 2 + 1) // 2)


@dataclass(frozen=True)
class ComponentInput:
    name: str
    cost_cents: int
    useful_life: int
    remaining_life: int
    category: str = "other"

    def __post_init__(self):
        if self.useful_life < 1:
            raise ValueError("Useful life must be at least one year.")
        if self.remaining_life < 0:
            raise ValueError("Remaining life cannot be negative.")
        if self.cost_cents < 0:
            raise ValueError("Replacement cost cannot be negative.")

    def due_in(self, year_index: int) -> bool:
        if year_index < self.remaining_life:
            return False
        return (year_index - self.remaining_life) % self.useful_life == 0

    @property
    def age(self) -> int:
        return max(0, min(self.useful_life, self.useful_life - self.remaining_life))


@dataclass(frozen=True)
class PlanInputs:
    start_year: int
    starting_balance_cents: int
    annual_contribution_cents: int
    inflation_bp: int
    interest_bp: int
    horizon_years: int
    components: tuple[ComponentInput, ...] = ()

    def __post_init__(self):
        if self.horizon_years < 1:
            raise ValueError("The plan must cover at least one year.")
        if self.inflation_bp < 0 or self.interest_bp < 0:
            raise ValueError("Rates cannot be negative.")


@dataclass(frozen=True)
class YearRow:
    index: int
    year: int
    begin_cents: int
    contribution_cents: int
    interest_cents: int
    expense_cents: int
    end_cents: int
    items: tuple[tuple[str, int], ...] = ()

    @property
    def negative(self) -> bool:
        return self.end_cents < 0


@dataclass(frozen=True)
class Projection:
    inputs: PlanInputs
    rows: tuple[YearRow, ...]
    fully_funded_cents: int
    percent_funded: int | None  # None when nothing has aged yet
    needed_contribution_cents: int
    total_expense_cents: int
    lowest_end_cents: int
    lowest_year: int
    first_negative_year: int | None
    next_items: tuple[tuple[int, str, int], ...] = field(default=())  # (year, name, cents)

    @property
    def negative_years(self) -> list[int]:
        return [r.year for r in self.rows if r.negative]


def inflated_cost(cost_cents: int, inflation_bp: int, years: int) -> int:
    """Today's cost after ``years`` of inflation, to the nearest cent."""
    factor = Fraction(BP + inflation_bp, BP) ** years
    return round_half_up(cost_cents * factor)


def interest_on(balance_cents: int, interest_bp: int) -> int:
    if balance_cents <= 0 or interest_bp == 0:
        return 0
    return round_half_up(Fraction(balance_cents * interest_bp, BP))


def project(inputs: PlanInputs, annual_contribution_cents: int | None = None) -> list[YearRow]:
    """Year-by-year table for the plan horizon."""
    contribution = (
        inputs.annual_contribution_cents
        if annual_contribution_cents is None
        else annual_contribution_cents
    )
    balance = inputs.starting_balance_cents
    rows = []
    for i in range(inputs.horizon_years):
        items = tuple(
            (c.name, inflated_cost(c.cost_cents, inputs.inflation_bp, i))
            for c in inputs.components
            if c.due_in(i)
        )
        expense = sum(cents for _name, cents in items)
        interest = interest_on(balance, inputs.interest_bp)
        end = balance + contribution + interest - expense
        rows.append(
            YearRow(
                index=i,
                year=inputs.start_year + i,
                begin_cents=balance,
                contribution_cents=contribution,
                interest_cents=interest,
                expense_cents=expense,
                end_cents=end,
                items=items,
            )
        )
        balance = end
    return rows


def fully_funded_cents(inputs: PlanInputs) -> int:
    total = sum(
        (Fraction(c.cost_cents * c.age, c.useful_life) for c in inputs.components),
        Fraction(0),
    )
    return round_half_up(total)


def percent_funded(balance_cents: int, fully_funded: int) -> int | None:
    if fully_funded <= 0:
        return None
    return round_half_up(Fraction(balance_cents * 100, fully_funded))


def needed_contribution(inputs: PlanInputs) -> int:
    """Smallest level annual contribution (cents) that never goes negative."""

    def ok(contribution: int) -> bool:
        return all(r.end_cents >= 0 for r in project(inputs, contribution))

    if ok(0):
        return 0
    # Contributing, every year, all the horizon's expenses plus any shortfall
    # at the start keeps every ending balance at zero or above.
    high = max(
        1,
        sum(r.expense_cents for r in project(inputs, 0)) - min(0, inputs.starting_balance_cents),
    )
    low = 0  # not enough
    while high - low > 1:
        mid = (low + high) // 2
        if ok(mid):
            high = mid
        else:
            low = mid
    return high


def analyse(inputs: PlanInputs, *, next_count: int = 5) -> Projection:
    rows = project(inputs)
    ffb = fully_funded_cents(inputs)
    lowest = min(rows, key=lambda r: (r.end_cents, r.index))
    negative = next((r.year for r in rows if r.negative), None)
    upcoming = [(r.year, name, cents) for r in rows for name, cents in r.items]
    return Projection(
        inputs=inputs,
        rows=tuple(rows),
        fully_funded_cents=ffb,
        percent_funded=percent_funded(inputs.starting_balance_cents, ffb),
        needed_contribution_cents=needed_contribution(inputs),
        total_expense_cents=sum(r.expense_cents for r in rows),
        lowest_end_cents=lowest.end_cents,
        lowest_year=lowest.year,
        first_negative_year=negative,
        next_items=tuple(upcoming[:next_count]),
    )


def per_lot_monthly(annual_cents: int, lots: int) -> int:
    """Annual amount shared by ``lots`` lots over 12 months, rounded up to the cent."""
    if lots <= 0:
        return 0
    return -(-annual_cents // (12 * lots))
