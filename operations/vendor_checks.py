"""Vendor checks (D77): paying approved invoices, and approved expense
reimbursements, by check on the association's pre-printed check stock.

* **Print** (``checks_pdf``): one check per page at the positions saved for
  the disbursement checks (``ledger.models.CheckPrintSettings``, the same
  stock), drawn by ``ledger.disbursements.check_page`` and
  ``core.pdf.render_positioned``: date, payee, amount in figures and words,
  the payee's address for a window envelope, and a memo with the invoice
  number. The stock carries the bank's numbers; the portal never knows them.
* **Issue** (``issue``): recording the checks as written takes the next
  numbers (unique across every association check, ``ledger.check_numbers``)
  and records, for each invoice, an ``InvoicePayment`` (method check, the
  number as reference) for what is still unpaid, with its ``VendorCheck``.
  The invoice becomes "Paid" as with any payment.
* **Clear** (``mark_cleared``) records the bank date (typed, or from a bank
  feed line of the same amount carrying the number); **void**
  (``void_check``, board only) records a negative ``InvoicePayment`` and
  reopens the invoice for a new payment.
* D66: a reimbursement check is never printed, issued, cleared or voided by
  the claimant, and one to a board member only by another board member
  (``operations.reimbursements.payment_refusal``).

``outstanding_checks(as_of)`` is the list the bank reconciliation needs (a
vendor check is also an ``InvoicePayment``, so the existing "payment" items
of ``ledger.accounting.uncleared`` already include it).
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from django.db import IntegrityError, transaction
from django.utils import timezone

import ledger.finance_gaps_audit  # noqa: F401  (registers the audit actions)
from audit.services import record_event
from core import pdf
from core.policy import SYSTEM, require
from ledger import check_numbers
from operations.models import (
    Invoice,
    InvoicePayment,
    VendorCheck,
    VendorCheckEvent,
    VendorCheckEventKind,
)


class VendorCheckError(ValueError):
    """A step the service refuses (shown on the page)."""


def _clean(text: str, limit: int) -> str:
    return " ".join((text or "").split())[:limit]


# --- What to pay ---------------------------------------------------------------------------


@dataclass
class Payable:
    invoice: Invoice
    remaining_cents: int
    payee: str
    mail_to: str
    memo: str
    claim: object = None
    refusal: str = ""


def payee_for(invoice: Invoice) -> tuple[str, str, str, object]:
    """(payee, mailing address, memo, reimbursement claim or None)."""
    from operations.reimbursements import claim_for_invoice, payee_and_address

    claim = claim_for_invoice(invoice)
    if claim is not None:
        name, address = payee_and_address(claim)
        return name, address, f"Reimbursement claim #{claim.pk}", claim
    vendor = invoice.vendor
    return vendor.name, vendor.address or "", f"Invoice {invoice.number}"[:60], None


def _refusal(actor, claim) -> str:
    if claim is None:
        return ""
    from operations.reimbursements import payment_refusal

    return payment_refusal(actor, claim)


def payables(actor=None) -> list[Payable]:
    """Approved invoices with money still to pay, oldest first."""
    from operations.services import paid_cents

    out = []
    for invoice in (
        Invoice.objects.filter(state="approved")
        .select_related("vendor")
        .order_by("due_date", "invoice_date", "id")
    ):
        remaining = invoice.amount_cents - paid_cents(invoice)
        if remaining <= 0:
            continue
        payee, mail_to, memo, claim = payee_for(invoice)
        out.append(
            Payable(
                invoice,
                remaining,
                payee,
                mail_to,
                memo,
                claim,
                _refusal(actor, claim) if actor is not None else "",
            )
        )
    return out


def _selected(invoice_ids, actor) -> list[Payable]:
    wanted = {int(i) for i in invoice_ids or []}
    rows = [p for p in payables(actor) if p.invoice.pk in wanted]
    if not rows or len(rows) != len(wanted):
        raise VendorCheckError("Choose approved invoices that still have money to pay.")
    for p in rows:
        if p.refusal:
            if p.claim is not None:
                from core import conflicts

                if conflicts.own_matter(actor, p.claim):
                    conflicts.audit_refusal(actor, p.claim, area="vendor_check", action="check")
            raise VendorCheckError(p.refusal)
    return rows


# --- Print and issue ------------------------------------------------------------------------


def checks_pdf(invoice_ids, *, on: datetime.date, actor, request=None) -> bytes:
    """The checks for these invoices, one per page, for the check stock."""
    from ledger.disbursements import check_page
    from ledger.models import CheckPrintSettings

    require(actor, "vendor_check.issue")
    rows = _selected(invoice_ids, actor)
    settings = CheckPrintSettings.get()
    pages = [
        check_page(
            settings,
            date=on,
            payee=p.payee,
            amount_cents=p.remaining_cents,
            memo=p.memo,
            address=p.mail_to,
            stub=f"{on.isoformat()} · {p.memo} · {p.invoice.category}"[:110],
        )
        for p in rows
    ]
    record_event(
        "vendor_check.printed", actor=actor, request=request, metadata={"count": len(rows)}
    )
    return pdf.render_positioned(pages, title="Checks")


def issue(
    invoice_ids, *, first_number: str, issued_on: datetime.date, actor, request=None
) -> list[VendorCheck]:
    """Record the checks written for these invoices, numbered from
    ``first_number`` in the order listed; each records the invoice payment."""
    from operations import services as ops

    require(actor, "vendor_check.issue")
    if issued_on > timezone.localdate():
        raise VendorCheckError("The date on the checks cannot be in the future.")
    rows = _selected(invoice_ids, actor)
    try:
        numbers = check_numbers.numbers_from(first_number, len(rows))
    except ValueError as exc:
        raise VendorCheckError(str(exc)) from exc
    out = []
    with transaction.atomic():
        check_numbers.lock()
        taken = check_numbers.taken(numbers)
        if taken:
            raise VendorCheckError(f"Check number {sorted(taken)[0]} is already recorded.")
        for p, number in zip(rows, numbers, strict=True):
            if issued_on < p.invoice.invoice_date:
                raise VendorCheckError(
                    f"The check for {p.memo} cannot be dated before the invoice."
                )
            try:
                payment = ops.record_payment(
                    p.invoice,
                    paid_on=issued_on,
                    method="check",
                    amount_cents=p.remaining_cents,
                    reference=number,
                    note=f"Check #{number}",
                    actor=actor,
                    request=request,
                )
            except ops.OperationsError as exc:
                raise VendorCheckError(str(exc)) from exc
            try:
                with transaction.atomic():
                    check = VendorCheck.objects.create(
                        invoice=p.invoice,
                        payment=payment,
                        number=number,
                        issued_on=issued_on,
                        amount_cents=p.remaining_cents,
                        payee_name=p.payee[:120],
                        mail_to=p.mail_to[:300],
                        memo=p.memo[:80],
                        issued_by=actor,
                    )
            except IntegrityError as exc:
                raise VendorCheckError(f"Check number {number} is already recorded.") from exc
            record_event(
                "vendor_check.issued",
                actor=actor,
                request=request,
                target=p.invoice,
                metadata={
                    "check_id": check.pk,
                    "invoice_id": p.invoice.pk,
                    "payment_id": payment.pk,
                    "amount_cents": check.amount_cents,
                },
            )
            out.append(check)
    return out


# --- Status, clear, void -------------------------------------------------------------------


def status(check: VendorCheck) -> str:
    event = VendorCheckEvent.objects.filter(check_record=check).first()
    return event.kind if event else "issued"


def _claim_refusal(actor, check: VendorCheck, action: str, request=None) -> None:
    from operations.reimbursements import claim_for_invoice, refuse_payment

    claim = claim_for_invoice(check.invoice)
    if claim is not None:
        try:
            refuse_payment(actor, claim, action, request)
        except ValueError as exc:
            raise VendorCheckError(str(exc)) from exc


def _event_date(on, check: VendorCheck, what: str) -> datetime.date:
    on = on or timezone.localdate()
    if on > timezone.localdate():
        raise VendorCheckError(f"The {what} date cannot be in the future.")
    if on < check.issued_on:
        raise VendorCheckError(f"The {what} date cannot be before the check was issued.")
    return on


def mark_cleared(check: VendorCheck, *, on=None, bank_transaction=None, actor, request=None):
    require(actor, "vendor_check.issue")
    _claim_refusal(actor, check, "clear", request)
    on = _event_date(on, check, "cleared")
    if bank_transaction is not None and bank_transaction.amount_cents != -check.amount_cents:
        raise VendorCheckError("That bank line is not this check's amount.")
    with transaction.atomic():
        check_numbers.lock()
        if status(check) != "issued":
            raise VendorCheckError("Only an issued check can be marked cleared.")
        event = VendorCheckEvent.objects.create(
            check_record=check,
            kind=VendorCheckEventKind.CLEARED,
            on=on,
            bank_transaction=bank_transaction,
            recorded_by=actor,
        )
    record_event(
        "vendor_check.cleared",
        actor=actor,
        request=request,
        target=check.invoice,
        metadata={
            "check_id": check.pk,
            "amount_cents": check.amount_cents,
            "bank_line": bank_transaction.pk if bank_transaction is not None else None,
        },
    )
    return event


def void_check(check: VendorCheck, *, reason: str, actor, request=None):
    """Void an issued, uncleared check (board only): a negative payment
    reverses it and the invoice waits for payment again."""
    from operations.services import INVOICE

    require(actor, "vendor_check.void")
    _claim_refusal(actor, check, "void", request)
    reason = _clean(reason, 150)
    if not reason:
        raise VendorCheckError("Give the reason for voiding the check.")
    today = timezone.localdate()
    with transaction.atomic():
        check_numbers.lock()
        if status(check) != "issued":
            raise VendorCheckError("Only an issued check that has not cleared can be voided.")
        reversal = InvoicePayment.objects.create(
            invoice=check.invoice,
            paid_on=today,
            method="check",
            reference=check.number,
            amount_cents=-check.amount_cents,
            note=f"Check #{check.number} voided: {reason}"[:200],
            recorded_by=actor,
        )
        event = VendorCheckEvent.objects.create(
            check_record=check,
            kind=VendorCheckEventKind.VOIDED,
            on=today,
            reason=reason,
            reversal=reversal,
            recorded_by=actor,
        )
        invoice = Invoice.objects.get(pk=check.invoice_id)
        if invoice.state == "paid":
            INVOICE.apply(
                invoice, "payment_voided", actor=SYSTEM, note=f"Check #{check.number} voided"
            )
    record_event(
        "vendor_check.voided",
        actor=actor,
        request=request,
        target=check.invoice,
        metadata={
            "check_id": check.pk,
            "amount_cents": check.amount_cents,
            "reversal_id": reversal.pk,
        },
    )
    return event


# --- Register, reconciliation, bank feed ----------------------------------------------------


def register(limit: int = 300) -> list[dict]:
    events = {e.check_record_id: e for e in VendorCheckEvent.objects.all()}
    out = []
    for check in VendorCheck.objects.select_related("invoice__vendor", "invoice__category")[:limit]:
        event = events.get(check.pk)
        out.append(
            {
                "check": check,
                "status": event.kind if event else "issued",
                "event": event,
                "refusal": "",
            }
        )
    return out


@dataclass(frozen=True)
class OutstandingCheck:
    """A vendor check as a cash-book payment, for bank reconciliation."""

    pk: int
    payment_id: int
    number: str
    issued_on: datetime.date
    amount_cents: int
    payee: str
    cleared_on: datetime.date | None


def outstanding_checks(
    as_of: datetime.date, since: datetime.date | None = None
) -> list[OutstandingCheck]:
    """Vendor checks issued through ``as_of`` (after ``since``), not voided by
    then and not reconciled (their ``InvoicePayment`` not on a statement).
    For the books branch's reconciliation and the Checks page."""
    from ledger.models import ReconciledItem

    reconciled = set(
        ReconciledItem.objects.filter(kind="payment").values_list("object_id", flat=True)
    )
    events = {e.check_record_id: e for e in VendorCheckEvent.objects.filter(on__lte=as_of)}
    rows = VendorCheck.objects.filter(issued_on__lte=as_of)
    if since is not None:
        rows = rows.filter(issued_on__gt=since)
    out = []
    for c in rows.order_by("issued_on", "id"):
        event = events.get(c.pk)
        if c.payment_id in reconciled or (event and event.kind == "voided"):
            continue
        cleared = event.on if event and event.kind == "cleared" else None
        out.append(
            OutstandingCheck(
                c.pk, c.payment_id, c.number, c.issued_on, c.amount_cents, c.payee_name, cleared
            )
        )
    return out


def clearing_suggestions(checks) -> dict[int, object]:
    """For each issued check, an unmatched outgoing bank feed line of the same
    amount whose description carries the number (bank-feed hook, kind
    "check" as for disbursement checks)."""
    from core.scoping import system_access
    from ledger.bankfeeds.matching import unmatched_lines
    from ledger.disbursements import bank_line_fits

    open_checks = [c for c in checks if status(c) == "issued"]
    if not open_checks:
        return {}
    with system_access("bank feed for vendor checks"):
        lines = [line for line in unmatched_lines(limit=500) if line.amount_cents < 0]
    out, used = {}, set()
    for check in open_checks:
        for line in lines:
            if line.pk not in used and bank_line_fits(line, check):
                out[check.pk] = line
                used.add(line.pk)
                break
    return out
