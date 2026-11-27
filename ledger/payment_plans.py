"""Payment plans for collection cases (D76).

A plan pays off a home's arrears in installments:

* **Prepare** (``prepare``, board or manager): the open charges of the
  home's current ownership account are the plan's total (fixed on the plan
  with the charge ids); the installments (due date and amount) must add up
  to it. The rules at that moment are fixed on the plan: whether late fees
  stop while it is kept (``payment_plan.suspend_late_fees``), the grace
  days before an installment counts as missed (``payment_plan.grace_days``)
  and the days after which a missed installment defaults the plan
  (``payment_plan.default_days``).
* **Approve** (``approve``): a board member other than the one who prepared
  it, never on their own home's matter (D65). Approval moves the collection
  case to "Payment plan agreed" through the collection workflow.
* **Acknowledge** (``acknowledge``): an owner on the deed may record, by a
  signed-in click on My account, that they agree; optional.
* **Progress** comes from the open-item allocation: what of the covered
  charges has been paid (``paid_cents``). An installment is met when the
  paid amount reaches the installments due so far.
* **Daily check** (``check_plans``, as SYSTEM): a plan whose charges are
  paid completes (the case closes when nothing is past due, otherwise it
  returns to its earlier state); a plan with an installment still unmet
  ``default_days`` after its due date defaults, and the case returns to the
  state before the plan (letters and late fees resume). Both go through the
  collection workflow as SYSTEM and are audited.
* **Cancel** (``cancel``): the board ends a plan early, with a note.

Every row is append-only; a plan's status is derived from its events.
"""

from __future__ import annotations

import datetime
import itertools

from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone

import ledger.finance_gaps_audit  # noqa: F401  (registers the audit actions)
from audit.services import record_event
from community.rules import rule, rule_with_citation
from core import conflicts
from core.money import format_cents
from core.policy import SYSTEM, can, require, roles_for
from core.scoping import internal, system_access
from ledger.models import (
    PLAN_ENDINGS,
    CollectionCase,
    PaymentPlan,
    PaymentPlanEvent,
    PaymentPlanInstallment,
    PlanEventKind,
)

SUSPEND_RULE = "payment_plan.suspend_late_fees"
GRACE_RULE = "payment_plan.grace_days"
DEFAULT_RULE = "payment_plan.default_days"
MAX_INSTALLMENTS = 60
PLAN_STATES = ("open", "reminder_sent", "second_reminder", "final_notice")


class PaymentPlanError(ValueError):
    """A step the service refuses (shown on the page)."""


def _user(actor):
    return None if actor is SYSTEM else actor


def _refuse_own(actor, plan_or_case, action: str, request=None) -> None:
    conflicts.refuse(
        actor,
        plan_or_case,
        area="payment_plan",
        action=action,
        request=request,
        error=PaymentPlanError,
    )


# --- Status -------------------------------------------------------------------------------


def _events(plan: PaymentPlan) -> dict[str, PaymentPlanEvent]:
    with system_access("payment plan events"):
        return {e.kind: e for e in PaymentPlanEvent.objects.filter(plan=plan)}


def status(plan: PaymentPlan, events=None) -> str:
    """proposed, active, completed, defaulted, cancelled or declined."""
    events = _events(plan) if events is None else events
    for kind in PLAN_ENDINGS:
        if kind in events:
            return kind
    return "active" if PlanEventKind.APPROVED in events else "proposed"


STATUS_LABELS = {
    "proposed": "Waiting for board approval",
    "active": "Current",
    "completed": "Completed",
    "defaulted": "Defaulted",
    "cancelled": "Cancelled",
    "declined": "Declined",
}


@internal("approved payment plan")
def approved_plan(case: CollectionCase) -> PaymentPlan | None:
    """The case's approved plan that has not ended, or None."""
    for plan in PaymentPlan.objects.filter(case=case).order_by("-created_at"):
        if status(plan) == "active":
            return plan
    return None


@internal("payment plan of a case")
def plan_for_case(case: CollectionCase) -> PaymentPlan | None:
    """The case's plan that is proposed or current, or None."""
    for plan in PaymentPlan.objects.filter(case=case).order_by("-created_at"):
        if status(plan) in ("proposed", "active"):
            return plan
    return None


@internal("payment plan progress")
def paid_cents(plan: PaymentPlan, on: datetime.date | None = None) -> int:
    """How much of the plan's charges the allocation has paid by ``on``."""
    from ledger import allocation

    on = on or timezone.localdate()
    state = allocation.state_as_of(plan.lot_id, on)
    remaining = sum(state.open.get(int(pk), 0) for pk in plan.charge_ids)
    return max(min(plan.total_cents - remaining, plan.total_cents), 0)


