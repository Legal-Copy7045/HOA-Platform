"""Reserve plan services: the board's components and settings, the live
projection, adoption (an append-only snapshot) and the CSV export.

The arithmetic is in ledger/reserves.py (pure, unit tested). Changing the
plan and adopting it are board decisions ("reserves.manage"); the manager,
auditor and board may view it ("reserves.view"); owners see only the latest
adopted snapshot's summary ("reserves.view_summary"), never board notes.
"""

from __future__ import annotations

import csv
import datetime
import io
from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone

from audit.services import record_event
from community.models import CommunityProfile
from core.lists import safe_cell
from core.policy import SYSTEM, require
from ledger import reserves
from ledger.models import (
    ReserveCategory,
    ReserveComponent,
    ReservePlanSettings,
    ReserveStudySnapshot,
)

MAX_RATE_BP = 2000  # 20%: anything above is almost certainly a typing mistake
MIN_HORIZON, MAX_HORIZON = 5, 60
MAX_LIFE = 100


class ReserveError(ValueError):
    """A reserve plan change the service refuses (shown on the form)."""


def _user(actor):
    return None if actor is SYSTEM else actor


@dataclass(frozen=True)
class Balance:
    cents: int
    as_of: datetime.date | None
    source: str  # "typed", "letters" (resale certificate figure) or "none"


def starting_balance(plan: ReservePlanSettings | None = None) -> Balance:
    """The reserve balance the plan starts from.

    The ledger holds owner accounts only (there is no reserve fund account
    in it yet), so the balance is the board's typed figure; when none is
    typed, the "reserved for capital expenditures" figure from Letters and
    certificates (the resale certificate) is used.
    """
    plan = plan or ReservePlanSettings.get()
    if plan.starting_balance_cents is not None:
        return Balance(plan.starting_balance_cents, plan.balance_as_of, "typed")
    profile = CommunityProfile.get()
    if profile.resale_reserves_cents is not None:
        return Balance(profile.resale_reserves_cents, plan.balance_as_of, "letters")
    return Balance(0, plan.balance_as_of, "none")


def start_year(plan: ReservePlanSettings) -> int:
    """The plan's first year: the balance date's year, else this year."""
    return (plan.balance_as_of or timezone.localdate()).year


def active_components():
    return ReserveComponent.objects.filter(active=True)


def current_inputs() -> tuple[reserves.PlanInputs, Balance]:
    plan = ReservePlanSettings.get()
    balance = starting_balance(plan)
    year = start_year(plan)
    components = tuple(
        reserves.ComponentInput(
            name=c.name,
            cost_cents=c.replacement_cost_cents,
            useful_life=c.useful_life_years,
            remaining_life=c.remaining_life(year),
            category=c.category,
        )
        for c in active_components()
    )
    inputs = reserves.PlanInputs(
        start_year=year,
        starting_balance_cents=balance.cents,
        annual_contribution_cents=plan.annual_contribution_cents,
        inflation_bp=plan.inflation_bp,
        interest_bp=plan.interest_bp,
        horizon_years=plan.horizon_years,
        components=components,
    )
    return inputs, balance


def current_projection() -> tuple[reserves.Projection, Balance]:
    inputs, balance = current_inputs()
    return reserves.analyse(inputs), balance


def latest_snapshot() -> ReserveStudySnapshot | None:
    return ReserveStudySnapshot.objects.order_by("-adopted_on", "-id").first()


# --- Board changes ------------------------------------------------------------------------


def _check_component(fields: dict) -> dict:
    name = (fields.get("name") or "").strip()
    if not name:
        raise ReserveError("Give the component a name.")
    if fields.get("category") not in ReserveCategory.values:
        raise ReserveError("Choose a category.")
    cost = fields.get("replacement_cost_cents")
    if cost is None or cost < 0:
        raise ReserveError("Enter today's replacement cost.")
    life = fields.get("useful_life_years")
    if not life or not 1 <= life <= MAX_LIFE:
        raise ReserveError(f"Useful life must be between 1 and {MAX_LIFE} years.")
    remaining = fields.get("remaining_life_years")
    last = fields.get("last_replaced_year")
    if remaining is None and last is None:
        raise ReserveError("Enter the years left, or the year it was last replaced.")
    if remaining is not None and not 0 <= remaining <= MAX_LIFE:
        raise ReserveError(f"Years left must be between 0 and {MAX_LIFE}.")
    this_year = timezone.localdate().year
    if last is not None and not 1900 <= last <= this_year:
        raise ReserveError(f"The year last replaced must be between 1900 and {this_year}.")
    return {
        "name": name[:120],
        "category": fields["category"],
        "quantity": (fields.get("quantity") or "").strip()[:60],
        "replacement_cost_cents": cost,
        "useful_life_years": life,
        "remaining_life_years": remaining,
        "last_replaced_year": last,
        "notes": (fields.get("notes") or "").strip(),
        "active": bool(fields.get("active", True)),
    }


