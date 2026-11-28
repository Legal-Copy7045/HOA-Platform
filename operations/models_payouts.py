"""Expense reimbursements and vendor checks (D77).

Imported at the bottom of ``operations/models.py`` so Django registers them
in the ``operations`` app. Append-only throughout (CLAUDE.md rule 5): a
claim is submitted once, each decision, payment, check and check event is
its own row, and the state is derived from them.

Money reaches the books the way every vendor payment does: an approved
claim becomes an ``Invoice`` in the claim's expense category (state
"approved", its approvals copied from the claim's), and paying it records an
``InvoicePayment`` (``ledger.accounting.journal_rows`` books it to that
category). A check printed on the association's stock is a ``VendorCheck``
on that payment; voiding it records a negative ``InvoicePayment``.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils import timezone

from core.append_only import AppendOnlyModel, ScopedAppendOnlyModel

# --- Expense reimbursements -----------------------------------------------------------


class ReimbursementClaim(ScopedAppendOnlyModel):
    """Someone paid an association expense out of pocket and asks to be paid
    back. Scoped to the claimant (a committee member sees only
    their own claims; the board and manager see all)."""

    scope_person_path = "claimant"
    conflict_noun = "reimbursement claim"  # own matters (D65)

    claimant = models.ForeignKey("community.Person", on_delete=models.PROTECT, related_name="+")
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    amount_cents = models.BigIntegerField()
    spent_on = models.DateField()
    payee = models.CharField(max_length=120, help_text="The shop or vendor that was paid.")
    category = models.ForeignKey(
        "operations.ExpenseCategory", on_delete=models.PROTECT, related_name="+"
    )
    description = models.TextField()
    # Approvals needed, fixed when submitted (rule reimbursement.second_approval_cents).
    required_approvals = models.PositiveSmallIntegerField(default=1)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount_cents__gt=0), name="reimbursement_positive"
            )
        ]

    def __str__(self) -> str:
        return f"reimbursement claim #{self.pk}"


class ReimbursementDecisionKind(models.TextChoices):
    APPROVE = "approve", "Approved"
    DECLINE = "decline", "Declined"
    WITHDRAW = "withdraw", "Withdrawn by the claimant"


class ReimbursementDecision(AppendOnlyModel):
    claim = models.ForeignKey(
        ReimbursementClaim, on_delete=models.PROTECT, related_name="decisions"
    )
    decision = models.CharField(max_length=8, choices=ReimbursementDecisionKind.choices)
    by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    amount_cents = models.BigIntegerField()  # what the approver saw
    note = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["created_at", "id"]
        constraints = [
            models.UniqueConstraint(fields=["claim", "by"], name="reimbursement_decision_once")
        ]

    def __str__(self) -> str:
        return f"reimbursement decision #{self.pk}"


class ReimbursementInvoice(AppendOnlyModel):
    """The invoice an approved claim became (paid like any approved invoice)."""

    claim = models.OneToOneField(
        ReimbursementClaim, on_delete=models.PROTECT, related_name="invoice_link"
    )
    invoice = models.OneToOneField(
        "operations.Invoice", on_delete=models.PROTECT, related_name="reimbursement"
    )
    created_at = models.DateTimeField(default=timezone.now)

    def __str__(self) -> str:
        return f"reimbursement invoice #{self.pk}"


# --- Vendor checks (D77) ------------------------------------------------------------------


class VendorCheck(AppendOnlyModel):
    """A check printed on the association's pre-printed stock (or written by
    hand) for an invoice: recorded when issued, with the ``InvoicePayment``
    it made. Check numbers are unique across every association check
    (``ledger.check_numbers``)."""

    invoice = models.ForeignKey(
        "operations.Invoice", on_delete=models.PROTECT, related_name="checks"
    )
    payment = models.OneToOneField(
        "operations.InvoicePayment", on_delete=models.PROTECT, related_name="check_record"
    )
    number = models.CharField(max_length=20)
    issued_on = models.DateField()
    amount_cents = models.BigIntegerField()
    payee_name = models.CharField(max_length=120)
    mail_to = models.CharField(max_length=300, blank=True)
    memo = models.CharField(max_length=80, blank=True)
    issued_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-issued_on", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["number"], name="vendor_check_number_unique"),
            models.CheckConstraint(
                condition=models.Q(amount_cents__gt=0), name="vendor_check_positive"
            ),
        ]

    def __str__(self) -> str:
        return f"check #{self.number}"


class VendorCheckEventKind(models.TextChoices):
    CLEARED = "cleared", "Cleared"
    VOIDED = "voided", "Voided"


class VendorCheckEvent(AppendOnlyModel):
    check_record = models.ForeignKey(VendorCheck, on_delete=models.PROTECT, related_name="events")
    kind = models.CharField(max_length=8, choices=VendorCheckEventKind.choices)
    on = models.DateField()
    reason = models.CharField(max_length=200, blank=True)
    bank_transaction = models.ForeignKey(
        "ledger.BankTransaction", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    # The negative InvoicePayment a void recorded.
    reversal = models.OneToOneField(
        "operations.InvoicePayment",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["created_at", "id"]
        constraints = [
            models.UniqueConstraint(fields=["check_record"], name="vendor_check_one_event")
        ]

    def __str__(self) -> str:
        return f"vendor check event #{self.pk}"
