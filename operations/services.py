from __future__ import annotations

from django.utils import timezone

from audit.services import record_event
from core import conflicts
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
from core.workflow import NotForYou, Transition, Workflow, WorkflowError  # noqa: E402
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


def _approvals_by_rule(invoice: Invoice) -> int:
    threshold = rule("invoice.dual_approval_cents")
    return 2 if threshold is not None and invoice.amount_cents >= threshold else 1


def approvals_required(invoice: Invoice) -> int:
    if invoice.required_approvals is not None:
        return invoice.required_approvals
    return _approvals_by_rule(invoice)


def _fix_required_approvals(obj, actor) -> None:
    """On submit: fix the approvals the invoice needs. An invoice under an
    active contract, within its cap and the first for its period, needs none:
    the board approved it in advance (recorded, and audited as such)."""
    period = _contract_period(obj)
    if period:
        Invoice.objects.filter(pk=obj.pk).update(required_approvals=0, contract_period=period)
        obj.required_approvals, obj.contract_period = 0, period
        INVOICE.apply(
            obj, "approve", actor=SYSTEM, note=f"Pre-approved under contract #{obj.contract_id}"
        )
        record_event(
            "invoice.contract_preapproved",
            actor=None,
            target=obj,
            metadata={"contract_id": obj.contract_id, "period": period},
        )
        return
    Invoice.objects.filter(pk=obj.pk).update(required_approvals=_approvals_by_rule(obj))


def _contract_period(invoice) -> str:
    """The contract period this invoice is pre-approved for, or "" if it is not."""
    if not invoice.contract_id:
        return ""
    contract = Contract.objects.get(pk=invoice.contract_id)
    from operations.vendor_onboarding import is_active as vendor_is_active

    if not vendor_is_active(invoice.vendor_id):
        return ""  # D79: an unapproved vendor's invoice is never pre-approved
    if (
        contract.state != "active"
        or contract.vendor_id != invoice.vendor_id
        or invoice.amount_cents > contract.amount_cap_cents
        or not contract.covers(invoice.invoice_date)
    ):
        return ""
    # Pre-approval stands in for the invoice's own approvals, so the contract
    # must carry as many board approvals as this invoice would need (review
    # SEC-002). Contracts approved by one member before that rule keep
    # pre-approving only invoices below the two-approver threshold.
    if len(contract_approvers(contract)) < _approvals_by_rule(invoice):
        return ""
    period = contract.period_for(invoice.invoice_date)
    taken = (
        Invoice.objects.filter(contract=contract, contract_period=period)
        .exclude(state__in=["void", "rejected"])
        .exclude(pk=invoice.pk)
        .exists()
    )
    return "" if taken else period


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
        Transition(
            "submit",
            {"draft"},
            "pending_approval",
            "invoice.enter",
            "Submit for approval",
            confirm="Submit this invoice? Board members other than you are asked to approve it.",
            after=_fix_required_approvals,
        ),
        Transition(
            "approve",
            {"pending_approval"},
            "approved",
            "invoice.approve",
            "Approved",
            confirm="Approve this invoice for payment?",
            guard=_enough_approvals,
        ),
        Transition(
            "reject",
            {"pending_approval"},
            "rejected",
            "invoice.approve",
            "Reject",
            confirm="Reject this invoice? It will not be paid.",
            guard=_not_entrant,
            note_required=True,
        ),
        Transition(
            "mark_paid",
            {"approved"},
            "paid",
            "invoice.record_payment",
            "Paid in full",
            confirm="Record this invoice as paid in full?",
            guard=_fully_paid,
        ),
        # D77: a voided vendor check reopens a paid invoice for a new payment.
        # Taken only by operations/vendor_checks.py (as SYSTEM).
        Transition(
            "payment_voided",
            {"paid"},
            "approved",
            "invoice.record_payment",
            "Check voided: awaiting payment again",
            guard=lambda obj, actor: (
                None if actor is SYSTEM else NotForYou("Recorded when a check is voided.")
            ),
        ),
        Transition(
            "void",
            {"draft", "pending_approval", "rejected"},
            "void",
            "invoice.enter",
            "Void",
            confirm="Void this invoice? It stays on record but can't be paid.",
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
    contract=None,
    actor,
    request=None,
) -> Invoice:
    require(actor, "invoice.enter")
    if amount_cents <= 0 or not number.strip():
        raise OperationsError("An invoice needs a number and a positive amount.")
    if contract is not None and contract.vendor_id != vendor.pk:
        raise OperationsError("That contract is with a different vendor.")
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
                contract=contract,
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


