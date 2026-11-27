from django.conf import settings
from django.db import models
from django.utils import timezone


class TaskStatus(models.TextChoices):
    OPEN = "open", "Open"
    DONE = "done", "Done"
    DROPPED = "dropped", "Dropped"


class BoardTask(models.Model):
    """Follow-ups for the board and manager (not visible to owners)."""

    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    assignee = models.ForeignKey(
        "community.Person", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    # A committee's own task (shown in its space); None = a board task.
    committee = models.ForeignKey(
        "governance.Committee", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    due_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=16, choices=TaskStatus.choices, default=TaskStatus.OPEN)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(default=timezone.now)
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["status", "due_date", "id"]

    def __str__(self) -> str:
        return self.title

    def delete(self, *args, **kwargs):
        raise PermissionError("Tasks are closed or dropped, not deleted.")


# --- Vendors, work orders, invoices ---------------------------------------------------

from core.append_only import AppendOnlyModel  # noqa: E402


class ExpenseCategory(models.Model):
    """Chart of expense categories, shared by invoices and budget lines."""

    code = models.CharField(max_length=20, unique=True)
    name = models.CharField(max_length=120)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["code"]

    def __str__(self) -> str:
        return f"{self.code} {self.name}"

    def delete(self, *args, **kwargs):
        raise PermissionError("Categories are deactivated, not deleted.")


class Vendor(models.Model):
    name = models.CharField(max_length=200)
    contact_name = models.CharField(max_length=120, blank=True)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=40, blank=True)
    address = models.TextField(blank=True)
    services = models.CharField(max_length=200, blank=True)
    insurance_expires_on = models.DateField(null=True, blank=True)
    w9_on_file = models.BooleanField(default=False)
    active = models.BooleanField(default=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name

    def delete(self, *args, **kwargs):
        raise PermissionError("Vendors are deactivated, not deleted.")


class Priority(models.TextChoices):
    LOW = "low", "Low"
    NORMAL = "normal", "Normal"
    URGENT = "urgent", "Urgent"


class WorkOrder(models.Model):
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    common_area = models.ForeignKey(
        "community.CommonArea", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    vendor = models.ForeignKey(
        Vendor, null=True, blank=True, on_delete=models.PROTECT, related_name="work_orders"
    )
    priority = models.CharField(max_length=8, choices=Priority.choices, default=Priority.NORMAL)
    state = models.CharField(max_length=16, default="open")
    state_changed_at = models.DateTimeField(default=timezone.now)
    due_on = models.DateField(null=True, blank=True)
    follow_up_on = models.DateField(null=True, blank=True)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Work order #{self.pk} · {self.title}"

    def delete(self, *args, **kwargs):
        raise PermissionError("Work orders are cancelled, not deleted.")


class Schedule(models.TextChoices):
    MONTHLY = "monthly", "Monthly"
    QUARTERLY = "quarterly", "Quarterly"
    ANNUAL = "annual", "Annually"


class Contract(models.Model):
    """A board-approved recurring service contract (landscaping, pond care,
    trash...). Each invoice under it within the cap, one per period, is
    pre-approved; anything over the cap needs the normal approvals. Once
    approved, its terms are fixed (database trigger); a change is a new
    contract."""

    vendor = models.ForeignKey(Vendor, on_delete=models.PROTECT, related_name="contracts")
    title = models.CharField(max_length=200)
    category = models.ForeignKey("ExpenseCategory", on_delete=models.PROTECT, related_name="+")
    schedule = models.CharField(max_length=10, choices=Schedule.choices)
    amount_cap_cents = models.PositiveBigIntegerField(
        help_text="Most that one invoice may be and still be pre-approved."
    )
    starts_on = models.DateField()
    ends_on = models.DateField(null=True, blank=True)
    # The board resolution or minutes that approved it.
    citation = models.CharField(max_length=200)
    state = models.CharField(max_length=12, default="draft")
    state_changed_at = models.DateTimeField(default=timezone.now)
    # Board approvals it needs before it pre-approves anything: the invoice
    # rule applied to its cap (two at or above invoice.dual_approval_cents),
    # fixed when it is entered (review SEC-002). Null on contracts entered
    # before the rule; worked out from the rule when they are approved.
    required_approvals = models.PositiveSmallIntegerField(null=True, blank=True)
    entered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["vendor__name", "title"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount_cap_cents__gt=0), name="contract_cap_positive"
            ),
            models.CheckConstraint(
                condition=models.Q(ends_on__isnull=True)
                | models.Q(ends_on__gte=models.F("starts_on")),
                name="contract_ends_after_start",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.vendor}: {self.title}"

    def delete(self, *args, **kwargs):
        raise PermissionError("Contracts are ended, not deleted.")

    def period_for(self, day) -> str:
        if self.schedule == Schedule.MONTHLY:
            return f"{day.year:04d}-{day.month:02d}"
        if self.schedule == Schedule.QUARTERLY:
            return f"{day.year:04d}-Q{(day.month - 1) // 3 + 1}"
        return f"{day.year:04d}"

    def covers(self, day) -> bool:
        return self.starts_on <= day and (self.ends_on is None or day <= self.ends_on)


class Invoice(models.Model):
    vendor = models.ForeignKey(Vendor, on_delete=models.PROTECT, related_name="invoices")
    contract = models.ForeignKey(
        Contract, null=True, blank=True, on_delete=models.PROTECT, related_name="invoices"
    )
    # The contract period this invoice is for (e.g. 2027-03), when pre-approved.
    contract_period = models.CharField(max_length=10, blank=True)
    work_order = models.ForeignKey(
        WorkOrder, null=True, blank=True, on_delete=models.PROTECT, related_name="invoices"
    )
    number = models.CharField(max_length=60)
    invoice_date = models.DateField()
    due_date = models.DateField(null=True, blank=True)
    amount_cents = models.PositiveBigIntegerField()
    description = models.TextField(blank=True)
    category = models.ForeignKey(ExpenseCategory, on_delete=models.PROTECT, related_name="+")
    state = models.CharField(max_length=24, default="draft")
    state_changed_at = models.DateTimeField(default=timezone.now)
    # Fixed when the invoice is submitted, so changing the threshold rule
    # afterwards cannot reduce the approvals it needs (review A6).
    required_approvals = models.PositiveSmallIntegerField(null=True, blank=True)
    entered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-invoice_date", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["vendor", "number"], name="invoice_unique_per_vendor"),
            # One pre-approved invoice per contract period (void/rejected free it).
            models.UniqueConstraint(
                fields=["contract", "contract_period"],
                condition=~models.Q(contract_period="") & ~models.Q(state__in=["void", "rejected"]),
                name="invoice_one_per_contract_period",
            ),
            models.CheckConstraint(
                condition=models.Q(amount_cents__gt=0), name="invoice_amount_positive"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.vendor} #{self.number}"

    def delete(self, *args, **kwargs):
        raise PermissionError("Invoices are voided, not deleted.")

    conflict_noun = "vendor's invoice"

    def conflict_of_interest(self, user) -> bool:
        """D79: a board member the vendor's conflict disclosure names may not
        approve, pay or otherwise advance its invoices (core.conflicts)."""
        from operations.vendor_onboarding import person_conflicted

        return person_conflicted(user, self.vendor_id)

    def conflict_message(self, actor) -> str:
        return (
            "You are named in this vendor's conflict-of-interest disclosure: another board "
            "member handles its invoices."
        )


class ApprovalDecision(models.TextChoices):
    APPROVE = "approve", "Approve"
    REJECT = "reject", "Reject"


class InvoiceApproval(AppendOnlyModel):
    invoice = models.ForeignKey(Invoice, on_delete=models.PROTECT, related_name="approvals")
    approver = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    decision = models.CharField(max_length=8, choices=ApprovalDecision.choices)
    # The amount the approver saw (review D6).
    amount_cents = models.PositiveBigIntegerField(null=True, blank=True)
    note = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self) -> str:
        return f"approval #{self.pk}"


