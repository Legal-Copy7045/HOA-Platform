from __future__ import annotations

from django.utils import timezone

from audit.services import record_event
from core.policy import require
from operations.models import BoardTask, TaskStatus


class OperationsError(ValueError):
    pass


def create_task(
    *, title: str, description: str = "", assignee=None, due_date=None, actor, request=None
) -> BoardTask:
    require(actor, "task.manage")
    if not title.strip():
        raise OperationsError("A task needs a title.")
    task = BoardTask.objects.create(
        title=title.strip()[:200],
        description=description,
        assignee=assignee,
        due_date=due_date,
        created_by=actor,
    )
    record_event("task.created", actor=actor, request=request, target=task)
    return task


def close_task(task: BoardTask, *, status: str, actor, request=None) -> BoardTask:
    require(actor, "task.manage")
    if status not in (TaskStatus.DONE, TaskStatus.DROPPED) or task.status != TaskStatus.OPEN:
        raise OperationsError("Only open tasks can be closed.")
    task.status, task.closed_at = status, timezone.now()
    task.save(update_fields=["status", "closed_at"])
    record_event(
        "task.closed", actor=actor, request=request, target=task, metadata={"status": status}
    )
    return task


# --- Vendors, work orders, invoices ------------------------------------------------------

import re  # noqa: E402

from django.db import IntegrityError, transaction  # noqa: E402
from django.db.models import Sum  # noqa: E402

from community.rules import rule  # noqa: E402
from core.policy import SYSTEM  # noqa: E402
from core.workflow import Transition, Workflow, WorkflowError  # noqa: E402
from operations.models import (  # noqa: E402
    ApprovalDecision,
    ExpenseCategory,
    Invoice,
    InvoiceApproval,
    InvoicePayment,
    PaymentMethod,
    Vendor,
    WorkOrder,
)


def save_vendor(*, vendor=None, actor, request=None, **fields) -> Vendor:
    require(actor, "vendor.manage")
    if not (fields.get("name") or "").strip():
        raise OperationsError("A vendor needs a name.")
    vendor = vendor or Vendor()
    for key, value in fields.items():
        setattr(vendor, key, value)
    vendor.save()
    record_event("vendor.saved", actor=actor, request=request, target=vendor)
    return vendor


def save_category(*, code: str, name: str, actor, request=None) -> ExpenseCategory:
    require(actor, "budget.manage")
    if not code.strip() or not name.strip():
        raise OperationsError("A category needs a code and a name.")
    try:
        category = ExpenseCategory.objects.create(code=code.strip()[:20], name=name.strip()[:120])
    except IntegrityError as exc:
        raise OperationsError("That code is already used.") from exc
    record_event("category.created", actor=actor, request=request, target=category)
    return category


def _has_vendor(obj, actor):
    return None if obj.vendor_id else "Assign a vendor first."


WORK_ORDER = Workflow(
    "work_order",
    [
        Transition(
            "assign",
            {"open"},
            "assigned",
            "workorder.manage",
            "Assigned to vendor",
            guard=_has_vendor,
        ),
        Transition("start", {"assigned"}, "in_progress", "workorder.manage", "Work started"),
        Transition(
            "complete",
            {"assigned", "in_progress"},
            "completed",
            "workorder.manage",
            "Work completed",
            note_required=True,
        ),
        Transition("close", {"completed"}, "closed", "workorder.manage", "Close"),
        Transition(
            "cancel",
            {"open", "assigned", "in_progress"},
            "cancelled",
            "workorder.manage",
            "Cancel",
            note_required=True,
        ),
    ],
)


def create_work_order(
    *,
    title: str,
    description: str = "",
    common_area=None,
    vendor=None,
    priority: str = "normal",
    due_on=None,
    actor,
    request=None,
) -> WorkOrder:
    require(actor, "workorder.manage")
    if not title.strip():
        raise OperationsError("A work order needs a title.")
    wo = WorkOrder.objects.create(
        title=title.strip()[:200],
        description=description,
        common_area=common_area,
        vendor=vendor,
        priority=priority,
        due_on=due_on,
        requested_by=actor,
    )
    record_event(
        "work_order.created",
        actor=actor,
        request=request,
        target=wo,
        metadata={"priority": priority},
    )
    return wo


def set_work_order_vendor(wo: WorkOrder, vendor: Vendor, *, actor, request=None) -> WorkOrder:
    require(actor, "workorder.manage")
    WorkOrder.objects.filter(pk=wo.pk).update(vendor=vendor)
    wo.vendor = vendor
    record_event("work_order.vendor_set", actor=actor, request=request, target=wo)
    return wo


# Invoices: entered (board or manager), approved by board members who did not
# enter it - one approval under the threshold, two different approvers at or
# above it (rule invoice.dual_approval_cents, default $500).


def approvals_required(invoice: Invoice) -> int:
    threshold = rule("invoice.dual_approval_cents")
    return 2 if threshold is not None and invoice.amount_cents >= threshold else 1


def approvers(invoice: Invoice) -> set[int]:
    return set(
        InvoiceApproval.objects.filter(
            invoice=invoice, decision=ApprovalDecision.APPROVE
        ).values_list("approver_id", flat=True)
    )


def _not_entrant(obj, actor):
    if actor is not SYSTEM and obj.entered_by_id == actor.pk:
        return "You entered this invoice; other board members must approve it."
    return None