def _vendor_may_be_paid(invoice: Invoice, actor, request=None) -> None:
    """D79: an unapproved or inactive vendor's invoices are neither approved
    nor paid (older vendors are grandfathered as active), and a board member
    the vendor's conflict disclosure names takes no step on them (D65)."""
    from operations.vendor_onboarding import invoice_block_reason

    problem = invoice_block_reason(invoice.vendor)
    if problem:
        raise OperationsError(problem)
    conflicts.refuse(actor, invoice, area="invoice", request=request, error=OperationsError)


def approve(invoice: Invoice, *, note: str = "", actor, request=None) -> Invoice:
    require(actor, "invoice.approve")
    _vendor_may_be_paid(invoice, actor, request)
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
            invoice=invoice,
            approver=actor,
            decision=ApprovalDecision.APPROVE,
            amount_cents=invoice.amount_cents,
            note=note,
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
            invoice=invoice,
            approver=actor,
            decision=ApprovalDecision.REJECT,
            amount_cents=invoice.amount_cents,
            note=note,
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
    from ledger.books.periods import refuse_closed  # no posting into a closed month (D70)

    refuse_closed(paid_on, error=OperationsError, what="The payment date")
    _vendor_may_be_paid(invoice, actor, request)
    with transaction.atomic():
        invoice = Invoice.objects.select_for_update().get(pk=invoice.pk)
        if invoice.state != "approved":
            raise OperationsError("Only approved invoices can be paid.")
        remaining = invoice.amount_cents - paid_cents(invoice)
        if not isinstance(amount_cents, int) or amount_cents <= 0 or amount_cents > remaining:
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


# --- Owner requests (Milestone 3) ------------------------------------------------------

from community.rules import rule_with_citation  # noqa: E402
from core.dates import add_business_days  # noqa: E402
from operations.models import (  # noqa: E402
    OwnerRequest,
    RequestState,
    RequestType,
)

MAX_OPEN_REQUESTS = 20


def _raise_lot_ids(actor) -> frozenset[int]:
    """Lots this person may raise requests (and add photos) for: owned lots,
    authorized-contact lots and tenancies. Never a view-only contact's lot,
    even when they own another home (review R2-009)."""
    from core.policy import roles_for

    roles = roles_for(actor)
    return roles.owned_lot_ids | roles.authorized_lot_ids | roles.tenant_lot_ids


def _may_raise_for(actor, lot_id: int) -> bool:
    return lot_id in _raise_lot_ids(actor)


def request_lots(actor):
    """Lots this person may raise a request for (owned, authorized contact or
    tenancy)."""
    from community.services import lots_by_ids

    return lots_by_ids(_raise_lot_ids(actor))