@internal("payment plan schedule")
def schedule(plan: PaymentPlan, today: datetime.date | None = None) -> list[dict]:
    """Each installment with what is due so far and its status: met, missed
    (unmet after the grace days), due (due, within the grace days) or
    upcoming."""
    today = today or timezone.localdate()
    paid = paid_cents(plan, today)
    rows, cumulative = [], 0
    for inst in PaymentPlanInstallment.objects.filter(plan=plan).order_by("number"):
        cumulative += inst.amount_cents
        grace_end = inst.due_on + datetime.timedelta(days=plan.grace_days)
        if paid >= cumulative:
            state = "met"
        elif today > grace_end:
            state = "missed"
        elif today >= inst.due_on:
            state = "due"
        else:
            state = "upcoming"
        rows.append(
            {
                "installment": inst,
                "cumulative_cents": cumulative,
                "status": state,
                "grace_end": grace_end,
                "default_on": inst.due_on + datetime.timedelta(days=plan.default_days),
            }
        )
    return rows


STATE_LABELS = {"met": "Paid", "missed": "Missed", "due": "Due", "upcoming": "Upcoming"}


def overview(plan: PaymentPlan, today=None) -> dict:
    rows = schedule(plan, today)
    events = _events(plan)
    st = status(plan, events)
    return {
        "plan": plan,
        "status": st,
        "status_label": STATUS_LABELS[st],
        "events": events,
        "schedule": rows,
        "paid_cents": paid_cents(plan, today),
        "remaining_cents": plan.total_cents - paid_cents(plan, today),
        "next": next((r for r in rows if r["status"] != "met"), None),
        "missed": [r for r in rows if r["status"] == "missed"],
        "acknowledged": events.get(PlanEventKind.ACKNOWLEDGED),
    }


# --- Prepare, approve, decline ------------------------------------------------------------


@internal("payment plan charges")
def covered_charges(case: CollectionCase) -> tuple[list, int, int | None]:
    """(open charges of the home's current account, their total, account id)."""
    from ledger import allocation
    from ledger.accounts import CURRENT, current_account_id

    charges = allocation.open_charges(case.lot_id, account=CURRENT)
    return charges, sum(c.open_cents for c in charges), current_account_id(case.lot_id)


def even_installments(total_cents: int, count: int, first_due: datetime.date) -> list[tuple]:
    """A suggestion: ``count`` monthly installments from ``first_due``, the
    odd cents on the first."""
    from ledger.special_assessments import installment_amounts

    amounts = installment_amounts(total_cents, count)
    out = []
    for i, cents in enumerate(amounts):
        month = first_due.month - 1 + i
        year = first_due.year + month // 12
        month = month % 12 + 1
        day = min(first_due.day, 28)
        out.append((datetime.date(year, month, day), cents))
    return out


def prepare(
    case: CollectionCase,
    *,
    installments: list[tuple[datetime.date, int]],
    start_on: datetime.date | None = None,
    note: str = "",
    actor,
    request=None,
) -> PaymentPlan:
    """Prepare a plan for the case's arrears (waits for a board approval)."""
    if actor is not SYSTEM:
        require(actor, "payment_plan.prepare")
    _refuse_own(actor, case, "prepare", request)
    case = CollectionCase.objects.unscoped("plan case").get(pk=case.pk)  # today's state
    if case.state not in PLAN_STATES:
        raise PaymentPlanError("A payment plan is set up for an open collection case.")
    if plan_for_case(case) is not None:
        raise PaymentPlanError("This case already has a payment plan.")
    start_on = start_on or timezone.localdate()
    items = list(installments or [])
    if not 1 <= len(items) <= MAX_INSTALLMENTS:
        raise PaymentPlanError(f"A plan has 1 to {MAX_INSTALLMENTS} installments.")
    dates = [d for d, _c in items]
    if any(b <= a for a, b in itertools.pairwise(dates)):
        raise PaymentPlanError("Each installment is due after the one before it.")
    if dates[0] < start_on:
        raise PaymentPlanError("The first installment cannot be due before the plan starts.")
    if any(not isinstance(c, int) or c <= 0 for _d, c in items):
        raise PaymentPlanError("Each installment is more than zero.")
    charges, total, account_id = covered_charges(case)
    if total <= 0 or account_id is None:
        raise PaymentPlanError("Nothing is open on this home's account: no plan is needed.")
    if sum(c for _d, c in items) != total:
        raise PaymentPlanError(
            f"The installments add up to {format_cents(sum(c for _d, c in items))}; the open "
            f"balance the plan covers is {format_cents(total)}."
        )
    suspend, cite1 = rule_with_citation(SUSPEND_RULE)
    grace, cite2 = rule_with_citation(GRACE_RULE)
    default, cite3 = rule_with_citation(DEFAULT_RULE)
    with transaction.atomic():
        plan = PaymentPlan.objects.create(
            case=case,
            lot_id=case.lot_id,
            account_id=account_id,
            total_cents=total,
            charge_ids=[c.entry.pk for c in charges],
            start_on=start_on,
            prior_state=case.state,
            suspend_late_fees=bool(suspend),
            grace_days=int(grace or 0),
            default_days=max(int(default or 0), int(grace or 0)),
            rule_citation="; ".join(c for c in (cite1, cite2, cite3) if c)[:400],
            note=(note or "").strip()[:2000],
            prepared_by=actor if actor is not SYSTEM else case.opened_by,
        )
        PaymentPlanInstallment.objects.bulk_create(
            [
                PaymentPlanInstallment(plan=plan, number=i, due_on=d, amount_cents=c)
                for i, (d, c) in enumerate(items, start=1)
            ]
        )
        record_event(
            "payment_plan.prepared",
            actor=_user(actor),
            request=request,
            target=plan,
            metadata={
                "plan_id": plan.pk,
                "case_id": case.pk,
                "lot_id": case.lot_id,
                "amount_cents": total,
                "installments": len(items),
            },
        )
    return plan