class ContractApproval(AppendOnlyModel):
    """One board member's approval of a contract (review SEC-002). Never
    edited; the approver never the person who entered it."""

    contract = models.ForeignKey(Contract, on_delete=models.PROTECT, related_name="approvals")
    approver = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    # The cap the approver saw.
    amount_cap_cents = models.PositiveBigIntegerField()
    note = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["created_at", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["contract", "approver"], name="contract_approval_once_per_person"
            ),
        ]

    def __str__(self) -> str:
        return f"contract approval #{self.pk}"


class PaymentMethod(models.TextChoices):
    CHECK = "check", "Check"
    ACH = "ach", "ACH / bank transfer"
    CARD = "card", "Card"
    OTHER = "other", "Other"


class InvoicePayment(AppendOnlyModel):
    """A payment (or, with a negative amount, a reversal). Never edited."""

    invoice = models.ForeignKey(Invoice, on_delete=models.PROTECT, related_name="payments")
    paid_on = models.DateField()
    method = models.CharField(max_length=8, choices=PaymentMethod.choices)
    reference = models.CharField(
        max_length=40,
        blank=True,
        help_text="Check number or short reference - never account numbers.",
    )
    amount_cents = models.BigIntegerField()
    note = models.CharField(max_length=200, blank=True)
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["paid_on", "id"]

    def __str__(self) -> str:
        return f"payment #{self.pk}"


# --- Owner requests (Milestone 3) ------------------------------------------------------

from core.scoping import ScopedModel, ScopedQuerySet  # noqa: E402