def submit_request(
    *, lot, request_type: str, subject: str, description: str, common_area=None, actor, request=None
) -> OwnerRequest:
    require(actor, "request.submit")
    if not _may_raise_for(actor, lot.pk):
        raise OperationsError("You can raise requests only for your own home.")
    if request_type not in RequestType.values:
        raise OperationsError("Choose what kind of request this is.")
    subject = " ".join((subject or "").split())[:200]
    if not subject or not (description or "").strip():
        raise OperationsError("Add a subject and a description.")
    if len(description) > 10_000:
        raise OperationsError("Please keep the description under 10,000 characters.")
    open_count = OwnerRequest.objects.unscoped("open request cap").filter(
        requested_by=actor, state__in=[s.value for s in OPEN_STATES]
    )
    if open_count.count() >= MAX_OPEN_REQUESTS:
        raise OperationsError("You have many open requests; please wait for some to be answered.")
    days, citation = rule_with_citation(f"request.sla_days.{request_type}")
    today = timezone.localdate()
    with transaction.atomic():
        req = OwnerRequest.objects.create(
            lot=lot,
            requested_by=actor,
            request_type=request_type,
            subject=subject,
            description=description.strip(),
            common_area=common_area,
            respond_by=add_business_days(today, days) if days else None,
            sla_citation=citation or "",
        )
        record_event(
            "request.submitted",
            actor=actor,
            request=request,
            target=req,
            metadata={"type": request_type, "lot_id": lot.pk},
        )
    from comms.threads import notify_staff

    notify_staff("request", req)
    return req


OPEN_STATES = (RequestState.SUBMITTED, RequestState.IN_PROGRESS, RequestState.WAITING)


def _is_requester(obj, actor):
    if actor is SYSTEM:
        return None
    if obj.requested_by_id != actor.pk:
        return "Only the person who raised this request can do that."
    if not _may_raise_for(actor, obj.lot_id):
        # Now view-only on that account, or a former owner: read-only.
        return "You can read this request but no longer act on it."
    return None


def mark_request_responded(req: OwnerRequest) -> None:
    """The first staff reply or step stops the response clock."""
    OwnerRequest.objects.unscoped("first response").filter(
        pk=req.pk, first_response_at__isnull=True
    ).update(first_response_at=timezone.now())


def _mark_responded(obj, actor) -> None:
    mark_request_responded(obj)


OWNER_REQUEST = Workflow(
    "request",
    [
        Transition(
            "start",
            {RequestState.SUBMITTED, RequestState.WAITING},
            RequestState.IN_PROGRESS,
            "request.manage",
            "Working on it",
            after=_mark_responded,
        ),
        Transition(
            "ask_owner",
            {RequestState.SUBMITTED, RequestState.IN_PROGRESS},
            RequestState.WAITING,
            "request.manage",
            "Ask the owner for more",
            note_required=True,
            after=_mark_responded,
        ),
        Transition(
            "resolve",
            set(OPEN_STATES),
            RequestState.RESOLVED,
            "request.manage",
            "Resolved",
            note_required=True,
            after=_mark_responded,
        ),
        Transition(
            "reopen",
            {RequestState.RESOLVED},
            RequestState.IN_PROGRESS,
            "request.submit",
            "Not resolved: reopen",
            guard=_is_requester,
            note_required=True,
        ),
        Transition(
            "close", {RequestState.RESOLVED}, RequestState.CLOSED, "request.manage", "Close"
        ),
        Transition(
            "withdraw",
            set(OPEN_STATES),
            RequestState.WITHDRAWN,
            "request.submit",
            "Withdraw",
            guard=_is_requester,
        ),
    ],
)


def assign_request(req: OwnerRequest, person, *, actor, request=None) -> OwnerRequest:
    """Assign to a board member or manager (``person`` None clears it)."""
    require(actor, "request.manage")
    conflicts.refuse(
        actor, req, area="request", action="assign", request=request, error=OperationsError
    )
    if person is not None:
        from core.policy import can

        if not (person.user_id and can(person.user, "request.manage")):
            raise OperationsError("Assign requests to a board member or the manager.")
    OwnerRequest.objects.unscoped("assign").filter(pk=req.pk).update(assignee=person)
    req.assignee = person
    record_event(
        "request.assigned",
        actor=actor,
        request=request,
        target=req,
        metadata={"person_id": person.pk if person else None},
    )
    return req


