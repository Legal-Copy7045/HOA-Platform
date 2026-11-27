"""Expense reimbursements (D77).

A board member, committee member or volunteer owner who paid an association
expense out of pocket submits a claim: the amount, the date, who was paid,
the expense category, what it was for and the receipt (one or more files,
through the safe upload path with the malware scan, ``documents.attachments``).

* **Approval**: a board member other than the claimant (own matters, D65;
  the manager never approves money). Two board members at or over rule
  ``reimbursement.second_approval_cents`` (fixed on the claim when
  submitted). A claim is approved only while at least one receipt passes
  the scan rule (``documents.services.scan_allows_download``). Any approver
  may decline it with a reason; the claimant may withdraw it while it waits.
* **Books**: the final approval turns the claim into an ``Invoice`` in the
  claim's category, from the system payee "Expense reimbursements" (number
  ``R-<claim>``), already approved, with the claim's approvals copied as its
  ``InvoiceApproval`` rows. Paying it records an ``InvoicePayment``, so the
  journal books it to the category like any vendor payment; the 1099
  worksheet leaves it out.
* **Paying** (D66): by check printed on the association's stock
  (``operations.vendor_checks``) or recorded as paid by bank transfer
  (``pay_by_transfer``, a short reference only, never bank numbers). The
  claimant never takes a step on their own payment, and a payment to a
  board member is processed by another board member, never the manager.
"""

from __future__ import annotations

import datetime

from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone

import ledger.finance_gaps_audit  # noqa: F401  (registers the audit actions)
from audit.services import record_event
from community.rules import rule
from core import conflicts
from core.money import format_cents
from core.policy import BOARD, SYSTEM, can, require, roles_for
from core.scoping import system_access
from operations.models import (
    ApprovalDecision,
    Invoice,
    InvoiceApproval,
    ReimbursementClaim,
    ReimbursementDecision,
    ReimbursementDecisionKind,
    ReimbursementInvoice,
    Vendor,
)

SECOND_APPROVAL_RULE = "reimbursement.second_approval_cents"
PAYEE_NAME = "Expense reimbursements"
RECEIPT_LABEL = "Receipt"
MAX_CENTS = 10_000_000  # $100,000: anything above is a typing mistake


class ReimbursementError(ValueError):
    """A step the service refuses (shown on the page)."""


def _clean(text: str, limit: int) -> str:
    return " ".join((text or "").split())[:limit]


def approvals_needed(amount_cents: int) -> int:
    threshold = rule(SECOND_APPROVAL_RULE)
    return 2 if threshold is not None and amount_cents >= threshold else 1


# --- State -----------------------------------------------------------------------------


def _decisions(claim) -> list[ReimbursementDecision]:
    return list(ReimbursementDecision.objects.filter(claim=claim))


def invoice_for(claim) -> Invoice | None:
    link = ReimbursementInvoice.objects.filter(claim=claim).select_related("invoice").first()
    return link.invoice if link else None


def state(claim) -> dict:
    """status: awaiting, declined, withdrawn, approved (to pay), paid."""
    from operations.services import paid_cents

    decisions = _decisions(claim)
    approvers = [d for d in decisions if d.decision == ReimbursementDecisionKind.APPROVE]
    out = {
        "have": len(approvers),
        "need": claim.required_approvals,
        "approvers": {d.by_id for d in approvers},
        "decisions": decisions,
        "invoice": None,
        "paid_cents": 0,
    }
    if any(d.decision == ReimbursementDecisionKind.WITHDRAW for d in decisions):
        return {**out, "status": "withdrawn"}
    if any(d.decision == ReimbursementDecisionKind.DECLINE for d in decisions):
        return {**out, "status": "declined"}
    invoice = invoice_for(claim)
    if invoice is None:
        return {**out, "status": "awaiting"}
    paid = paid_cents(invoice)
    return {
        **out,
        "invoice": invoice,
        "paid_cents": paid,
        "status": "paid" if paid >= claim.amount_cents else "approved",
    }


STATUS_LABELS = {
    "awaiting": "Waiting for approval",
    "declined": "Declined",
    "withdrawn": "Withdrawn",
    "approved": "Approved, to be paid",
    "paid": "Paid",
}


# --- Receipts ----------------------------------------------------------------------------


def receipts(claim):
    from documents.attachments import attachments_for

    return attachments_for(claim)