class RequestType(models.TextChoices):
    GENERAL = "general", "General question"
    BILLING = "billing", "Billing question"
    MAINTENANCE = "maintenance", "Maintenance report"
    COMMON_AREA = "common_area", "Common area issue"


class RequestState(models.TextChoices):
    SUBMITTED = "submitted", "Submitted"
    IN_PROGRESS = "in_progress", "In progress"
    WAITING = "waiting", "Waiting on you"
    RESOLVED = "resolved", "Resolved"
    CLOSED = "closed", "Closed"
    WITHDRAWN = "withdrawn", "Withdrawn"


OPEN_REQUEST_STATES = (RequestState.SUBMITTED, RequestState.IN_PROGRESS, RequestState.WAITING)


class OwnerRequest(ScopedModel):
    """A question or report from someone on an account (owner, authorized
    contact or tenant). Owners and authorized contacts see their lot's
    requests from their own time; tenants see only their own."""

    lot = models.ForeignKey("community.Lot", on_delete=models.PROTECT, related_name="+")
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    request_type = models.CharField(max_length=16, choices=RequestType.choices)
    subject = models.CharField(max_length=200)
    description = models.TextField()
    common_area = models.ForeignKey(
        "community.CommonArea", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    state = models.CharField(
        max_length=16, choices=RequestState.choices, default=RequestState.SUBMITTED
    )
    state_changed_at = models.DateTimeField(default=timezone.now)
    assignee = models.ForeignKey(
        "community.Person", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    # Response target from the SLA rule for its type, fixed on submission.
    respond_by = models.DateField(null=True, blank=True)
    sla_citation = models.CharField(max_length=200, blank=True)
    first_response_at = models.DateTimeField(null=True, blank=True)
    work_order = models.ForeignKey(
        WorkOrder, null=True, blank=True, on_delete=models.PROTECT, related_name="requests"
    )
    created_at = models.DateTimeField(default=timezone.now)

    objects = ScopedQuerySet.as_manager()
    scope_lot_path = "lot"
    # Own matters (D65)
    conflict_user_fields = ("requested_by",)
    conflict_noun = "request"

    @classmethod
    def scope_for_owner(cls, queryset, roles):
        from core.scoping import owner_era_q

        # Their own, plus their account lots' requests from their own time
        # (owner_era_q covers owners, authorized and view-only contacts and
        # former owners' periods; tenants only ever see their own).
        mine = models.Q(requested_by_id=roles.user_id) if roles.user_id else models.Q(pk__in=[])
        return queryset.filter(mine | owner_era_q(roles))

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Request #{self.pk}"

    @property
    def is_open(self) -> bool:
        return self.state in OPEN_REQUEST_STATES

    @property
    def is_overdue(self) -> bool:
        return bool(
            self.is_open
            and self.first_response_at is None
            and self.respond_by
            and self.respond_by < timezone.localdate()
        )

    def delete(self, *args, **kwargs):
        raise PermissionError("Requests are closed or withdrawn, not deleted.")


# --- Compliance calendar (Milestone 4) -------------------------------------------------


class Recurrence(models.TextChoices):
    ONCE = "once", "Once"
    MONTHLY = "monthly", "Every month"
    QUARTERLY = "quarterly", "Every quarter"
    ANNUAL = "annual", "Every year"


class ComplianceCategory(models.TextChoices):
    INSURANCE = "insurance", "Insurance"
    TAX = "tax", "Tax filing"
    CORPORATE = "corporate", "Corporate filing"
    RESERVE = "reserve", "Reserve study"
    CONTRACT = "contract", "Seasonal or contract work"
    GOVERNANCE = "governance", "Meetings and elections"
    OTHER = "other", "Other"


class ComplianceItem(models.Model):
    """A recurring obligation with a due date and a reminder window."""

    title = models.CharField(max_length=200)
    category = models.CharField(max_length=12, choices=ComplianceCategory.choices)
    due_on = models.DateField()
    recurrence = models.CharField(max_length=10, choices=Recurrence.choices)
    remind_days = models.PositiveSmallIntegerField(default=30)
    responsible = models.ForeignKey(
        "community.Person", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    notes = models.TextField(blank=True)
    citation = models.CharField(max_length=200, blank=True)
    active = models.BooleanField(default=True)
    last_done_on = models.DateField(null=True, blank=True)
    reminded_for = models.DateField(null=True, blank=True)  # due date last reminded
    # The day of the month a recurring item falls on, so "the 31st" stays the
    # last day of each month instead of drifting to the 28th (BUG-015).
    anchor_day = models.PositiveSmallIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["due_on", "title"]

    def __str__(self) -> str:
        return self.title

    def delete(self, *args, **kwargs):
        raise PermissionError("Compliance items are retired, not deleted.")


# --- Vendor onboarding (D79): models in their own module -----------------------------

from operations.onboarding_models import *  # noqa: E402, F403