def _event(plan, kind, *, actor, note=""):
    try:
        with transaction.atomic():
            return PaymentPlanEvent.objects.create(
                plan=plan, kind=kind, by=_user(actor), note=(note or "")[:300]
            )
    except IntegrityError as exc:
        raise PaymentPlanError("That was already recorded.") from exc


def _case(plan) -> CollectionCase:
    with system_access("payment plan case"):
        return CollectionCase.objects.get(pk=plan.case_id)


def approve(plan: PaymentPlan, *, note: str = "", actor, request=None) -> PaymentPlan:
    """A board member other than the preparer approves; the case moves to
    "Payment plan agreed"."""
    from ledger import services

    require(actor, "payment_plan.approve")
    _refuse_own(actor, plan, "approve", request)
    if plan.prepared_by_id == actor.pk:
        raise PaymentPlanError(
            "You prepared this plan; another board member approves it (two people)."
        )
    with transaction.atomic():
        CollectionCase.objects.unscoped("plan approval").select_for_update().get(pk=plan.case_id)
        if status(plan) != "proposed":
            raise PaymentPlanError("Only a plan waiting for approval can be approved.")
        _event(plan, PlanEventKind.APPROVED, actor=actor, note=note)
        count = PaymentPlanInstallment.objects.unscoped("plan count").filter(plan=plan).count()
        services.COLLECTION.apply(
            _case(plan),
            "payment_plan",
            actor=actor,
            request=request,
            note=f"Payment plan #{plan.pk} approved: {format_cents(plan.total_cents)} in "
            f"{count} installment{'s' if count != 1 else ''}.",
        )
        record_event(
            "payment_plan.approved",
            actor=actor,
            request=request,
            target=plan,
            metadata={"plan_id": plan.pk, "case_id": plan.case_id, "lot_id": plan.lot_id},
        )
    return plan


def decline(plan: PaymentPlan, *, note: str, actor, request=None) -> PaymentPlan:
    require(actor, "payment_plan.approve")
    _refuse_own(actor, plan, "decline", request)
    if not (note or "").strip():
        raise PaymentPlanError("Say why the plan is declined.")
    with transaction.atomic():
        if status(plan) != "proposed":
            raise PaymentPlanError("Only a plan waiting for approval can be declined.")
        _event(plan, PlanEventKind.DECLINED, actor=actor, note=note)
    record_event(
        "payment_plan.declined",
        actor=actor,
        request=request,
        target=plan,
        metadata={"plan_id": plan.pk, "case_id": plan.case_id},
    )
    return plan


def may_acknowledge(user, plan: PaymentPlan) -> bool:
    """An owner on the deed of the plan's account, today."""
    from ledger.accounts import current_account_id

    if not can(user, "payment_plan.acknowledge"):
        return False
    roles = roles_for(user)
    if plan.lot_id not in roles.owned_lot_ids:
        return False
    return current_account_id(plan.lot_id) == plan.account_id


