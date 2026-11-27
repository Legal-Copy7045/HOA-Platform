"""Renewal tracking and records retention (D72). Imported by documents.models.

Renewals: the association's own insurance policies (``InsurancePolicy``),
vendors' insurance certificates (``VendorCertificate``, append-only: a new
certificate is a new row) and the renewal terms of a contract
(``ContractTerms``: renews by itself or not, the notice needed to cancel).
The board's decision on a contract term is an append-only
``RenewalDecision``; each reminder email sent is an append-only
``RenewalReminder`` (one per item, deadline and threshold).

Retention: ``LegalHold`` / ``LegalHoldRelease`` (append-only), a two-person
disposal (``DisposalRequest`` with its items, then a ``DisposalDecision`` by
a different board member) and the permanent ``DocumentDisposal`` record of
each file removed. The ``DocumentVersion`` row itself is never touched: its
metadata (name, size, checksum, who uploaded it) stays; only the stored file
goes.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils import timezone
from simple_history.models import HistoricalRecords

from core.append_only import AppendOnlyModel


class PolicyKind(models.TextChoices):
    GENERAL_LIABILITY = "general_liability", "General liability"
    DIRECTORS_OFFICERS = "d_and_o", "Directors and officers (D&O)"
    FIDELITY = "fidelity", "Fidelity bond / crime"
    PROPERTY = "property", "Property (common areas)"
    UMBRELLA = "umbrella", "Umbrella"
    OTHER = "other", "Other"


class InsurancePolicy(models.Model):
    """One term of one of the association's insurance policies. A renewal is
    a new row (its document becomes a new version of the same library
    document); corrections to a row are kept in its change history."""

    kind = models.CharField(max_length=20, choices=PolicyKind.choices)
    carrier = models.CharField(max_length=200)
    policy_number = models.CharField(max_length=60, blank=True)
    # Per-occurrence limit (or the bond amount), in cents.
    coverage_cents = models.PositiveBigIntegerField()
    aggregate_cents = models.PositiveBigIntegerField(null=True, blank=True)
    deductible_cents = models.PositiveBigIntegerField(null=True, blank=True)
    premium_cents = models.PositiveBigIntegerField(null=True, blank=True)
    starts_on = models.DateField()
    ends_on = models.DateField()
    document = models.ForeignKey(
        "documents.Document", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    notes = models.TextField(blank=True)
    # The term this one renews (recorded from that policy's page): the older
    # term is then no longer tracked, and its document gets this term's file
    # as a new version.
    renews = models.OneToOneField(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="renewed_by"
    )
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    recorded_at = models.DateTimeField(default=timezone.now)

    history = HistoricalRecords()

    class Meta:
        ordering = ["kind", "-ends_on", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(ends_on__gt=models.F("starts_on")),
                name="insurance_policy_ends_after_start",
            ),
            models.CheckConstraint(
                condition=models.Q(coverage_cents__gt=0), name="insurance_policy_coverage_positive"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.get_kind_display()} ({self.carrier})"

    def delete(self, *args, **kwargs):
        raise PermissionError("Insurance policies are kept; record the next term instead.")


class VendorCertificate(AppendOnlyModel):
    """A vendor's certificate of insurance. Never changed: a new certificate
    is a new row; the newest expiry is the vendor's."""

    vendor = models.ForeignKey(
        "operations.Vendor", on_delete=models.PROTECT, related_name="insurance_certificates"
    )
    insurer = models.CharField(max_length=200, blank=True)
    coverage_cents = models.PositiveBigIntegerField(null=True, blank=True)
    expires_on = models.DateField()
    document = models.ForeignKey(
        "documents.Document", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    recorded_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-expires_on", "-id"]

    def __str__(self) -> str:
        return f"certificate #{self.pk}"


class RenewalType(models.TextChoices):
    AUTO = "auto", "Renews by itself unless cancelled"
    MANUAL = "manual", "Ends unless the board renews it"


class ContractTerms(models.Model):
    """How a contract renews. Kept beside the contract (whose approved terms
    are fixed by a trigger) so the board can record them at any time."""

    contract = models.OneToOneField(
        "operations.Contract", on_delete=models.PROTECT, related_name="renewal_terms"
    )
    renewal = models.CharField(max_length=8, choices=RenewalType.choices)
    # Written notice the contract needs before its end to cancel (0: none).
    notice_days = models.PositiveSmallIntegerField(default=0)
    # For a contract that renews by itself: the length of each new term.
    renews_for_months = models.PositiveSmallIntegerField(null=True, blank=True)
    document = models.ForeignKey(
        "documents.Document", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    updated_at = models.DateTimeField(default=timezone.now)

    history = HistoricalRecords()

    def __str__(self) -> str:
        return f"renewal terms of contract {self.contract_id}"

    def delete(self, *args, **kwargs):
        raise PermissionError("Contract renewal terms are changed, not deleted.")


class RenewalChoice(models.TextChoices):
    RENEW = "renew", "Renew"
    CANCEL = "cancel", "Cancel (notice sent)"
    LET_END = "let_end", "Let it end"


class RenewalDecision(AppendOnlyModel):
    """The board's decision on one term of a contract: renew, cancel or let
    it end. A board decision (never the manager); append-only."""

    contract = models.ForeignKey(
        "operations.Contract", on_delete=models.PROTECT, related_name="renewal_decisions"
    )
    term_ends_on = models.DateField()
    decision = models.CharField(max_length=8, choices=RenewalChoice.choices)
    note = models.CharField(max_length=300)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    decided_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-decided_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["contract", "term_ends_on"], name="renewal_decision_once_per_term"
            )
        ]

    def __str__(self) -> str:
        return f"renewal decision #{self.pk}"


