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
        return f"WO-{self.pk} {self.title}"

    def delete(self, *args, **kwargs):
        raise PermissionError("Work orders are cancelled, not deleted.")


class Invoice(models.Model):
    vendor = models.ForeignKey(Vendor, on_delete=models.PROTECT, related_name="invoices")
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
            models.CheckConstraint(
                condition=models.Q(amount_cents__gt=0), name="invoice_amount_positive"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.vendor} #{self.number}"

    def delete(self, *args, **kwargs):
        raise PermissionError("Invoices are voided, not deleted.")


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