def has_clean_receipt(claim) -> bool:
    from documents.attachments import current_scan
    from documents.services import scan_allows_download

    return any(scan_allows_download(current_scan(a)) for a in receipts(claim))


# --- Submit, withdraw --------------------------------------------------------------------


def submit(
    *,
    amount_cents: int,
    spent_on: datetime.date,
    payee: str,
    category,
    description: str,
    files,
    actor,
    request=None,
) -> ReimbursementClaim:
    """Submit a claim with its receipt(s). Each file is checked and scanned
    on upload; a file the scanner flags is refused and nothing is kept."""
    from community.services import _refuse_read_only
    from documents.attachments import attach
    from documents.services import DocumentError

    require(actor, "reimbursement.submit")
    _refuse_read_only(actor)
    person_id = roles_for(actor).person_id
    if person_id is None:
        raise ReimbursementError("Only someone in the community register can claim.")
    payee = _clean(payee, 120)
    description = (description or "").strip()[:2000]
    if not payee or not description:
        raise ReimbursementError("Say who was paid and what the expense was for.")
    if not isinstance(amount_cents, int) or not 0 < amount_cents <= MAX_CENTS:
        raise ReimbursementError("Enter the amount paid, more than zero.")
    if spent_on > timezone.localdate():
        raise ReimbursementError("The expense date cannot be in the future.")
    if category is None or not category.active:
        raise ReimbursementError("Choose the expense category.")
    files = [f for f in (files or []) if f]
    if not files:
        raise ReimbursementError("Attach the receipt (a PDF or a photo).")
    if len(files) > 5:
        raise ReimbursementError("Attach at most 5 files.")
    try:
        with transaction.atomic():
            claim = ReimbursementClaim.objects.create(
                claimant_id=person_id,
                submitted_by=actor,
                amount_cents=amount_cents,
                spent_on=spent_on,
                payee=payee,
                category=category,
                description=description,
                required_approvals=approvals_needed(amount_cents),
            )
            for f in files:
                attach(claim, f, actor=actor, label=RECEIPT_LABEL, request=request)
    except DocumentError as exc:
        raise ReimbursementError(f"The receipt was not accepted: {exc}") from exc
    record_event(
        "reimbursement.submitted",
        actor=actor,
        request=request,
        target=claim,
        metadata={
            "claim_id": claim.pk,
            "amount_cents": amount_cents,
            "need": claim.required_approvals,
        },
    )
    return claim


def withdraw(claim, *, actor, request=None) -> ReimbursementDecision:
    if claim.submitted_by_id != actor.pk:
        raise ReimbursementError("Only the person who submitted the claim can withdraw it.")
    with transaction.atomic():
        ReimbursementClaim.objects.unscoped("claim lock").select_for_update().get(pk=claim.pk)
        if state(claim)["status"] != "awaiting":
            raise ReimbursementError("Only a claim waiting for approval can be withdrawn.")
        row = ReimbursementDecision.objects.create(
            claim=claim,
            decision=ReimbursementDecisionKind.WITHDRAW,
            by=actor,
            amount_cents=claim.amount_cents,
        )
    record_event(
        "reimbursement.withdrawn",
        actor=actor,
        request=request,
        target=claim,
        metadata={"claim_id": claim.pk},
    )
    return row


# --- Approve, decline ---------------------------------------------------------------------


def _refuse_own(actor, claim, action: str, request=None) -> None:
    conflicts.refuse(
        actor,
        claim,
        area="reimbursement",
        action=action,
        request=request,
        error=ReimbursementError,
        message="This is your own claim: other board members approve and pay it.",
    )


def _payee_vendor() -> Vendor:
    vendor = Vendor.objects.filter(name=PAYEE_NAME, services="reimbursements").first()
    if vendor is None:
        vendor = Vendor.objects.create(
            name=PAYEE_NAME,
            services="reimbursements",
            active=False,
            notes="System payee for expense reimbursements (D77). Not a 1099 vendor.",
        )
    return vendor