def can_add_request_photos(actor, req: OwnerRequest) -> bool:
    """The person who raised an open request may add photos, while they may
    still raise requests for that home (not view-only, not a former owner);
    staff who work requests may too."""
    from core.policy import can

    if actor is SYSTEM or not getattr(actor, "is_authenticated", False):
        return False
    if can(actor, "request.manage") and not conflicts.own_matter(actor, req):
        return req.state not in (RequestState.WITHDRAWN, RequestState.CLOSED)
    return (
        req.is_open
        and req.requested_by_id == actor.pk
        and can(actor, "request.submit")
        and _may_raise_for(actor, req.lot_id)
    )


def request_to_work_order(req: OwnerRequest, *, actor, request=None) -> WorkOrder:
    """A maintenance or common-area report becomes a work order (once)."""
    require(actor, "workorder.manage")
    conflicts.refuse(
        actor, req, area="request", action="work_order", request=request, error=OperationsError
    )
    if req.request_type not in (RequestType.MAINTENANCE, RequestType.COMMON_AREA):
        raise OperationsError("Only maintenance and common area reports become work orders.")
    with transaction.atomic():
        locked = OwnerRequest.objects.unscoped("to work order").select_for_update().get(pk=req.pk)
        if locked.work_order_id:
            raise OperationsError("This request already has a work order.")
        wo = create_work_order(
            title=req.subject,
            description=f"From owner request #{req.pk}.\n\n{req.description}",
            common_area=req.common_area,
            actor=actor,
            request=request,
        )
        OwnerRequest.objects.unscoped("to work order").filter(pk=req.pk).update(work_order=wo)
        record_event(
            "request.work_order_created",
            actor=actor,
            request=request,
            target=req,
            metadata={"work_order_id": wo.pk},
        )
    if req.state == RequestState.SUBMITTED:
        OWNER_REQUEST.apply(req, "start", actor=actor, request=request)
    req.work_order = wo
    return wo


def set_work_order_follow_up(wo: WorkOrder, on, *, actor, request=None) -> WorkOrder:
    require(actor, "workorder.manage")
    WorkOrder.objects.filter(pk=wo.pk).update(follow_up_on=on)
    wo.follow_up_on = on
    record_event(
        "followup.set",
        actor=actor,
        request=request,
        target=wo,
        metadata={"kind": "work_order", "object_id": wo.pk, "on": on.isoformat() if on else None},
    )
    return wo


# --- Committee tasks (authorization by governance.services) ---------------------------


def create_committee_task(
    *, committee_id: int, title: str, due_date=None, assignee=None, actor, request=None
) -> BoardTask:
    """Called only by governance.services after its membership check."""
    from governance.models import Committee
    from governance.services import require_committee_member

    require_committee_member(actor, Committee.objects.get(pk=committee_id))
    if not title.strip():
        raise OperationsError("A task needs a title.")
    task = BoardTask.objects.create(
        title=title.strip()[:200],
        assignee=assignee,
        due_date=due_date,
        committee_id=committee_id,
        created_by=actor,
    )
    record_event("task.created", actor=actor, request=request, target=task)
    return task


def close_committee_task(
    task: BoardTask, *, committee_id: int, status: str, actor, request=None
) -> BoardTask:
    from governance.models import Committee
    from governance.services import require_committee_member

    require_committee_member(actor, Committee.objects.get(pk=committee_id))
    if task.committee_id != committee_id:
        raise OperationsError("That task belongs to another committee.")
    if status not in (TaskStatus.DONE, TaskStatus.DROPPED) or task.status != TaskStatus.OPEN:
        raise OperationsError("Only open tasks can be closed.")
    BoardTask.objects.filter(pk=task.pk).update(status=status, closed_at=timezone.now())
    task.status = status
    record_event(
        "task.closed", actor=actor, request=request, target=task, metadata={"status": status}
    )
    return task


# --- Contracts (recurring invoices, Milestone 3) -------------------------------------

