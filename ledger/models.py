"""Owner accounts (one per lot), budgets, collections and resale certificates.

Ledger entries are append-only integer cents (CLAUDE.md rules 5 and 7):
positive amounts are charges (the lot owes more), negative amounts are
payments and credits. The balance is the sum. A prepayment simply leaves the
balance negative, and each monthly assessment draws it down. Mistakes are
corrected with a reversing entry.
"""

from django.conf import settings
from django.db import models
from django.utils import timezone

from core.append_only import ScopedAppendOnlyModel
from core.scoping import ScopedModel, ScopedQuerySet


class EntryKind(models.TextChoices):
    ASSESSMENT = "assessment", "Monthly assessment"
    LATE_FEE = "late_fee", "Late fee"
    OTHER_CHARGE = "other_charge", "Other charge"
    RESALE_FEE = "resale_fee", "Resale certificate fee"
    PAYMENT = "payment", "Payment received"
    CREDIT = "credit", "Credit / adjustment"
    REVERSAL = "reversal", "Reversal"


CHARGE_KINDS = {
    EntryKind.ASSESSMENT,
    EntryKind.LATE_FEE,
    EntryKind.OTHER_CHARGE,
    EntryKind.RESALE_FEE,
}


class LedgerEntry(ScopedAppendOnlyModel):
    scope_lot_path = "lot"

    lot = models.ForeignKey("community.Lot", on_delete=models.PROTECT, related_name="+")
    posted_on = models.DateField()
    kind = models.CharField(max_length=16, choices=EntryKind.choices)
    amount_cents = models.BigIntegerField()
    description = models.CharField(max_length=200)
    reference = models.CharField(max_length=40, blank=True)
    period = models.CharField(max_length=7, blank=True, help_text="YYYY-MM for assessments")
    reverses = models.OneToOneField(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="reversed_by"
    )
    posted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["posted_on", "id"]
        indexes = [models.Index(fields=["lot", "posted_on"])]
        constraints = [
            models.UniqueConstraint(
                fields=["lot", "period"],
                condition=models.Q(kind="assessment"),
                name="one_assessment_per_lot_per_month",
            ),
            models.CheckConstraint(
                condition=~models.Q(amount_cents=0), name="ledger_entry_nonzero"
            ),
        ]

    def __str__(self) -> str:
        return f"entry #{self.pk}"


# --- Budgets ---------------------------------------------------------------------------


class BudgetStatus(models.TextChoices):
    DRAFT = "draft", "Draft"
    ADOPTED = "adopted", "Adopted"


class Budget(models.Model):
    fiscal_year = models.PositiveSmallIntegerField(unique=True)
    status = models.CharField(
        max_length=8, choices=BudgetStatus.choices, default=BudgetStatus.DRAFT
    )
    adopted_on = models.DateField(null=True, blank=True)
    adopted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-fiscal_year"]

    def __str__(self) -> str:
        return f"FY{self.fiscal_year} budget"

    def delete(self, *args, **kwargs):
        raise PermissionError("Budgets are kept.")


class LineKind(models.TextChoices):
    INCOME = "income", "Income"
    EXPENSE = "expense", "Expense"


class IncomeSource(models.TextChoices):
    ASSESSMENTS = "assessments", "Assessments"
    RESALE_FEES = "resale_fees", "Resale certificate fees"
    OTHER = "other", "Other income"


class BudgetLine(models.Model):
    budget = models.ForeignKey(Budget, on_delete=models.PROTECT, related_name="lines")
    kind = models.CharField(max_length=8, choices=LineKind.choices)
    name = models.CharField(max_length=120)
    category = models.ForeignKey(
        "operations.ExpenseCategory",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
        help_text="Expense lines: actuals come from invoices.",
    )
    income_source = models.CharField(
        max_length=16,
        choices=IncomeSource.choices,
        blank=True,
        help_text="Income lines: actuals come from the ledger.",
    )
    annual_cents = models.BigIntegerField()

    class Meta:
        ordering = ["kind", "name"]

    def __str__(self) -> str:
        return self.name

    def monthly_cents(self) -> list[int]:
        """Even monthly spread; any remainder lands in the last month."""
        base = self.annual_cents // 12
        months = [base] * 12
        months[-1] += self.annual_cents - base * 12
        return months


# --- Collections (tracking only) ------------------------------------------------------


class CollectionCase(ScopedModel):
    scope_lot_path = "lot"
    objects = ScopedQuerySet.as_manager()

    lot = models.ForeignKey("community.Lot", on_delete=models.PROTECT, related_name="+")
    state = models.CharField(max_length=24, default="open")
    state_changed_at = models.DateTimeField(default=timezone.now)
    opened_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    opened_at = models.DateTimeField(default=timezone.now)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-opened_at"]

    def __str__(self) -> str:
        return f"Collection case #{self.pk}"

    def delete(self, *args, **kwargs):
        raise PermissionError("Collection cases are closed, not deleted.")


# --- Resale certificates --------------------------------------------------------------


class ResaleRequest(ScopedModel):
    scope_lot_path = "lot"
    objects = ScopedQuerySet.as_manager()

    lot = models.ForeignKey("community.Lot", on_delete=models.PROTECT, related_name="+")
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    requester_name = models.CharField(max_length=120, help_text="Seller, buyer or agent")
    requester_email = models.EmailField(blank=True)
    expected_settlement = models.DateField(null=True, blank=True)
    state = models.CharField(max_length=16, default="submitted")
    state_changed_at = models.DateTimeField(default=timezone.now)
    fee_cents = models.BigIntegerField(null=True, blank=True)
    due_on = models.DateField(null=True, blank=True)
    other_disclosures = models.TextField(
        blank=True, help_text="Board statements required by law not held elsewhere in the portal"
    )
    created_at = models.DateTimeField(default=timezone.now)
    issued_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Resale #{self.pk}"

    def delete(self, *args, **kwargs):
        raise PermissionError("Resale requests are cancelled, not deleted.")