def acknowledge(plan: PaymentPlan, *, actor, request=None) -> PaymentPlanEvent:
    """The owner records that they agree to the plan (optional)."""
    from accounts.recovery import refuse_if_locked

    if not may_acknowledge(actor, plan):
        raise PaymentPlanError("Only an owner on the deed can agree to this plan.")
    refuse_if_locked(actor, "agree to a payment plan", PaymentPlanError)
    if status(plan) not in ("proposed", "active"):
        raise PaymentPlanError("This plan has ended.")
    event = _event(plan, PlanEventKind.ACKNOWLEDGED, actor=actor)
    record_event(
        "payment_plan.acknowledged",
        actor=actor,
        request=request,
        target=plan,
        metadata={"plan_id": plan.pk, "lot_id": plan.lot_id},
    )
    return event


# --- Ending a plan ------------------------------------------------------------------------


def _return_case(plan: PaymentPlan, *, note: str, close: bool = False) -> None:
    """Move the case on through the collection workflow as SYSTEM."""
    from ledger import services

    case = _case(plan)
    if case.state != "payment_plan":
        return
    if close:
        services.COLLECTION.apply(case, "close", actor=SYSTEM, note=note)
        return
    key = {v: k for k, v in services.PLAN_END_STATES.items()}.get(plan.prior_state, "open")
    services.COLLECTION.apply(case, f"plan_end_{key}", actor=SYSTEM, note=note)


def cancel(plan: PaymentPlan, *, note: str, actor, request=None) -> PaymentPlan:
    """The board ends a current plan early; the case returns to its earlier state."""
    require(actor, "payment_plan.approve")
    _refuse_own(actor, plan, "cancel", request)
    if not (note or "").strip():
        raise PaymentPlanError("Say why the plan is cancelled.")
    with transaction.atomic():
        st = status(plan)
        if st not in ("proposed", "active"):
            raise PaymentPlanError("This plan has already ended.")
        _event(plan, PlanEventKind.CANCELLED, actor=actor, note=note)
        if st == "active":
            _return_case(plan, note=f"Payment plan #{plan.pk} cancelled: {note.strip()[:200]}")
    record_event(
        "payment_plan.cancelled",
        actor=actor,
        request=request,
        target=plan,
        metadata={"plan_id": plan.pk, "case_id": plan.case_id},
    )
    return plan


@internal("payment plan check")
def check_plans(today: datetime.date | None = None) -> dict[str, int]:
    """Daily: complete paid plans, default plans with an installment unmet
    past its default date. Returns counts."""
    from ledger import services
    from ledger.accounts import CURRENT

    today = today or timezone.localdate()
    done = {"completed": 0, "defaulted": 0}
    for plan in PaymentPlan.objects.all().order_by("pk"):
        if status(plan) != "active":
            continue
        rows = schedule(plan, today)
        if paid_cents(plan, today) >= plan.total_cents:
            with transaction.atomic():
                _event(plan, PlanEventKind.COMPLETED, actor=SYSTEM, note="All installments paid")
                clear = services.past_due(plan.lot_id, today, account=CURRENT) <= 0
                _return_case(
                    plan,
                    note=f"Payment plan #{plan.pk} completed."
                    + ("" if clear else " Charges since the plan are still past due."),
                    close=clear,
                )
            record_event(
                "payment_plan.completed",
                metadata={"plan_id": plan.pk, "case_id": plan.case_id, "lot_id": plan.lot_id},
                target=plan,
            )
            done["completed"] += 1
            continue
        overdue = [r for r in rows if r["status"] == "missed" and today > r["default_on"]]
        if overdue:
            missed = len([r for r in rows if r["status"] == "missed"])
            with transaction.atomic():
                _event(
                    plan,
                    PlanEventKind.DEFAULTED,
                    actor=SYSTEM,
                    note=f"Installment {overdue[0]['installment'].number} unpaid",
                )
                _return_case(
                    plan,
                    note=f"Payment plan #{plan.pk} defaulted: installment "
                    f"{overdue[0]['installment'].number} unpaid {plan.default_days} days after "
                    "its due date.",
                )
            record_event(
                "payment_plan.defaulted",
                metadata={
                    "plan_id": plan.pk,
                    "case_id": plan.case_id,
                    "lot_id": plan.lot_id,
                    "missed": missed,
                },
                target=plan,
            )
            done["defaulted"] += 1
    return done


# --- Hooks for late fees, interest, My items and My account ------------------------------


