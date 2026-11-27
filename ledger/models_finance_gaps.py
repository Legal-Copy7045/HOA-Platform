"""Special assessments (D75) and payment plans (D76).

Imported at the bottom of ``ledger/models.py`` so Django registers these
models in the ``ledger`` app; kept in their own module so they merge cleanly
with other work on the ledger. Every record here is append-only (CLAUDE.md
rule 5): a state is derived from the rows, never stored and edited.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils import timezone

from core.append_only import AppendOnlyModel, ScopedAppendOnlyModel

# --- Special assessments (D75) ----------------------------------------------------------


class SpecialAssessmentFund(models.TextChoices):
    # Which fund the money is designated to. The general ledger (books
    # branch) maps this to its fund accounts; nothing here posts to a fund.
    OPERATING = "operating", "Operating fund"
    RESERVE = "reserve", "Reserve fund"


class SpecialAllocation(models.TextChoices):
    EQUAL = "equal", "Equal share per home"
    CUSTOM = "custom", "Custom amount per home"


class SpecialAssessment(AppendOnlyModel):
    """One levy: what it is for, the total, how it is split and when it is
    due. Fixed when levied; future installments are cancelled with a
    ``SpecialAssessmentCancel`` row, never by editing."""

    title = models.CharField(max_length=120)
    purpose = models.TextField()
    total_cents = models.BigIntegerField()
    allocation = models.CharField(max_length=8, choices=SpecialAllocation.choices)
    installments = models.PositiveSmallIntegerField()
    fund = models.CharField(max_length=10, choices=SpecialAssessmentFund.choices)
    # The board resolution levying it (required, like D52): recorded in the
    # portal, or the reference of one adopted elsewhere.
    resolution = models.ForeignKey(
        "governance.Resolution", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    resolution_ref = models.CharField(max_length=120, blank=True)
    # The owners' vote, when the Declaration requires one above an amount
    # (rule special_assessment.vote_required_cents): a closed formal vote in
    # the portal, or the reference of one held elsewhere.
    vote_required = models.BooleanField(default=False)
    vote = models.ForeignKey(
        "governance.Vote", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    vote_ref = models.CharField(max_length=120, blank=True)
    vote_threshold_percent = models.PositiveSmallIntegerField(null=True, blank=True)
    late_fees = models.BooleanField(default=False)
    rule_citation = models.CharField(max_length=400, blank=True)
    # None only for demo data levied by the system.
    levied_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(total_cents__gt=0), name="special_assessment_total_positive"
            ),
            models.CheckConstraint(
                condition=models.Q(installments__gte=1, installments__lte=60),
                name="special_assessment_installments_range",
            ),
        ]

    def __str__(self) -> str:
        return f"special assessment #{self.pk}"


class SpecialAssessmentShare(ScopedAppendOnlyModel):
    """One home's share of the total (all installments together)."""

    scope_lot_path = "lot"

    assessment = models.ForeignKey(
        SpecialAssessment, on_delete=models.PROTECT, related_name="shares"
    )
    lot = models.ForeignKey("community.Lot", on_delete=models.PROTECT, related_name="+")
    amount_cents = models.BigIntegerField()

    class Meta:
        ordering = ["assessment", "id"]
        constraints = [
            models.UniqueConstraint(fields=["assessment", "lot"], name="special_share_once"),
            models.CheckConstraint(
                condition=models.Q(amount_cents__gte=0), name="special_share_not_negative"
            ),
        ]

    def __str__(self) -> str:
        return f"special assessment share #{self.pk}"


class SpecialAssessmentInstallment(AppendOnlyModel):
    assessment = models.ForeignKey(
        SpecialAssessment, on_delete=models.PROTECT, related_name="installment_rows"
    )
    number = models.PositiveSmallIntegerField()
    due_on = models.DateField()

    class Meta:
        ordering = ["assessment", "number"]
        constraints = [
            models.UniqueConstraint(
                fields=["assessment", "number"], name="special_installment_once"
            )
        ]

    def __str__(self) -> str:
        return f"installment {self.number}"


class SpecialAssessmentCharge(ScopedAppendOnlyModel):
    """The ledger charge one installment posted on one home: at most one
    (the unique constraint makes the scheduled posting idempotent)."""

    scope_lot_path = "lot"

    installment = models.ForeignKey(
        SpecialAssessmentInstallment, on_delete=models.PROTECT, related_name="charges"
    )
    lot = models.ForeignKey("community.Lot", on_delete=models.PROTECT, related_name="+")
    entry = models.OneToOneField("ledger.LedgerEntry", on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["installment", "lot"], name="special_charge_once")
        ]

    def __str__(self) -> str:
        return f"special assessment charge #{self.pk}"