from operations.models import Contract, ContractApproval, Schedule  # noqa: E402


def _contract_approvals_by_rule(cap_cents: int) -> int:
    """The invoice rule applied to a contract's cap: two different board
    approvals at or above invoice.dual_approval_cents, otherwise one."""
    threshold = rule("invoice.dual_approval_cents")
    return 2 if threshold is not None and cap_cents >= threshold else 1


def contract_approvals_required(contract) -> int:
    if contract.required_approvals is not None:
        return contract.required_approvals
    return _contract_approvals_by_rule(contract.amount_cap_cents)


def contract_approvers(contract) -> set[int]:
    """Board members who approved it, never counting the person who entered it."""
    return set(
        ContractApproval.objects.filter(contract=contract)
        .exclude(approver_id=contract.entered_by_id)
        .values_list("approver_id", flat=True)
    )


def _not_contract_entrant(obj, actor):
    if actor is not SYSTEM and obj.entered_by_id == actor.pk:
        return "You entered this contract; another board member must approve it."
    return None


def _contract_ready(obj, actor):
    problem = _not_contract_entrant(obj, actor)
    if problem:
        return problem
    have, need = len(contract_approvers(obj)), contract_approvals_required(obj)
    return None if have >= need else f"{need - have} more approval(s) needed."


CONTRACT = Workflow(
    "contract",
    [
        # Applied only by approve_contract once enough approvals are recorded.
        Transition(
            "approve",
            {"draft"},
            "active",
            "invoice.approve",
            "Approved by the board",
            guard=_contract_ready,
            note_required=True,
        ),
        Transition("discard", {"draft"}, "discarded", "contract.manage", "Discard"),
    ],
)


def approve_contract(contract: Contract, *, note: str, actor, request=None) -> Contract:
    """Record one board member's approval; the contract becomes active (and
    starts pre-approving invoices) once it has the approvals it needs - two
    different members, neither the entrant, when its cap is at or above the
    invoice two-approver threshold (review SEC-002)."""
    require(actor, "invoice.approve")
    if not note.strip():
        raise OperationsError("Please add a note: the minutes or resolution that approve it.")
    with transaction.atomic():
        contract = Contract.objects.select_for_update().get(pk=contract.pk)
        if contract.state != "draft":
            raise OperationsError("Only a contract waiting for approval can be approved.")
        problem = _not_contract_entrant(contract, actor)
        if problem:
            raise OperationsError(problem)
        if actor.pk in contract_approvers(contract):
            raise OperationsError(
                "You have already approved this contract; a different board "
                "member must give the second approval."
            )
        ContractApproval.objects.create(
            contract=contract,
            approver=actor,
            amount_cap_cents=contract.amount_cap_cents,
            note=note.strip()[:4000],
        )
        record_event(
            "contract.approval_given",
            actor=actor,
            request=request,
            target=contract,
            metadata={
                "have": len(contract_approvers(contract)),
                "need": contract_approvals_required(contract),
            },
        )
        if _contract_ready(contract, actor) is None:
            contract = CONTRACT.apply(contract, "approve", actor=actor, request=request, note=note)
    return contract


def create_contract(
    *,
    vendor: Vendor,
    title: str,
    category,
    schedule: str,
    amount_cap_cents: int,
    starts_on,
    citation: str,
    ends_on=None,
    actor,
    request=None,
) -> Contract:
    require(actor, "contract.manage")
    title = " ".join((title or "").split())[:200]
    citation = " ".join((citation or "").split())[:200]
    if not title or not citation:
        raise OperationsError("A contract needs a title and the resolution that approves it.")
    if schedule not in Schedule.values:
        raise OperationsError("Choose how often the vendor invoices.")
    if not amount_cap_cents or amount_cap_cents <= 0:
        raise OperationsError("Set the most one invoice may be.")
    if ends_on and ends_on < starts_on:
        raise OperationsError("A contract cannot end before it starts.")
    contract = Contract.objects.create(
        vendor=vendor,
        title=title,
        category=category,
        schedule=schedule,
        amount_cap_cents=amount_cap_cents,
        starts_on=starts_on,
        ends_on=ends_on,
        citation=citation,
        entered_by=actor,
        # Fixed now, so changing the threshold rule later cannot reduce it.
        required_approvals=_contract_approvals_by_rule(amount_cap_cents),
    )
    record_event(
        "contract.entered",
        actor=actor,
        request=request,
        target=contract,
        metadata={"amount_cap_cents": amount_cap_cents, "schedule": schedule},
    )
    return contract