def approve(claim, *, note: str = "", actor, request=None) -> dict:
    """One approval; the last one needed turns the claim into an approved
    invoice ready to pay."""
    require(actor, "reimbursement.approve")
    _refuse_own(actor, claim, "approve", request)
    if claim.submitted_by_id == actor.pk:
        raise ReimbursementError("You submitted this claim; other board members approve it.")
    if not has_clean_receipt(claim):
        raise ReimbursementError(
            "No receipt has passed the malware scan yet; approve once one has."
        )
    with transaction.atomic():
        ReimbursementClaim.objects.unscoped("claim lock").select_for_update().get(pk=claim.pk)
        st = state(claim)
        if st["status"] != "awaiting":
            raise ReimbursementError("Only a claim waiting for approval can be approved.")
        if actor.pk in st["approvers"]:
            raise ReimbursementError(
                "You have already approved this claim; a different board member gives the "
                "second approval."
            )
        try:
            with transaction.atomic():
                ReimbursementDecision.objects.create(
                    claim=claim,
                    decision=ReimbursementDecisionKind.APPROVE,
                    by=actor,
                    amount_cents=claim.amount_cents,
                    note=_clean(note, 500),
                )
        except IntegrityError as exc:
            raise ReimbursementError("You have already decided this claim.") from exc
        have = st["have"] + 1
        record_event(
            "reimbursement.approval_given",
            actor=actor,
            request=request,
            target=claim,
            metadata={"claim_id": claim.pk, "have": have, "need": claim.required_approvals},
        )
        if have >= claim.required_approvals:
            invoice = Invoice.objects.create(
                vendor=_payee_vendor(),
                number=f"R-{claim.pk}",
                invoice_date=claim.spent_on,
                amount_cents=claim.amount_cents,
                description=f"Reimbursement claim #{claim.pk}: {claim.payee}"[:500],
                category=claim.category,
                state="approved",
                required_approvals=claim.required_approvals,
                entered_by=claim.submitted_by,
            )
            for d in ReimbursementDecision.objects.filter(
                claim=claim, decision=ReimbursementDecisionKind.APPROVE
            ):
                InvoiceApproval.objects.create(
                    invoice=invoice,
                    approver_id=d.by_id,
                    decision=ApprovalDecision.APPROVE,
                    amount_cents=claim.amount_cents,
                    note=f"Reimbursement claim #{claim.pk} approved",
                )
            ReimbursementInvoice.objects.create(claim=claim, invoice=invoice)
            record_event(
                "reimbursement.approved",
                actor=actor,
                request=request,
                target=claim,
                metadata={"claim_id": claim.pk, "invoice_id": invoice.pk},
            )
    return state(claim)


def decline(claim, *, note: str, actor, request=None) -> dict:
    require(actor, "reimbursement.approve")
    _refuse_own(actor, claim, "decline", request)
    note = _clean(note, 500)
    if not note:
        raise ReimbursementError("Say why the claim is declined.")
    with transaction.atomic():
        ReimbursementClaim.objects.unscoped("claim lock").select_for_update().get(pk=claim.pk)
        st = state(claim)
        if st["status"] != "awaiting" or actor.pk in st["approvers"]:
            raise ReimbursementError("Only a claim waiting for your decision can be declined.")
        ReimbursementDecision.objects.create(
            claim=claim,
            decision=ReimbursementDecisionKind.DECLINE,
            by=actor,
            amount_cents=claim.amount_cents,
            note=note,
        )
    record_event(
        "reimbursement.declined",
        actor=actor,
        request=request,
        target=claim,
        metadata={"claim_id": claim.pk},
    )
    return state(claim)


# --- Paying ----------------------------------------------------------------------------


def claim_for_invoice(invoice) -> ReimbursementClaim | None:
    link = ReimbursementInvoice.objects.filter(invoice=invoice).first()
    if link is None:
        return None
    with system_access("claim of an invoice"):
        return ReimbursementClaim.objects.select_related("claimant").get(pk=link.claim_id)


def _board_person_ids() -> set[int]:
    from community.models import BoardSeat

    with system_access("board people"):
        return set(BoardSeat.objects.active().values_list("person_id", flat=True))


def payment_refusal(actor, claim) -> str:
    """Why ``actor`` may not pay this claim ("" when they may): never one's
    own claim; a claim of a board member only by another board member,
    never the manager (D66)."""
    if actor is SYSTEM:
        return ""
    roles = roles_for(actor)
    if claim.submitted_by_id == actor.pk or (
        roles.person_id is not None and roles.person_id == claim.claimant_id
    ):
        return "This is your own claim: another board member pays it."
    if claim.claimant_id in _board_person_ids() and BOARD not in roles.groups:
        return "This claim pays a board member; another board member processes it."
    return ""