class SpecialAssessmentLateFee(ScopedAppendOnlyModel):
    """A late fee posted for one home's unpaid installment (once)."""

    scope_lot_path = "lot"

    installment = models.ForeignKey(
        SpecialAssessmentInstallment, on_delete=models.PROTECT, related_name="+"
    )
    lot = models.ForeignKey("community.Lot", on_delete=models.PROTECT, related_name="+")
    entry = models.OneToOneField("ledger.LedgerEntry", on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["installment", "lot"], name="special_late_fee_once")
        ]

    def __str__(self) -> str:
        return f"special assessment late fee #{self.pk}"


class SpecialAssessmentCancel(AppendOnlyModel):
    """The board cancelled installments ``from_number`` onwards (not yet
    posted ones; posted charges are reversed only by a normal reversal)."""

    assessment = models.ForeignKey(
        SpecialAssessment, on_delete=models.PROTECT, related_name="cancels"
    )
    from_number = models.PositiveSmallIntegerField()
    reason = models.CharField(max_length=300)
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self) -> str:
        return f"special assessment cancellation #{self.pk}"


class SpecialAssessmentNotice(AppendOnlyModel):
    """The owner notice of the levy was sent (counts only)."""

    assessment = models.ForeignKey(
        SpecialAssessment, on_delete=models.PROTECT, related_name="notices"
    )
    emailed = models.PositiveIntegerField(default=0)
    paper = models.PositiveIntegerField(default=0)
    sent_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(default=timezone.now)

    def __str__(self) -> str:
        return f"special assessment notice #{self.pk}"


# --- Payment plans (D76) -----------------------------------------------------------------


class PaymentPlan(ScopedAppendOnlyModel):
    """An agreed schedule for paying off a collection case's arrears.

    ``charge_ids`` are the open charges the plan covers (fixed when it is
    prepared); progress is how much of them the open-item allocation has
    paid since. Status comes from ``PaymentPlanEvent`` rows."""

    scope_lot_path = "lot"
    conflict_noun = "payment plan"  # own matters (D65)

    case = models.ForeignKey(
        "ledger.CollectionCase", on_delete=models.PROTECT, related_name="plans"
    )
    lot = models.ForeignKey("community.Lot", on_delete=models.PROTECT, related_name="+")
    account = models.ForeignKey("community.Account", on_delete=models.PROTECT, related_name="+")
    total_cents = models.BigIntegerField()
    charge_ids = models.JSONField(default=list)
    start_on = models.DateField()
    # The case's state before the plan: where it returns if the plan ends
    # without the arrears being paid.
    prior_state = models.CharField(max_length=24)
    suspend_late_fees = models.BooleanField(default=False)
    grace_days = models.PositiveSmallIntegerField(default=0)
    default_days = models.PositiveSmallIntegerField(default=30)
    rule_citation = models.CharField(max_length=400, blank=True)
    note = models.TextField(blank=True)
    prepared_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(total_cents__gt=0), name="payment_plan_total_positive"
            )
        ]

    def __str__(self) -> str:
        return f"payment plan #{self.pk}"


class PaymentPlanInstallment(ScopedAppendOnlyModel):
    scope_lot_path = "plan__lot"

    plan = models.ForeignKey(PaymentPlan, on_delete=models.PROTECT, related_name="installments")
    number = models.PositiveSmallIntegerField()
    due_on = models.DateField()
    amount_cents = models.BigIntegerField()

    class Meta:
        ordering = ["plan", "number"]
        constraints = [
            models.UniqueConstraint(fields=["plan", "number"], name="plan_installment_once"),
            models.CheckConstraint(
                condition=models.Q(amount_cents__gt=0), name="plan_installment_positive"
            ),
        ]

    def __str__(self) -> str:
        return f"plan installment {self.number}"


class PlanEventKind(models.TextChoices):
    APPROVED = "approved", "Approved"
    DECLINED = "declined", "Declined"
    ACKNOWLEDGED = "acknowledged", "Acknowledged by the owner"
    COMPLETED = "completed", "Completed"
    DEFAULTED = "defaulted", "Defaulted"
    CANCELLED = "cancelled", "Cancelled"


# Events after which the plan is over.
PLAN_ENDINGS = (
    PlanEventKind.DECLINED,
    PlanEventKind.COMPLETED,
    PlanEventKind.DEFAULTED,
    PlanEventKind.CANCELLED,
)


class PaymentPlanEvent(ScopedAppendOnlyModel):
    scope_lot_path = "plan__lot"

    plan = models.ForeignKey(PaymentPlan, on_delete=models.PROTECT, related_name="events")
    kind = models.CharField(max_length=12, choices=PlanEventKind.choices)
    by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    note = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["created_at", "id"]
        constraints = [
            models.UniqueConstraint(fields=["plan", "kind"], name="plan_event_once"),
        ]

    def __str__(self) -> str:
        return f"plan event #{self.pk}"