@internal("payment plan kept")
def plan_kept(lot_id: int, on: datetime.date | None = None) -> bool:
    """The home has an approved plan, not ended, with no installment missed
    (overdue interest, D80, is not charged while it does)."""
    for plan in PaymentPlan.objects.filter(lot_id=lot_id):
        if status(plan) != "active":
            continue
        if not any(r["status"] == "missed" for r in schedule(plan, on)):
            return True
    return False


@internal("late fees suspended")
def late_fee_suspended_lots(on: datetime.date | None = None) -> set[int]:
    """Homes with a current plan (no missed installment) that stops late fees."""
    out = set()
    for plan in PaymentPlan.objects.filter(suspend_late_fees=True):
        if status(plan) != "active":
            continue
        if any(r["status"] == "missed" for r in schedule(plan, on)):
            continue
        out.add(plan.lot_id)
    return out


def board_items(user) -> list:
    """Plans waiting for this board member's approval; missed installments."""
    from core.my_items import Item

    items = []
    with system_access("payment plan items"):
        plans = list(PaymentPlan.objects.select_related("lot").order_by("created_at"))
    for plan in plans:
        st = status(plan)
        if conflicts.own_matter(user, plan):
            continue
        url = reverse("payment_plan_detail", args=[plan.pk])
        if (
            st == "proposed"
            and can(user, "payment_plan.approve")
            and plan.prepared_by_id != user.pk
        ):
            items.append(
                Item(
                    "Collections",
                    f"Approve the payment plan for {plan.lot.label}",
                    f"{format_cents(plan.total_cents)} in installments; prepared by another "
                    "board member or the manager.",
                    url,
                    "warning",
                )
            )
        elif st == "active" and can(user, "collections.manage"):
            missed = [r for r in schedule(plan) if r["status"] == "missed"]
            if missed:
                first = missed[0]
                items.append(
                    Item(
                        "Collections",
                        f"{plan.lot.label}: payment plan installment {first['installment'].number}"
                        " missed",
                        f"{format_cents(first['installment'].amount_cents)} was due "
                        f"{first['installment'].due_on:%B %-d}. The plan defaults on "
                        f"{first['default_on']:%B %-d} if it is still unpaid.",
                        url,
                        "danger",
                        due=first["default_on"],
                    )
                )
    return items


@internal("owner payment plans")
def for_owner(user, lot_id: int) -> list[dict]:
    """The proposed or current plans on this home's current account, with
    the schedule and the next installment (My account)."""
    from ledger.accounts import current_account_id

    account_id = current_account_id(lot_id)
    out = []
    for plan in PaymentPlan.objects.filter(lot_id=lot_id, account_id=account_id).order_by(
        "-created_at"
    ):
        if status(plan) not in ("proposed", "active"):
            continue
        info = overview(plan)
        info["can_acknowledge"] = may_acknowledge(user, plan) and not info["acknowledged"]
        out.append(info)
    return out


def owner_items(user) -> list:
    """Owners on the deed: a plan to agree to; the next installment due soon."""
    from core.my_items import Item

    roles = roles_for(user)
    if not roles.owned_lot_ids:
        return []
    items = []
    for lot_id in roles.owned_lot_ids:
        for info in for_owner(user, lot_id):
            plan = info["plan"]
            if info["can_acknowledge"]:
                items.append(
                    Item(
                        "My home",
                        "Review your payment plan",
                        f"{format_cents(plan.total_cents)} in installments. You can record that "
                        "you agree on My account.",
                        reverse("my_account") + "#payment-plan",
                        "info",
                        nav="my_account",
                    )
                )
            nxt = info["next"]
            if info["status"] == "active" and nxt is not None:
                due = nxt["installment"].due_on
                if due <= timezone.localdate() + datetime.timedelta(days=14):
                    items.append(
                        Item(
                            "My home",
                            "Payment plan installment of "
                            + format_cents(nxt["installment"].amount_cents),
                            f"Due {due:%B %-d}.",
                            reverse("my_account") + "#payment-plan",
                            "warning" if nxt["status"] in ("due", "missed") else "info",
                            due=due,
                            nav="my_account",
                        )
                    )
    return items


def agreement_values(plan: PaymentPlan) -> dict:
    """Facts for the agreement letter (ledger/letters.py)."""
    rows = schedule(plan)
    return {
        "plan": plan,
        "schedule": rows,
        "suspend_late_fees": plan.suspend_late_fees,
        "grace_days": plan.grace_days,
        "default_days": plan.default_days,
        "citation": plan.rule_citation,
        "approved": _events(plan).get(PlanEventKind.APPROVED),
        "suspend_rule_now": rule(SUSPEND_RULE),
    }
