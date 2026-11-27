"""Books and close (D69, D70, D71): the association's own books.

Imported by ledger/models.py so Django registers these models in the ledger
app. Configuration (bank accounts' funds, categories, coding rules) is
ordinary rows with history; everything that is money, a decision or a
record of the close is append-only (model, trigger and runtime role, rule 5).
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils import timezone
from simple_history.models import HistoricalRecords

from core.append_only import AppendOnlyModel


class Fund(models.TextChoices):
    OPERATING = "operating", "Operating fund"
    RESERVE = "reserve", "Reserve fund"


class BankAccountSetup(models.Model):
    """One association bank account in the books (D69): its fund, the account
    it is in the chart, and its balance on the books' start date. The bank
    lines themselves are the bank feed's (BankConnection, BankTransaction)."""

    connection = models.OneToOneField(
        "ledger.BankConnection", on_delete=models.PROTECT, related_name="books"
    )
    fund = models.CharField(max_length=10, choices=Fund.choices, default=Fund.OPERATING)
    gl_account = models.CharField(max_length=80)
    # Deposits, Stripe payouts, vendor payments and owner checks are in this
    # account unless matched to a line of another account.
    is_primary = models.BooleanField(default=False)
    opening_balance_cents = models.BigIntegerField(null=True, blank=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    updated_at = models.DateTimeField(default=timezone.now)
    history = HistoricalRecords()

    class Meta:
        ordering = ["-is_primary", "fund", "id"]

    def __str__(self) -> str:
        return f"books account #{self.pk}"

    def delete(self, *args, **kwargs):
        raise PermissionError("Bank accounts in the books are kept.")


class CategoryKind(models.TextChoices):
    INCOME = "income", "Income"
    EXPENSE = "expense", "Expense"
    TRANSFER = "transfer", "Transfer between the association's accounts"
    ASSET = "asset", "Asset"
    LIABILITY = "liability", "Liability"
    EQUITY = "equity", "Fund balance"


class GLCategory(models.Model):
    """A category bank lines are coded to (D69): interest income, bank fees,
    insurance paid by bank debit, a transfer to the reserve account... Never
    deleted; a category no longer used is switched off."""

    key = models.SlugField(max_length=40, unique=True)
    name = models.CharField(max_length=80)
    account = models.CharField(max_length=80)
    kind = models.CharField(max_length=10, choices=CategoryKind.choices)
    # Blank: the fund of the bank account the line is on.
    fund = models.CharField(max_length=10, choices=Fund.choices, blank=True)
    # Form 1120-H: income from members in their capacity as members.
    exempt_function = models.BooleanField(default=False)
    active = models.BooleanField(default=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    updated_at = models.DateTimeField(default=timezone.now)
    history = HistoricalRecords()

    class Meta:
        ordering = ["kind", "account", "id"]

    def __str__(self) -> str:
        return self.name

    def delete(self, *args, **kwargs):
        raise PermissionError("Categories are kept; switch one off instead.")


class LineSign(models.TextChoices):
    ANY = "any", "Money in or out"
    IN = "in", "Money in"
    OUT = "out", "Money out"


class CodingRule(models.Model):
    """A board rule that codes new bank lines by itself (D69): the words the
    bank's description contains, money in or out, optionally one account and
    an amount range. The first matching active rule (lowest priority number)
    wins. Ending a rule keeps it for the record."""

    name = models.CharField(max_length=80)
    contains = models.CharField(max_length=80)
    sign = models.CharField(max_length=4, choices=LineSign.choices, default=LineSign.ANY)
    connection = models.ForeignKey(
        "ledger.BankConnection", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    min_cents = models.BigIntegerField(null=True, blank=True)
    max_cents = models.BigIntegerField(null=True, blank=True)
    category = models.ForeignKey(GLCategory, on_delete=models.PROTECT, related_name="rules")
    priority = models.PositiveSmallIntegerField(default=100)
    active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(default=timezone.now)
    ended_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    ended_at = models.DateTimeField(null=True, blank=True)
    history = HistoricalRecords()

    class Meta:
        ordering = ["priority", "id"]

    def __str__(self) -> str:
        return f"coding rule #{self.pk}"

    def delete(self, *args, **kwargs):
        raise PermissionError("Coding rules are kept; end one instead.")


class GLEntry(AppendOnlyModel):
    """A bank line coded to a category: the journal entry for money that does
    not pass through an owner account or an invoice (D69). Positive amounts
    are money into the bank account. Undoing is a reversing entry
    (``reverses``), dated in an open month."""

    transaction = models.ForeignKey(
        "ledger.BankTransaction", on_delete=models.PROTECT, related_name="gl_entries"
    )
    posted_on = models.DateField()
    category = models.ForeignKey(GLCategory, on_delete=models.PROTECT, related_name="+")
    fund = models.CharField(max_length=10, choices=Fund.choices)
    cash_fund = models.CharField(max_length=10, choices=Fund.choices)
    cash_account = models.CharField(max_length=80)
    category_account = models.CharField(max_length=80)
    amount_cents = models.BigIntegerField()
    memo = models.CharField(max_length=200, blank=True)
    reference = models.CharField(max_length=60, blank=True)
    rule = models.ForeignKey(
        CodingRule, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    coded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    reverses = models.OneToOneField(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="reversed_by"
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["posted_on", "id"]
        constraints = [
            models.CheckConstraint(condition=~models.Q(amount_cents=0), name="gl_entry_nonzero"),
        ]

    def __str__(self) -> str:
        return f"GL entry #{self.pk}"


class PeriodKind(models.TextChoices):
    MONTH = "month", "Month"
    YEAR = "year", "Fiscal year"


class CloseHow(models.TextChoices):
    AUTO = "auto", "Closed automatically"
    MANUAL = "manual", "Closed by a board member"


class PeriodClose(AppendOnlyModel):
    """A month or fiscal year closed (D70). Nothing may be posted, backdated
    or moved into a closed month (services and a database trigger). A reopen
    needs a request and a different board member's approval."""

    kind = models.CharField(max_length=5, choices=PeriodKind.choices)
    period = models.CharField(max_length=8)  # "2026-10" or "FY2026"
    starts_on = models.DateField()
    ends_on = models.DateField()
    how = models.CharField(max_length=6, choices=CloseHow.choices)
    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    closed_at = models.DateTimeField(default=timezone.now)
    # The checks and per-account reconciliation figures as they stood (ids,
    # counts and amounts only).
    summary = models.JSONField(default=dict)

    class Meta:
        ordering = ["-starts_on", "-id"]
        indexes = [models.Index(fields=["kind", "starts_on", "ends_on"])]

    def __str__(self) -> str:
        return f"close {self.period}"


class PeriodReopenRequest(AppendOnlyModel):
    close = models.ForeignKey(PeriodClose, on_delete=models.PROTECT, related_name="reopen_requests")
    reason = models.TextField()
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    requested_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-requested_at", "-id"]

    def __str__(self) -> str:
        return f"reopen request #{self.pk}"


class PeriodReopenDecision(AppendOnlyModel):
    request = models.OneToOneField(
        PeriodReopenRequest, on_delete=models.PROTECT, related_name="decision"
    )
    approved = models.BooleanField()
    note = models.TextField(blank=True)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    decided_at = models.DateTimeField(default=timezone.now)

    def __str__(self) -> str:
        return f"reopen decision #{self.pk}"


class ExceptionNote(AppendOnlyModel):
    """A close exception explained (D70): a known difference on an account, a
    late fee deliberately waived, a warning acknowledged. A money difference
    (``amount_cents`` not zero), or a note the manager recorded, counts only
    once a different board member agrees."""

    period = models.CharField(max_length=8)
    check_key = models.CharField(max_length=30)
    item = models.CharField(max_length=40, blank=True)
    amount_cents = models.BigIntegerField(default=0)
    reason = models.TextField()
    # False when a board member noted something that moves no money (a
    # waived late fee, an acknowledged warning): it counts at once.
    needs_agreement = models.BooleanField(default=True)
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    recorded_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-recorded_at", "-id"]
        indexes = [models.Index(fields=["period", "check_key"])]

    def __str__(self) -> str:
        return f"exception note #{self.pk}"


class ExceptionAgreement(AppendOnlyModel):
    note = models.OneToOneField(ExceptionNote, on_delete=models.PROTECT, related_name="agreement")
    agreed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    agreed_at = models.DateTimeField(default=timezone.now)

    def __str__(self) -> str:
        return f"exception agreement #{self.pk}"


class CloseRun(AppendOnlyModel):
    """One evaluation of a period's checks by the automatic close (D70), kept
    so My items and the package show what was found and when."""

    kind = models.CharField(max_length=5, choices=PeriodKind.choices)
    period = models.CharField(max_length=8)
    ran_at = models.DateTimeField(default=timezone.now)
    passed = models.BooleanField()
    closed = models.BooleanField(default=False)
    blocking = models.PositiveIntegerField(default=0)
    warnings = models.PositiveIntegerField(default=0)
    results = models.JSONField(default=list)

    class Meta:
        ordering = ["-ran_at", "-id"]

    def __str__(self) -> str:
        return f"close run {self.period}"


def _package_key(instance, filename):
    import uuid

    return f"books/packages/{uuid.uuid4().hex}"


class YearEndPackage(AppendOnlyModel):
    """A year-end package (D71): the .zip as built, stored privately under a
    random key, with its manifest and the keyed signature."""

    fiscal_year = models.PositiveSmallIntegerField()
    year_closed = models.BooleanField()
    file = models.FileField(upload_to=_package_key, max_length=200)
    size = models.PositiveBigIntegerField()
    sha256 = models.CharField(max_length=64)
    manifest = models.JSONField(default=dict)
    signature = models.CharField(max_length=64)
    file_count = models.PositiveIntegerField()
    skipped_count = models.PositiveIntegerField(default=0)
    automatic = models.BooleanField(default=False)
    built_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    built_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-built_at", "-id"]

    def __str__(self) -> str:
        return f"year-end package FY{self.fiscal_year}"
