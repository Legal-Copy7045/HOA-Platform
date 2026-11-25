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