def save_component(
    component: ReserveComponent | None = None, *, actor, request=None, **fields
) -> ReserveComponent:
    require(actor, "reserves.manage")
    values = _check_component(fields)
    created = component is None
    component = component or ReserveComponent()
    for key, value in values.items():
        setattr(component, key, value)
    component.updated_by = _user(actor)
    component.updated_at = timezone.now()
    component.save()
    record_event(
        "reserves.component_saved",
        actor=_user(actor),
        request=request,
        target=component,
        metadata={
            "created": created,
            "category": component.category,
            "cost_cents": component.replacement_cost_cents,
            "active": component.active,
        },
    )
    return component


def save_settings(
    *,
    starting_balance_cents: int | None,
    balance_as_of: datetime.date | None,
    annual_contribution_cents: int,
    inflation_bp: int,
    interest_bp: int,
    horizon_years: int,
    actor,
    request=None,
) -> ReservePlanSettings:
    require(actor, "reserves.manage")
    if annual_contribution_cents is None or annual_contribution_cents < 0:
        raise ReserveError("Enter the annual contribution (0 if none).")
    for label, value in (("Inflation", inflation_bp), ("Interest", interest_bp)):
        if value is None or not 0 <= value <= MAX_RATE_BP:
            raise ReserveError(f"{label} must be between 0% and {MAX_RATE_BP // 100}%.")
    if not horizon_years or not MIN_HORIZON <= horizon_years <= MAX_HORIZON:
        raise ReserveError(f"The plan must cover {MIN_HORIZON} to {MAX_HORIZON} years.")
    if balance_as_of and not 1900 <= balance_as_of.year <= timezone.localdate().year + 1:
        raise ReserveError("Check the balance date.")
    plan = ReservePlanSettings.get()
    plan.starting_balance_cents = starting_balance_cents
    plan.balance_as_of = balance_as_of
    plan.annual_contribution_cents = annual_contribution_cents
    plan.inflation_bp = inflation_bp
    plan.interest_bp = interest_bp
    plan.horizon_years = horizon_years
    plan.updated_by = _user(actor)
    plan.updated_at = timezone.now()
    plan.save()
    record_event(
        "reserves.settings_saved",
        actor=_user(actor),
        request=request,
        metadata={
            "annual_contribution_cents": annual_contribution_cents,
            "inflation_bp": inflation_bp,
            "interest_bp": interest_bp,
            "horizon_years": horizon_years,
        },
    )
    return plan


@transaction.atomic
def adopt(*, actor, request=None) -> ReserveStudySnapshot:
    """Record the current plan as adopted by the board (append-only)."""
    require(actor, "reserves.manage")
    if actor is SYSTEM:
        raise ReserveError("A board member adopts the reserve plan.")
    result, balance = current_projection()
    inputs = result.inputs
    if not inputs.components:
        raise ReserveError("Add at least one active component before adopting the plan.")
    snapshot = ReserveStudySnapshot.objects.create(
        adopted_on=timezone.localdate(),
        adopted_by=actor,
        start_year=inputs.start_year,
        horizon_years=inputs.horizon_years,
        starting_balance_cents=inputs.starting_balance_cents,
        balance_as_of=balance.as_of,
        annual_contribution_cents=inputs.annual_contribution_cents,
        inflation_bp=inputs.inflation_bp,
        interest_bp=inputs.interest_bp,
        fully_funded_cents=result.fully_funded_cents,
        percent_funded=result.percent_funded,
        needed_contribution_cents=result.needed_contribution_cents,
        total_expense_cents=result.total_expense_cents,
        lowest_end_cents=result.lowest_end_cents,
        lowest_year=result.lowest_year,
        first_negative_year=result.first_negative_year,
        components=[
            {
                "name": c.name,
                "category": c.category,
                "cost_cents": c.cost_cents,
                "useful_life": c.useful_life,
                "remaining_life": c.remaining_life,
            }
            for c in inputs.components
        ],
        next_items=[
            {"year": year, "name": name, "cents": cents} for year, name, cents in result.next_items
        ],
    )
    record_event(
        "reserves.adopted",
        actor=actor,
        request=request,
        target=snapshot,
        metadata={
            "components": len(inputs.components),
            "percent_funded": result.percent_funded,
            "needed_contribution_cents": result.needed_contribution_cents,
            "horizon_years": inputs.horizon_years,
        },
    )
    return snapshot


# --- Export -------------------------------------------------------------------------------

EXPORT_HEADER = [
    "Year",
    "Starting balance",
    "Contribution",
    "Interest",
    "Expenses",
    "Ending balance",
    "Below zero",
    "Items due",
]


def _dollars(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}{cents // 100}.{cents % 100:02d}"


def export_csv(*, actor, request=None) -> bytes:
    require(actor, "reserves.view")
    result, _balance = current_projection()
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(EXPORT_HEADER)
    for row in result.rows:
        writer.writerow(
            [
                row.year,
                _dollars(row.begin_cents),
                _dollars(row.contribution_cents),
                _dollars(row.interest_cents),
                _dollars(row.expense_cents),
                _dollars(row.end_cents),
                "yes" if row.negative else "",
                safe_cell("; ".join(f"{name} {_dollars(cents)}" for name, cents in row.items)),
            ]
        )
    record_event(
        "reserves.exported",
        actor=_user(actor),
        request=request,
        metadata={"rows": len(result.rows), "start_year": result.inputs.start_year},
    )
    return buf.getvalue().encode()