class RenewalReminder(AppendOnlyModel):
    """One reminder email sent to the board (ids and dates only)."""

    kind = models.CharField(max_length=10)  # policy, vendor, contract
    object_id = models.PositiveBigIntegerField()
    deadline = models.DateField()
    threshold = models.SmallIntegerField()  # days before the deadline
    sent_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-sent_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["kind", "object_id", "deadline", "threshold"],
                name="renewal_reminder_once",
            )
        ]

    def __str__(self) -> str:
        return f"reminder #{self.pk}"


# --- Records retention --------------------------------------------------------------------


class LegalHold(AppendOnlyModel):
    """Stops disposal of one document, or of every document in a category,
    until released (litigation, an insurance claim, an audit)."""

    document = models.ForeignKey(
        "documents.Document", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    category = models.CharField(max_length=16, blank=True)
    reason = models.CharField(max_length=300)
    placed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    placed_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-placed_at", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=(models.Q(document__isnull=False) & models.Q(category=""))
                | (models.Q(document__isnull=True) & ~models.Q(category="")),
                name="legal_hold_document_or_category",
            )
        ]

    def __str__(self) -> str:
        return f"legal hold #{self.pk}"

    @property
    def category_label(self) -> str:
        from documents.models import Category

        return Category(self.category).label if self.category in Category.values else ""


class LegalHoldRelease(AppendOnlyModel):
    hold = models.OneToOneField(LegalHold, on_delete=models.PROTECT, related_name="release")
    reason = models.CharField(max_length=300)
    released_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    released_at = models.DateTimeField(default=timezone.now)

    def __str__(self) -> str:
        return f"hold release #{self.pk}"


class DisposalRequest(AppendOnlyModel):
    """A board member's proposal to dispose of files past their retention
    period. Nothing is removed until a different board member confirms."""

    reason = models.CharField(max_length=300)
    proposed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    proposed_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-proposed_at", "-id"]

    def __str__(self) -> str:
        return f"disposal request #{self.pk}"


class DisposalRequestItem(AppendOnlyModel):
    request = models.ForeignKey(DisposalRequest, on_delete=models.PROTECT, related_name="items")
    version = models.ForeignKey(
        "documents.DocumentVersion", on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(fields=["request", "version"], name="disposal_item_once")
        ]

    def __str__(self) -> str:
        return f"disposal item #{self.pk}"


class DisposalDecision(AppendOnlyModel):
    """Confirmed (by a board member other than the proposer) or declined."""

    request = models.OneToOneField(
        DisposalRequest, on_delete=models.PROTECT, related_name="decision"
    )
    approved = models.BooleanField()
    note = models.CharField(max_length=300, blank=True)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    decided_at = models.DateTimeField(default=timezone.now)

    def __str__(self) -> str:
        return f"disposal decision #{self.pk}"


class DocumentDisposal(AppendOnlyModel):
    """The permanent record that one version's stored file was disposed of:
    what, under which rule, when, who proposed and who confirmed, and why.
    The version row (name, size, checksum) stays as it was."""

    version = models.OneToOneField(
        "documents.DocumentVersion", on_delete=models.PROTECT, related_name="disposal"
    )
    document = models.ForeignKey(
        "documents.Document", on_delete=models.PROTECT, related_name="disposals"
    )
    request = models.ForeignKey(DisposalRequest, on_delete=models.PROTECT, related_name="+")
    category = models.CharField(max_length=16)
    basis = models.CharField(max_length=16)
    years = models.PositiveSmallIntegerField()
    kept_until = models.DateField()
    reason = models.CharField(max_length=300)
    proposed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    disposed_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-disposed_at", "-id"]

    def __str__(self) -> str:
        return f"disposal #{self.pk}"


class RetentionScan(AppendOnlyModel):
    """The nightly look for files past their retention period (counts only)."""

    ran_at = models.DateTimeField(default=timezone.now)
    due = models.PositiveIntegerField(default=0)
    held = models.PositiveIntegerField(default=0)
    files_removed = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["-ran_at", "-id"]

    def __str__(self) -> str:
        return f"retention scan #{self.pk}"
