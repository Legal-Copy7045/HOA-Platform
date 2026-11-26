"""Owner accounts (one per lot), budgets, collections and resale certificates.

Ledger entries are append-only integer cents (CLAUDE.md rules 5 and 7):
positive amounts are charges (the lot owes more), negative amounts are
payments and credits. The balance is the sum. A prepayment simply leaves the
balance negative, and each monthly assessment draws it down. Mistakes are
corrected with a reversing entry.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils import timezone

from core.append_only import AppendOnlyModel, ScopedAppendOnlyModel
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
            models.UniqueConstraint(
                fields=["lot", "period"],
                condition=models.Q(kind="late_fee") & ~models.Q(period=""),
                name="one_late_fee_per_lot_per_month",
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
    # Owner notices (Milestone 4): the proposed budget before the meeting, and
    # the adopted budget with the new dues afterwards.
    proposed_notice_sent_at = models.DateTimeField(null=True, blank=True)
    adopted_notice_sent_at = models.DateTimeField(null=True, blank=True)

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
    follow_up_on = models.DateField(null=True, blank=True)

    class Meta:
        ordering = ["-opened_at"]
        constraints = [
            # One open case per lot, even under concurrent requests (review D10).
            models.UniqueConstraint(
                fields=["lot"],
                condition=~models.Q(state="closed"),
                name="collection_one_open_case_per_lot",
            )
        ]

    def __str__(self) -> str:
        return f"Collection case #{self.pk}"

    def delete(self, *args, **kwargs):
        raise PermissionError("Collection cases are closed, not deleted.")


# --- Resale certificates --------------------------------------------------------------


class ResaleRequest(ScopedModel):
    scope_lot_path = "lot"

    @classmethod
    def scope_for_owner(cls, queryset, roles):
        from core.scoping import owner_era_q

        return queryset.filter(owner_era_q(roles))

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


# --- Letter templates (Milestone 4) ------------------------------------------------------


class LetterKind(models.TextChoices):
    STATEMENT = "statement", "Account statement message"
    FIRST = "first", "First delinquency notice"
    SECOND = "second", "Second delinquency notice"
    FINAL = "final", "Final notice before referral"


class LetterTemplate(models.Model):
    """Board-edited wording for statements and delinquency notices. Plain
    text with {placeholders}; nothing else is interpreted."""

    kind = models.CharField(max_length=10, choices=LetterKind.choices, primary_key=True)
    subject = models.CharField(max_length=200, blank=True)  # the "RE:" line
    body = models.TextField()
    certified = models.BooleanField(default=False)  # "CERTIFIED AND REGULAR MAIL"
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    updated_at = models.DateTimeField(default=timezone.now)

    def __str__(self) -> str:
        return self.get_kind_display()


# --- Money tools (Milestone 4 part 3) ---------------------------------------------------

DEFAULT_GL = (
    "cash = 1000 Operating cash\n"
    "receivable = 1200 Owner receivables\n"
    "assessment = 4000 Assessment income\n"
    "late_fee = 4100 Late fee income\n"
    "resale_fee = 4200 Resale certificate fees\n"
    "other_charge = 4900 Other owner charges\n"
    "credit = 4950 Owner credits and adjustments"
)
# Online payments (D32-payments): added to every setup without changing the
# stored default above.
ONLINE_GL = {
    "stripe_clearing": "1050 Stripe clearing",
    "card_surcharge": "4300 Credit card surcharge income",
    "processing_fees": "6150 Payment processing fees",
}


class AccountingSetup(models.Model):
    """One row: chart-of-accounts mapping for the export, and the bank
    balance the cash book starts from."""

    gl_accounts = models.TextField(default=DEFAULT_GL)
    opening_cash_cents = models.BigIntegerField(default=0)
    opening_cash_on = models.DateField(null=True, blank=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    updated_at = models.DateTimeField(default=timezone.now)

    def __str__(self) -> str:
        return "accounting setup"

    @classmethod
    def get(cls) -> AccountingSetup:
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def accounts(self) -> dict[str, str]:
        out = {}
        for line in (self.gl_accounts or DEFAULT_GL).splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip() and value.strip():
                out[key.strip()] = value.strip()[:80]
        defaults = {
            k.strip(): v.strip()
            for k, _, v in (line.partition("=") for line in DEFAULT_GL.splitlines())
        }
        return {**defaults, **ONLINE_GL, **out}


class DepositBatch(AppendOnlyModel):
    """Owner payments taken to the bank together. A mistaken batch is voided by
    a reversing batch (negative total, ``voids`` set), never edited."""

    deposited_on = models.DateField()
    bank_reference = models.CharField(max_length=40, blank=True)
    total_cents = models.BigIntegerField()
    voids = models.OneToOneField(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="voided_by"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-deposited_on", "-id"]

    def __str__(self) -> str:
        return f"deposit #{self.pk}"


class DepositItem(ScopedAppendOnlyModel):
    scope_lot_path = "entry__lot"

    batch = models.ForeignKey(DepositBatch, on_delete=models.PROTECT, related_name="items")
    entry = models.ForeignKey(LedgerEntry, on_delete=models.PROTECT, related_name="+")
    amount_cents = models.BigIntegerField()

    def __str__(self) -> str:
        return f"deposit item #{self.pk}"


class BankReconciliation(AppendOnlyModel):
    """A month's bank statement matched to the cash book. Kept as recorded;
    a redo is a new reconciliation for the same date."""

    statement_date = models.DateField()
    statement_balance_cents = models.BigIntegerField()
    book_balance_cents = models.BigIntegerField()
    outstanding_deposits_cents = models.BigIntegerField()
    outstanding_payments_cents = models.BigIntegerField()
    difference_cents = models.BigIntegerField()
    note = models.TextField(blank=True)
    reconciled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-statement_date", "-id"]

    def __str__(self) -> str:
        return f"reconciliation {self.statement_date}"


class ReconciledItem(AppendOnlyModel):
    """A deposit or vendor payment marked as cleared on a statement."""

    reconciliation = models.ForeignKey(
        BankReconciliation, on_delete=models.PROTECT, related_name="items"
    )
    kind = models.CharField(
        max_length=8,
        choices=[("deposit", "Deposit"), ("payment", "Payment"), ("returned", "Returned item")],
    )
    object_id = models.PositiveBigIntegerField()
    amount_cents = models.BigIntegerField()

    def __str__(self) -> str:
        return f"cleared {self.kind} #{self.object_id}"


class CourtesyReminder(ScopedAppendOnlyModel):
    """One courtesy email per lot per month, so a re-run never repeats it."""

    scope_lot_path = "lot"

    lot = models.ForeignKey("community.Lot", on_delete=models.PROTECT, related_name="+")
    period = models.CharField(max_length=7)  # YYYY-MM
    amount_cents = models.BigIntegerField()
    sent_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["lot", "period"], name="courtesy_once_per_month")
        ]

    def __str__(self) -> str:
        return f"courtesy reminder {self.period}"


class CollectionLetter(ScopedAppendOnlyModel):
    """A delinquency notice generated for a collection case (printed and
    mailed by the board; nothing is sent from the portal)."""

    scope_lot_path = "case__lot"

    case = models.ForeignKey(CollectionCase, on_delete=models.PROTECT, related_name="letters")
    kind = models.CharField(max_length=10, choices=LetterKind.choices)
    amount_cents = models.BigIntegerField()
    generated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    generated_on = models.DateField(default=timezone.localdate)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["created_at", "id"]
        constraints = [
            models.UniqueConstraint(fields=["case", "kind"], name="collection_letter_once_per_kind")
        ]

    def __str__(self) -> str:
        return f"{self.kind} letter"


# --- Open-item accounting (decision D32-payments) ----------------------------------------
# Every payment or credit is applied to specific charges, so each charge has
# its own open balance. Applications are append-only: undoing one is a new,
# negative application. ledger/allocation.py is the only writer.


class ChargeNumber(ScopedAppendOnlyModel):
    """The human number of a charge ("Invoice 2026-0142"): year and a
    sequence within the year, given once when the charge posts."""

    scope_lot_path = "entry__lot"

    entry = models.OneToOneField(
        LedgerEntry, on_delete=models.PROTECT, primary_key=True, related_name="charge_number"
    )
    year = models.PositiveSmallIntegerField()
    seq = models.PositiveIntegerField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=["year", "seq"], name="charge_number_unique")]

    def __str__(self) -> str:
        return self.label

    @property
    def label(self) -> str:
        return f"{self.year}-{self.seq:04d}"


class ApplicationKind(models.TextChoices):
    AUTO = "auto", "By the application rule"
    DIRECTED = "directed", "To the charges chosen"
    REVERSAL = "reversal", "Reversal matched to its entry"
    UNWIND = "unwind", "Undone by a reversal"
    MIGRATION = "migration", "Rebuilt from history"


class PaymentApplication(ScopedAppendOnlyModel):
    """``amount_cents`` of a payment or credit (``source``, a negative entry)
    applied to a charge (``charge``, a positive entry). Negative rows undo
    earlier ones."""

    scope_lot_path = "lot"

    lot = models.ForeignKey("community.Lot", on_delete=models.PROTECT, related_name="+")
    source = models.ForeignKey(
        LedgerEntry, on_delete=models.PROTECT, related_name="applications_out"
    )
    charge = models.ForeignKey(
        LedgerEntry, on_delete=models.PROTECT, related_name="applications_in"
    )
    amount_cents = models.BigIntegerField()
    kind = models.CharField(max_length=10, choices=ApplicationKind.choices)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["id"]
        indexes = [models.Index(fields=["lot", "id"])]
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(amount_cents=0), name="payment_application_nonzero"
            )
        ]

    def __str__(self) -> str:
        return f"application #{self.pk}"
