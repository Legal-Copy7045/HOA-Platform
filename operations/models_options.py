"""Records of the finance options that belong to the operations app.

Capital projects (D83) and insurance claims (D85). A project or claim is a
working record whose state only its service changes; everything that
decides or moves money is append-only beside it: approvals, change orders
and their approvals, links to contracts/vendors/invoices/work orders (an
unlink is a new row), settlement approvals, insurance money expected and
received, and every state change (an event row).
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils import timezone

from core.append_only import AppendOnlyModel

# --- Capital projects (D83) ---------------------------------------------------------------


class Funding(models.TextChoices):
    RESERVE = "reserve", "Reserve fund"
    OPERATING = "operating", "Operating fund"
    SPECIAL = "special", "Special assessment"
    MIXED = "mixed", "More than one fund"


class ProjectState(models.TextChoices):
    PLANNED = "planned", "Planned"
    APPROVED = "approved", "Approved"
    IN_PROGRESS = "in_progress", "In progress"
    COMPLETED = "completed", "Completed"
    CANCELLED = "cancelled", "Cancelled"


class CapitalProject(models.Model):
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    reserve_component = models.ForeignKey(
        "ledger.ReserveComponent", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    budget_cents = models.PositiveBigIntegerField()
    funding = models.CharField(max_length=10, choices=Funding.choices)
    # Special assessment funding: a reference until the special assessments
    # of claude/finance-gaps exist (operations.capital_projects adapter).
    special_assessment_ref = models.CharField(max_length=200, blank=True)
    resolution_ref = models.CharField(max_length=200, blank=True)
    state = models.CharField(
        max_length=12, choices=ProjectState.choices, default=ProjectState.PLANNED
    )
    state_changed_at = models.DateTimeField(default=timezone.now)
    percent_complete = models.PositiveSmallIntegerField(default=0)
    owner_visible = models.BooleanField(default=False)
    # Board approvals it needs, fixed when approval is first asked (rule
    # capital.second_approval_cents), so a later rule change cannot lower it.
    required_approvals = models.PositiveSmallIntegerField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(percent_complete__lte=100), name="capital_percent_max"
            ),
        ]

    def __str__(self) -> str:
        return self.title

    def delete(self, *args, **kwargs):
        raise PermissionError("Capital projects are cancelled, not deleted.")


class CapitalProjectEvent(AppendOnlyModel):
    project = models.ForeignKey(CapitalProject, on_delete=models.PROTECT, related_name="events")
    action = models.CharField(max_length=24)
    from_state = models.CharField(max_length=12, blank=True)
    to_state = models.CharField(max_length=12, blank=True)
    note = models.TextField(blank=True)
    by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["at", "id"]


class CapitalProjectApproval(AppendOnlyModel):
    project = models.ForeignKey(CapitalProject, on_delete=models.PROTECT, related_name="approvals")
    approver = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    budget_cents = models.PositiveBigIntegerField()
    resolution_ref = models.CharField(max_length=200)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["created_at", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["project", "approver"], name="capital_one_approval_per_member"
            )
        ]


class ChangeOrder(AppendOnlyModel):
    project = models.ForeignKey(
        CapitalProject, on_delete=models.PROTECT, related_name="change_orders"
    )
    amount_cents = models.BigIntegerField(help_text="Positive adds to the budget, negative cuts it")
    reason = models.TextField()
    required_approvals = models.PositiveSmallIntegerField()
    proposed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["created_at", "id"]
        constraints = [
            models.CheckConstraint(condition=~models.Q(amount_cents=0), name="change_order_nonzero")
        ]


class ChangeOrderDecision(AppendOnlyModel):
    APPROVE, DECLINE = "approve", "decline"
    change_order = models.ForeignKey(
        ChangeOrder, on_delete=models.PROTECT, related_name="decisions"
    )
    decision = models.CharField(
        max_length=8, choices=[(APPROVE, "Approved"), (DECLINE, "Declined")]
    )
    by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    note = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["created_at", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["change_order", "by"], name="change_order_one_decision_per_member"
            )
        ]


class LinkKind(models.TextChoices):
    CONTRACT = "contract", "Contract"
    VENDOR = "vendor", "Vendor"
    INVOICE = "invoice", "Invoice"
    WORK_ORDER = "work_order", "Work order"


class CapitalProjectLink(AppendOnlyModel):
    """A contract, vendor, invoice or work order linked to a project. An
    unlink is a new row pointing at the link it removes (``removes``)."""

    project = models.ForeignKey(CapitalProject, on_delete=models.PROTECT, related_name="links")
    kind = models.CharField(max_length=10, choices=LinkKind.choices)
    contract = models.ForeignKey(
        "operations.Contract", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    vendor = models.ForeignKey(
        "operations.Vendor", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    invoice = models.ForeignKey(
        "operations.Invoice", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    work_order = models.ForeignKey(
        "operations.WorkOrder", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    # Contracts and vendors: what the association is committed to pay them
    # for this project (typed; contracts carry a per-period cap, not a total).
    committed_cents = models.PositiveBigIntegerField(default=0)
    removes = models.OneToOneField(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="removed_by"
    )
    linked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["created_at", "id"]


# --- Insurance claims (D85) ---------------------------------------------------------------


class ClaimState(models.TextChoices):
    REPORTED = "reported", "Reported"
    UNDER_REVIEW = "under_review", "Under review"
    APPROVED = "approved", "Approved"
    DENIED = "denied", "Denied"
    PAID = "paid", "Paid"
    CLOSED = "closed", "Closed"


class InsuranceClaim(models.Model):
    # The association's policy as typed; operations.insurance_claims.policy_for
    # reads the matching documents renewals InsurancePolicy from these.
    carrier = models.CharField(max_length=200)
    policy_number = models.CharField(max_length=80, blank=True)
    incident_on = models.DateField()
    discovered_on = models.DateField(null=True, blank=True)
    description = models.TextField()
    location = models.CharField(max_length=200, blank=True)
    common_area = models.ForeignKey(
        "community.CommonArea", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    work_orders = models.ManyToManyField("operations.WorkOrder", blank=True, related_name="+")
    claim_number = models.CharField(max_length=80, blank=True)
    adjuster_name = models.CharField(max_length=120, blank=True)
    adjuster_contact = models.CharField(max_length=200, blank=True)
    state = models.CharField(max_length=14, choices=ClaimState.choices, default=ClaimState.REPORTED)
    state_changed_at = models.DateTimeField(default=timezone.now)
    estimate_cents = models.PositiveBigIntegerField(null=True, blank=True)
    deductible_cents = models.PositiveBigIntegerField(null=True, blank=True)
    proof_of_loss_due = models.DateField(null=True, blank=True)
    appeal_deadline = models.DateField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-incident_on", "-id"]

    def __str__(self) -> str:
        return f"Claim #{self.pk}"

    def delete(self, *args, **kwargs):
        raise PermissionError("Insurance claims are closed, not deleted.")


class InsuranceClaimEvent(AppendOnlyModel):
    claim = models.ForeignKey(InsuranceClaim, on_delete=models.PROTECT, related_name="events")
    action = models.CharField(max_length=24)
    from_state = models.CharField(max_length=14, blank=True)
    to_state = models.CharField(max_length=14, blank=True)
    note = models.TextField(blank=True)
    by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["at", "id"]


class SettlementOffer(AppendOnlyModel):
    """The insurer's offer as recorded; board members accept it (two above
    rule insurance.second_approval_cents)."""

    claim = models.ForeignKey(InsuranceClaim, on_delete=models.PROTECT, related_name="offers")
    amount_cents = models.PositiveBigIntegerField()
    required_approvals = models.PositiveSmallIntegerField()
    note = models.TextField(blank=True)
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["created_at", "id"]


class SettlementApproval(AppendOnlyModel):
    offer = models.ForeignKey(SettlementOffer, on_delete=models.PROTECT, related_name="approvals")
    approver = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["created_at", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["offer", "approver"], name="settlement_one_approval_per_member"
            )
        ]


class InsuranceMoney(AppendOnlyModel):
    EXPECTED, RECEIVED = "expected", "received"
    claim = models.ForeignKey(InsuranceClaim, on_delete=models.PROTECT, related_name="money")
    kind = models.CharField(max_length=8, choices=[(EXPECTED, "Expected"), (RECEIVED, "Received")])
    amount_cents = models.BigIntegerField(help_text="Negative corrects an earlier line")
    on = models.DateField()
    reference = models.CharField(max_length=80, blank=True)
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["on", "id"]
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(amount_cents=0), name="insurance_money_nonzero"
            )
        ]
