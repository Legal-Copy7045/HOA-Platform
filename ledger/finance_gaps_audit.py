"""Audit actions of the finance gaps (D75-D78), registered here rather than in
audit/actions.py so the work merges cleanly. Ids, counts, amounts and kinds
only: never names, addresses or free text (CLAUDE.md, Audit)."""

from __future__ import annotations

from audit import actions


def _spec(name: str, description: str, *fields: str) -> None:
    actions.register(actions.ActionSpec(name, description, frozenset(fields)))


# Special assessments (D75)
_spec(
    "special_assessment.levied",
    "Special assessment levied",
    "assessment_id",
    "amount_cents",
    "homes",
    "installments",
    "allocation",
    "fund",
    "vote_required",
)
_spec(
    "special_assessment.installments_posted",
    "Special assessment installments posted",
    "assessment_id",
    "installment",
    "charges",
    "amount_cents",
    "automatic",
)
_spec(
    "special_assessment.cancelled",
    "Special assessment's future installments cancelled",
    "assessment_id",
    "from_installment",
)
_spec(
    "special_assessment.notice_sent",
    "Special assessment notice sent",
    "assessment_id",
    "emailed",
    "paper",
)
_spec(
    "special_assessment.late_fees_posted",
    "Late fees posted on special assessment installments",
    "charges",
    "amount_cents",
    "automatic",
)
# Payment plans (D76)
_spec(
    "payment_plan.prepared",
    "Payment plan prepared",
    "plan_id",
    "case_id",
    "lot_id",
    "amount_cents",
    "installments",
)
_spec("payment_plan.approved", "Payment plan approved", "plan_id", "case_id", "lot_id")
_spec("payment_plan.declined", "Payment plan declined", "plan_id", "case_id")
_spec("payment_plan.acknowledged", "Payment plan acknowledged by the owner", "plan_id", "lot_id")
_spec("payment_plan.completed", "Payment plan completed", "plan_id", "case_id", "lot_id")
_spec("payment_plan.defaulted", "Payment plan defaulted", "plan_id", "case_id", "lot_id", "missed")
_spec("payment_plan.cancelled", "Payment plan cancelled", "plan_id", "case_id")
_spec("payment_plan.letter", "Payment plan agreement letter downloaded", "plan_id")
# Reimbursements (D77)
_spec(
    "reimbursement.submitted",
    "Expense reimbursement claim submitted",
    "claim_id",
    "amount_cents",
    "need",
)
_spec("reimbursement.approval_given", "Reimbursement approval given", "claim_id", "have", "need")
_spec("reimbursement.approved", "Reimbursement approved for payment", "claim_id", "invoice_id")
_spec("reimbursement.declined", "Reimbursement declined", "claim_id")
_spec("reimbursement.withdrawn", "Reimbursement withdrawn by the claimant", "claim_id")
_spec(
    "reimbursement.paid_by_transfer",
    "Reimbursement recorded as paid by bank transfer",
    "claim_id",
    "payment_id",
    "amount_cents",
)
# Vendor checks (D77)
_spec(
    "vendor_check.issued",
    "Vendor check recorded as issued",
    "check_id",
    "invoice_id",
    "payment_id",
    "amount_cents",
)
_spec("vendor_check.printed", "Vendor checks printed for check stock", "count")
_spec("vendor_check.cleared", "Vendor check cleared", "check_id", "amount_cents", "bank_line")
_spec("vendor_check.voided", "Vendor check voided", "check_id", "amount_cents", "reversal_id")
# Budget preparation (D78)
_spec(
    "budget.prepared",
    "Next year's budget prepared from this year's",
    "fiscal_year",
    "source_year",
    "lines",
    "basis",
)
