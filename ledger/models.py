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
from simple_history.models import HistoricalRecords

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


# --- Reserve plan (in-house reserve study) -------------------------------------------------


class ReserveCategory(models.TextChoices):
    ROADS = "roads", "Roads and paving"
    STORMWATER = "stormwater", "Stormwater and ponds"
    SIGNS = "signs", "Signs and entrance"
    LANDSCAPING = "landscaping", "Landscaping"
    FENCING = "fencing", "Fencing"
    LIGHTING = "lighting", "Lighting"
    OTHER = "other", "Other"


class ReserveComponent(models.Model):
    """Something the association will one day replace, at today's cost.
    Changing the plan is a board decision; every change is kept (history)."""

    name = models.CharField(max_length=120)
    category = models.CharField(
        max_length=12, choices=ReserveCategory.choices, default=ReserveCategory.OTHER
    )
    quantity = models.CharField(max_length=60, blank=True, help_text='e.g. "1,200 sq yd"')
    replacement_cost_cents = models.BigIntegerField()
    useful_life_years = models.PositiveSmallIntegerField()
    remaining_life_years = models.PositiveSmallIntegerField(null=True, blank=True)
    last_replaced_year = models.PositiveSmallIntegerField(null=True, blank=True)
    notes = models.TextField(blank=True, help_text="Board only")
    active = models.BooleanField(default=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    updated_at = models.DateTimeField(default=timezone.now)
    history = HistoricalRecords()

    class Meta:
        ordering = ["category", "name", "id"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(replacement_cost_cents__gte=0),
                name="reserve_component_cost_not_negative",
            ),
            models.CheckConstraint(
                condition=models.Q(useful_life_years__gte=1),
                name="reserve_component_life_at_least_one",
            ),
            models.CheckConstraint(
                condition=models.Q(remaining_life_years__isnull=False)
                | models.Q(last_replaced_year__isnull=False),
                name="reserve_component_remaining_or_last_replaced",
            ),
        ]

    def __str__(self) -> str:
        return self.name

    def delete(self, *args, **kwargs):
        raise PermissionError("Reserve components are switched off (not active), not deleted.")

    def remaining_life(self, start_year: int) -> int:
        """Years left at the start of ``start_year`` (0 = due that year)."""
        if self.remaining_life_years is not None:
            return self.remaining_life_years
        return max(0, self.last_replaced_year + self.useful_life_years - start_year)


class ReservePlanSettings(models.Model):
    """One row: the board's reserve plan inputs. Rates are basis points."""

    starting_balance_cents = models.BigIntegerField(null=True, blank=True)
    balance_as_of = models.DateField(null=True, blank=True)
    annual_contribution_cents = models.BigIntegerField(default=0)
    inflation_bp = models.PositiveIntegerField(default=300)
    interest_bp = models.PositiveIntegerField(default=100)
    horizon_years = models.PositiveSmallIntegerField(default=30)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    updated_at = models.DateTimeField(default=timezone.now)
    history = HistoricalRecords()

    def __str__(self) -> str:
        return "reserve plan settings"

    def delete(self, *args, **kwargs):
        raise PermissionError("The reserve plan settings are kept.")

    @classmethod
    def get(cls) -> ReservePlanSettings:
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class ReserveStudySnapshot(AppendOnlyModel):
    """A reserve plan as the board adopted it: the inputs and the figures it
    produced, kept as recorded. A later adoption is a new snapshot."""

    adopted_on = models.DateField(default=timezone.localdate)
    adopted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    start_year = models.PositiveSmallIntegerField()
    horizon_years = models.PositiveSmallIntegerField()
    starting_balance_cents = models.BigIntegerField()
    balance_as_of = models.DateField(null=True, blank=True)
    annual_contribution_cents = models.BigIntegerField()
    inflation_bp = models.PositiveIntegerField()
    interest_bp = models.PositiveIntegerField()
    fully_funded_cents = models.BigIntegerField()
    percent_funded = models.IntegerField(null=True, blank=True)
    needed_contribution_cents = models.BigIntegerField()
    total_expense_cents = models.BigIntegerField()
    lowest_end_cents = models.BigIntegerField()
    lowest_year = models.PositiveSmallIntegerField()
    first_negative_year = models.PositiveSmallIntegerField(null=True, blank=True)
    # Components as adopted (no board notes) and the next items due.
    components = models.JSONField(default=list)
    next_items = models.JSONField(default=list)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-adopted_on", "-id"]

    def __str__(self) -> str:
        return f"reserve plan adopted {self.adopted_on}"