def _enough_approvals(obj, actor):
    have, need = len(approvers(obj)), approvals_required(obj)
    return None if have >= need else f"{need - have} more approval(s) needed."


def _fully_paid(obj, actor):
    return None if paid_cents(obj) >= obj.amount_cents else "Not fully paid yet."


INVOICE = Workflow(
    "invoice",
    [
        Transition("submit", {"draft"}, "pending_approval", "invoice.enter", "Submit for approval"),
        Transition(
            "approve",
            {"pending_approval"},
            "approved",
            "invoice.approve",
            "Approved",
            guard=_enough_approvals,
        ),
        Transition(
            "reject",
            {"pending_approval"},
            "rejected",
            "invoice.approve",
            "Reject",
            guard=_not_entrant,
            note_required=True,
        ),
        Transition(
            "mark_paid",
            {"approved"},
            "paid",
            "invoice.record_payment",
            "Paid in full",
            guard=_fully_paid,
        ),
        Transition(
            "void",
            {"draft", "pending_approval", "rejected"},
            "void",
            "invoice.enter",
            "Void",
            note_required=True,
        ),
    ],
)


def create_invoice(
    *,
    vendor: Vendor,
    number: str,
    invoice_date,
    amount_cents: int,
    category,
    due_date=None,
    description: str = "",
    work_order=None,
    actor,
    request=None,
) -> Invoice:
    require(actor, "invoice.enter")
    if amount_cents <= 0 or not number.strip():
        raise OperationsError("An invoice needs a number and a positive amount.")
    try:
        with transaction.atomic():
            invoice = Invoice.objects.create(
                vendor=vendor,
                number=number.strip()[:60],
                invoice_date=invoice_date,
                amount_cents=amount_cents,
                category=category,
                due_date=due_date,
                description=description,
                work_order=work_order,
                entered_by=actor,
            )
    except IntegrityError as exc:
        raise OperationsError("This vendor already has an invoice with that number.") from exc
    record_event(
        "invoice.entered",
        actor=actor,
        request=request,
        target=invoice,
        metadata={"amount_cents": amount_cents},
    )
    return invoice


def approve(invoice: Invoice, *, note: str = "", actor, request=None) -> Invoice:
    require(actor, "invoice.approve")
    with transaction.atomic():
        invoice = Invoice.objects.select_for_update().get(pk=invoice.pk)
        if invoice.state != "pending_approval":
            raise OperationsError("Only invoices awaiting approval can be approved.")
        problem = _not_entrant(invoice, actor)
        if problem:
            raise OperationsError(problem)
        if actor.pk in approvers(invoice):
            raise OperationsError(
                "You have already approved this invoice; a different board "
                "member must give the second approval."
            )
        InvoiceApproval.objects.create(
            invoice=invoice, approver=actor, decision=ApprovalDecision.APPROVE, note=note
        )
        record_event(
            "invoice.approval_given",
            actor=actor,
            request=request,
            target=invoice,
            metadata={"have": len(approvers(invoice)), "need": approvals_required(invoice)},
        )
        if _enough_approvals(invoice, actor) is None:
            invoice = INVOICE.apply(
                invoice, "approve", actor=actor, request=request, note="Required approvals received"
            )
    return invoice


def reject(invoice: Invoice, *, note: str, actor, request=None) -> Invoice:
    with transaction.atomic():
        InvoiceApproval.objects.create(
            invoice=invoice, approver=actor, decision=ApprovalDecision.REJECT, note=note
        )
        return INVOICE.apply(invoice, "reject", actor=actor, note=note, request=request)


def paid_cents(invoice: Invoice) -> int:
    return InvoicePayment.objects.filter(invoice=invoice).aggregate(s=Sum("amount_cents"))["s"] or 0


_ACCOUNT_NUMBER = re.compile(r"\d{9,}")


def record_payment(
    invoice: Invoice,
    *,
    paid_on,
    method: str,
    amount_cents: int,
    reference: str = "",
    note: str = "",
    actor,
    request=None,
) -> InvoicePayment:
    require(actor, "invoice.record_payment")
    if method not in PaymentMethod.values:
        raise OperationsError("Choose a payment method.")
    if _ACCOUNT_NUMBER.search(reference.replace(" ", "").replace("-", "")):
        raise OperationsError("Enter a check number or short reference, never an account number.")
    with transaction.atomic():
        invoice = Invoice.objects.select_for_update().get(pk=invoice.pk)
        if invoice.state != "approved":
            raise OperationsError("Only approved invoices can be paid.")
        remaining = invoice.amount_cents - paid_cents(invoice)
        if amount_cents == 0 or amount_cents > remaining:
            raise OperationsError(
                f"Amount must be between 1 cent and the remaining {remaining} cents."
            )
        payment = InvoicePayment.objects.create(
            invoice=invoice,
            paid_on=paid_on,
            method=method,
            reference=reference[:40],
            note=note[:200],
            amount_cents=amount_cents,
            recorded_by=actor,
        )
        record_event(
            "invoice.payment_recorded",
            actor=actor,
            request=request,
            target=invoice,
            metadata={"amount_cents": amount_cents, "method": method},
        )
        if paid_cents(invoice) >= invoice.amount_cents:
            INVOICE.apply(invoice, "mark_paid", actor=actor, request=request)
    return payment


def ensure_workflow_error_is_operations_error(exc: WorkflowError) -> OperationsError:
    return OperationsError(str(exc))