def end_contract(contract: Contract, *, ends_on, actor, request=None) -> Contract:
    """Set the last day an active contract covers (once)."""
    require(actor, "contract.manage")
    with transaction.atomic():
        contract = Contract.objects.select_for_update().get(pk=contract.pk)
        if contract.state != "active" or contract.ends_on is not None:
            raise OperationsError("Only an open-ended active contract can be given an end date.")
        if ends_on < contract.starts_on:
            raise OperationsError("A contract cannot end before it starts.")
        Contract.objects.filter(pk=contract.pk).update(ends_on=ends_on)
        contract.ends_on = ends_on
    record_event(
        "contract.ended",
        actor=actor,
        request=request,
        target=contract,
        metadata={"ends_on": ends_on.isoformat()},
    )
    return contract


def contract_status(contract: Contract, on=None) -> dict:
    """This period's invoice (if received) and spend to date."""
    on = on or timezone.localdate()
    period = contract.period_for(on)
    invoices = list(contract.invoices.exclude(state__in=["void", "rejected"]))
    return {
        "period": period,
        "this_period": next((i for i in invoices if i.contract_period == period), None),
        "total_cents": sum(i.amount_cents for i in invoices),
        "count": len(invoices),
    }


# --- Compliance calendar (Milestone 4) ------------------------------------------------

import calendar as _calendar  # noqa: E402
import datetime as _dt  # noqa: E402

from operations.models import ComplianceItem, Recurrence  # noqa: E402


def _add_months(day: _dt.date, months: int, anchor: int | None = None) -> _dt.date:
    month = day.month - 1 + months
    year, month = day.year + month // 12, month % 12 + 1
    want = anchor or day.day
    return _dt.date(year, month, min(want, _calendar.monthrange(year, month)[1]))


def next_due(item: ComplianceItem) -> _dt.date | None:
    step = {Recurrence.MONTHLY: 1, Recurrence.QUARTERLY: 3, Recurrence.ANNUAL: 12}.get(
        item.recurrence
    )
    return _add_months(item.due_on, step, item.anchor_day) if step else None


def save_compliance_item(*, item=None, actor, request=None, **fields) -> ComplianceItem:
    require(actor, "compliance.manage")
    title = " ".join((fields.get("title") or "").split())[:200]
    if not title or not fields.get("due_on"):
        raise OperationsError("An item needs a title and a due date.")
    fields["title"] = title
    from operations.models import ComplianceCategory

    if fields.get("category") not in ComplianceCategory.values:
        raise OperationsError("Choose a category.")
    if fields.get("recurrence") not in Recurrence.values:
        raise OperationsError("Choose how often it repeats.")
    remind = fields.get("remind_days", 30)
    if not isinstance(remind, int) or not 0 <= remind <= 365:
        raise OperationsError("Remind between 0 and 365 days before.")
    fields["anchor_day"] = fields["due_on"].day
    if item is None:
        item = ComplianceItem.objects.create(**fields)
    else:
        ComplianceItem.objects.filter(pk=item.pk).update(**fields)
        item.refresh_from_db()
    record_event("compliance.saved", actor=actor, request=request, target=item)
    return item