# --- Bank feed (optional feature "bank_feeds", D32-bankfeeds) ---------------------------
# Lines mirrored from the HOA's bank (Teller, or a statement file). Never full
# account or routing numbers: the last four digits only.


class BankProvider(models.TextChoices):
    FILE = "file", "Statement files"
    TELLER = "teller", "Teller"


class BankConnectionStatus(models.TextChoices):
    ACTIVE = "active", "Connected"
    NEEDS_REAUTH = "needs_reauth", "Needs reconnecting"
    DISCONNECTED = "disconnected", "Disconnected"


class BankConnection(models.Model):
    """One bank account the portal reads. Disconnecting deletes the access
    token and keeps the account's imported lines."""

    provider = models.CharField(max_length=10, choices=BankProvider.choices)
    display_name = models.CharField(max_length=80)
    institution_name = models.CharField(max_length=80, blank=True)
    account_last_four = models.CharField(max_length=4, blank=True)
    # Teller's own opaque ids ("acc_...", "enr_..."), not bank numbers.
    external_account_id = models.CharField(max_length=64, blank=True)
    enrollment_id = models.CharField(max_length=64, blank=True)
    # Fernet token from BANK_FEED_TOKEN_KEY; empty once disconnected.
    token_encrypted = models.TextField(blank=True)
    status = models.CharField(
        max_length=14, choices=BankConnectionStatus.choices, default=BankConnectionStatus.ACTIVE
    )
    last_synced_at = models.DateTimeField(null=True, blank=True)
    last_error_status = models.PositiveSmallIntegerField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(default=timezone.now)
    disconnected_at = models.DateTimeField(null=True, blank=True)
    disconnected_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        ordering = ["display_name", "id"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(account_last_four__regex=r"^[0-9]{0,4}$"),
                name="bank_connection_last_four_only",
            )
        ]

    def __str__(self) -> str:
        return f"bank connection #{self.pk}"

    @property
    def label(self) -> str:
        tail = f" ··{self.account_last_four}" if self.account_last_four else ""
        return f"{self.display_name}{tail}"


class BankTransactionSource(models.TextChoices):
    TELLER = "teller", "Teller"
    OFX = "ofx", "OFX/QFX file"
    CSV = "csv", "CSV file"


class BankTransaction(AppendOnlyModel):
    """A line from the bank: positive amounts are money in. Mirrors the bank,
    so never corrected here; re-imports are skipped by the bank's own id."""

    connection = models.ForeignKey(
        BankConnection, on_delete=models.PROTECT, related_name="transactions"
    )
    bank_id = models.CharField(max_length=255)
    posted_on = models.DateField()
    amount_cents = models.BigIntegerField()
    description = models.CharField(max_length=200, blank=True)
    kind = models.CharField(max_length=40, blank=True)
    source = models.CharField(max_length=8, choices=BankTransactionSource.choices)
    imported_at = models.DateTimeField(default=timezone.now)
    imported_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        ordering = ["-posted_on", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["connection", "bank_id"], name="bank_transaction_once_per_connection"
            )
        ]
        indexes = [models.Index(fields=["posted_on", "amount_cents"])]

    def __str__(self) -> str:
        return f"bank line #{self.pk}"


class BankBalance(AppendOnlyModel):
    """The bank's ledger balance on a date, as reported by the feed or file."""

    connection = models.ForeignKey(BankConnection, on_delete=models.PROTECT, related_name="+")
    as_of = models.DateField()
    balance_cents = models.BigIntegerField()
    source = models.CharField(max_length=8, choices=BankTransactionSource.choices)
    recorded_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-as_of", "-id"]

    def __str__(self) -> str:
        return f"bank balance {self.as_of}"


class BankMatch(AppendOnlyModel):
    """A bank line confirmed as one cash-book item (deposit, vendor payment or
    returned payment). Undoing is a new row pointing at the match it undoes."""

    transaction = models.ForeignKey(BankTransaction, on_delete=models.PROTECT, related_name="+")
    kind = models.CharField(
        max_length=8,
        choices=[("deposit", "Deposit"), ("payment", "Payment"), ("returned", "Returned item")],
    )
    object_id = models.PositiveBigIntegerField()
    undoes = models.OneToOneField(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="undone_by"
    )
    confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    confirmed_at = models.DateTimeField(default=timezone.now)

    def __str__(self) -> str:
        return f"bank match #{self.pk}"
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
