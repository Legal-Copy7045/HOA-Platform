"""Stage tracker paths (D99) for the finance state machines that are not
core.workflow Workflows (their state derives from append-only decisions):
payment plans, disbursements and special assessments. States only."""

from core.workflow import outcome, stage, track


def _plan_state(plan) -> str:
    from ledger import payment_plans

    return payment_plans.status(plan)


def _plan_visited(plan) -> list[str]:
    from ledger import payment_plans
    from ledger.models_finance_gaps import PlanEventKind

    events = payment_plans._events(plan)
    out = ["proposed"]
    if PlanEventKind.APPROVED in events:
        out.append("active")
    return [*out, payment_plans.status(plan, events)]


PAYMENT_PLAN = track(
    "ledger.paymentplan",
    [
        stage("Proposed", "proposed", who="The board: approve or decline"),
        stage("Current", "active", who="The owner: pay each installment"),
        stage("Completed", "completed"),
    ],
    [
        outcome("Declined", "declined"),
        outcome("Defaulted", "defaulted"),
        outcome("Cancelled", "cancelled"),
    ],
    state=_plan_state,
    visited=_plan_visited,
)


def _batch_state(batch) -> str:
    from ledger import disbursements as disb
    from ledger.models import DisbursementLine

    state = disb.batch_state(batch)
    if state.status != "approved":
        return state.status
    lines = DisbursementLine.objects.unscoped("stage tracker").filter(batch=batch)
    if any(disb.line_status(line, state) == "to_issue" for line in lines):
        return "approved"
    return "paid"


DISBURSEMENT = track(
    "ledger.disbursementbatch",
    [
        stage("Proposed", "awaiting", who="The board: approve or decline"),
        stage("Approved", "approved", who="The board or manager: write the checks"),
        stage("Paid out", "paid"),
    ],
    [outcome("Declined", "declined")],
    state=_batch_state,
    visited=lambda batch: ["awaiting", _batch_state(batch)],
)


def _assessment_state(sa) -> str:
    from ledger import special_assessments as sas

    if sas.cancelled_from(sa) is not None:
        return "cancelled"
    posted = len(sas._posted_numbers(sa))
    if posted == 0:
        return "levied"
    return "billed" if posted >= sa.installments else "billing"


def _next_installment(sa):
    from ledger import special_assessments as sas
    from ledger.models_finance_gaps import SpecialAssessmentInstallment

    posted = sas._posted_numbers(sa)
    nxt = (
        SpecialAssessmentInstallment.objects.filter(assessment=sa)
        .exclude(number__in=posted)
        .order_by("number")
        .first()
    )
    return nxt.due_on if nxt else None


SPECIAL_ASSESSMENT = track(
    "ledger.specialassessment",
    [
        stage("Levied", "levied", who="Automatic: the first installment is charged"),
        stage("Installments charged", "billing", who="Automatic: the next installment is charged"),
        stage("All installments charged", "billed"),
    ],
    [outcome("Cancelled", "cancelled")],
    state=_assessment_state,
    due=_next_installment,
)