def refuse_payment(actor, claim, action: str, request=None) -> None:
    reason = payment_refusal(actor, claim)
    if reason:
        if conflicts.own_matter(actor, claim):
            conflicts.audit_refusal(
                actor, claim, area="reimbursement", action=action, request=request
            )
        raise ReimbursementError(reason)


def pay_by_transfer(claim, *, paid_on: datetime.date, reference: str, actor, request=None):
    """Record that the claim was paid by bank transfer (reference only)."""
    from operations import services as ops

    require(actor, "reimbursement.pay")
    refuse_payment(actor, claim, "pay", request)
    reference = _clean(reference, 40)
    if not reference:
        raise ReimbursementError("Give the transfer's reference (never an account number).")
    if paid_on > timezone.localdate():
        raise ReimbursementError("The payment date cannot be in the future.")
    st = state(claim)
    if st["status"] != "approved":
        raise ReimbursementError("Only an approved, unpaid claim can be paid.")
    invoice = st["invoice"]
    try:
        payment = ops.record_payment(
            invoice,
            paid_on=paid_on,
            method="ach",
            amount_cents=claim.amount_cents - st["paid_cents"],
            reference=reference,
            note=f"Reimbursement claim #{claim.pk}",
            actor=actor,
            request=request,
        )
    except ops.OperationsError as exc:
        raise ReimbursementError(str(exc)) from exc
    record_event(
        "reimbursement.paid_by_transfer",
        actor=actor,
        request=request,
        target=claim,
        metadata={
            "claim_id": claim.pk,
            "payment_id": payment.pk,
            "amount_cents": payment.amount_cents,
        },
    )
    return payment


def payee_and_address(claim) -> tuple[str, str]:
    """Who the check is made out to and where it is mailed: the claimant,
    at their postal address (``community.services.postal_address``)."""
    from community.services import postal_address

    with system_access("reimbursement payee"):
        person = claim.claimant
        address, _source = postal_address(person)
    return person.display_name, address


# --- Lists and My items -------------------------------------------------------------------


def rows(queryset) -> list[dict]:
    out = []
    for claim in queryset.select_related("claimant", "category"):
        out.append({"claim": claim, "state": state(claim)})
    return out


def board_items(user) -> list:
    """Claims waiting for this board member's approval; approved claims this
    person may pay."""
    from core.my_items import Item

    items = []
    approver = can(user, "reimbursement.approve")
    payer = can(user, "reimbursement.pay")
    if not (approver or payer):
        return items
    with system_access("reimbursement items"):
        claims = list(ReimbursementClaim.objects.select_related("claimant")[:200])
    for claim in claims:
        if conflicts.own_matter(user, claim) or claim.submitted_by_id == user.pk:
            continue
        st = state(claim)
        url = reverse("reimbursement_detail", args=[claim.pk])
        if st["status"] == "awaiting" and approver and user.pk not in st["approvers"]:
            items.append(
                Item(
                    "Finance",
                    f"Approve a reimbursement of {format_cents(claim.amount_cents)}",
                    f"{claim.payee} · {claim.spent_on:%B %-d}"
                    + (f" · {st['have']} of {st['need']} approvals" if st["need"] > 1 else ""),
                    url,
                    "warning",
                    nav="reimbursements",
                )
            )
        elif st["status"] == "approved" and payer and not payment_refusal(user, claim):
            items.append(
                Item(
                    "Finance",
                    f"Pay an approved reimbursement of {format_cents(claim.amount_cents)}",
                    "By check from Checks, or record a bank transfer.",
                    url,
                    "info",
                    nav="reimbursements",
                )
            )
    return items


def claimant_items(user) -> list:
    """The claimant's own claims that are waiting or approved but unpaid."""
    from core.my_items import Item

    person_id = roles_for(user).person_id
    if person_id is None or not can(user, "reimbursement.submit"):
        return []
    items = []
    for claim in ReimbursementClaim.objects.for_user(user).filter(claimant_id=person_id)[:50]:
        st = state(claim)
        if st["status"] not in ("awaiting", "approved"):
            continue
        items.append(
            Item(
                "My home",
                f"Your reimbursement of {format_cents(claim.amount_cents)}: "
                + STATUS_LABELS[st["status"]].lower(),
                f"{claim.payee} · {claim.spent_on:%B %-d}",
                reverse("reimbursement_detail", args=[claim.pk]),
                "info",
                nav="reimbursements?mine=1",
            )
        )
    return items