def complete_compliance_item(
    item: ComplianceItem, *, actor, done_on=None, seen_due_on=None, request=None
):
    """Mark done; a recurring item moves to its next due date. Only if it is
    still due on the date the person saw, so a double-click cannot skip a
    period (BUG-015)."""
    require(actor, "compliance.manage")
    done_on = done_on or timezone.localdate()
    nxt = next_due(item)
    fields = {"last_done_on": done_on}
    if nxt:
        fields.update(due_on=nxt, reminded_for=None)
    else:
        fields.update(active=False)
    updated = ComplianceItem.objects.filter(
        pk=item.pk, due_on=seen_due_on or item.due_on, active=True
    ).update(**fields)
    if not updated:
        raise OperationsError("Already marked done.")
    record_event(
        "compliance.done",
        actor=actor,
        request=request,
        target=item,
        metadata={"recurring": bool(nxt)},
    )
    item.refresh_from_db()
    return item


def due_soon(on=None):
    on = on or timezone.localdate()
    out = []
    for item in ComplianceItem.objects.filter(active=True):
        if item.due_on - _dt.timedelta(days=item.remind_days) <= on:
            out.append(item)
    return out


def send_compliance_reminders(on=None) -> int:
    """Email the board once per due date when an item enters its window."""
    from django.conf import settings

    from community.services import board_emails

    on = on or timezone.localdate()
    fresh = [i for i in due_soon(on) if i.reminded_for != i.due_on]
    if not fresh:
        return 0
    lines = "\n".join(
        f"- {i.title}: due {i.due_on:%B %-d, %Y}" + (" (overdue)" if i.due_on < on else "")
        for i in fresh
    )
    body = (
        f"Coming up on the compliance calendar:\n\n{lines}\n\n"
        f"{settings.PORTAL_BASE_URL}/board/compliance/\n\n-- \n{settings.SITE_NAME}"
    )
    from comms.notify import mail_board

    mail_board(board_emails(), subject="Compliance reminders", body=body)
    for i in fresh:
        ComplianceItem.objects.filter(pk=i.pk).update(reminded_for=i.due_on)
    return len(fresh)


COMPLIANCE_STARTERS = [
    # (title, category, month, day, recurrence, remind_days, notes)
    (
        "Association insurance renewal (property, liability, D&O, fidelity)",
        "insurance",
        None,
        None,
        "annual",
        60,
        "Get renewal quotes; check limits against the Declaration.",
    ),
    (
        "Federal tax return (Form 1120-H)",
        "tax",
        4,
        15,
        "annual",
        45,
        "Due the 15th day of the 4th month after the fiscal year ends.",
    ),
    (
        "Pennsylvania corporate annual report",
        "corporate",
        None,
        None,
        "annual",
        45,
        "Check the due date with the PA Department of State.",
    ),
    (
        "Reserve study update",
        "reserve",
        None,
        None,
        "annual",
        90,
        "Many associations update every 3-5 years; set the real date.",
    ),
    (
        "Annual owners' meeting and election",
        "governance",
        None,
        None,
        "annual",
        75,
        "Notice to owners 10 to 60 days before (68 Pa.C.S. § 5308).",
    ),
    ("Snow removal contract", "contract", 10, 1, "annual", 30, ""),
]


def add_starter_items(*, actor) -> int:
    """Suggested items to edit; dates the board doesn't know yet are set a year
    out so they show up for review."""
    require(actor, "compliance.manage")
    today = timezone.localdate()
    added = 0
    for title, category, month, day, recurrence, remind, notes in COMPLIANCE_STARTERS:
        if ComplianceItem.objects.filter(title=title).exists():
            continue
        if month:
            due = _dt.date(today.year, month, day)
            if due < today:
                due = _dt.date(today.year + 1, month, day)
        else:
            due = _add_months(today, 12)
        ComplianceItem.objects.create(
            title=title,
            category=category,
            due_on=due,
            recurrence=recurrence,
            remind_days=remind,
            notes=notes,
        )
        added += 1
    return added
