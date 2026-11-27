"""Capital projects (D83, optional feature "capital_projects").

A project has a budget, its funding (reserve fund, operating, a special
assessment or a mix) and a state:

    planned -> approved -> in_progress -> completed
    (planned, approved or in_progress) -> cancelled

Approval is a board decision citing the board resolution; at or above rule
``capital.second_approval_cents`` two different board members approve (the
number is fixed when approval is first asked). The manager keeps the record:
details, links to existing contracts, vendors, invoices and work orders,
progress, photos and permits. Once approved the budget changes only by
change orders (append-only, positive or negative, with a reason), each
approved by a board member other than the one who proposed it, two at or
above the same threshold.

Figures: budget (original plus approved change orders), committed (what
the linked contracts and vendors are committed to for this project, typed
when linking) and spent (payments on the linked invoices). Owners see only
the projects the board shows them: title, status, budget and spent.

For claude/books-and-close at merge: ``spend_by_project`` gives each
project's fund and the invoices it paid, so statements can show capital
spending by project and fund.
"""

from __future__ import annotations

from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.utils import timezone

from audit import actions as audit_actions
from audit.services import record_event
from community.rules import rule
from core.policy import can, require
from operations.models import Contract, Invoice, InvoicePayment, Vendor, WorkOrder
from operations.models_options import (
    CapitalProject,
    CapitalProjectApproval,
    CapitalProjectEvent,
    CapitalProjectLink,
    ChangeOrder,
    ChangeOrderDecision,
    Funding,
    LinkKind,
    ProjectState,
)

FEATURE = "capital_projects"
FILE_LABEL = "Project file"

for _name, _text, _fields in (
    ("capital.created", "Capital project created", ("project_id", "budget_cents", "funding")),
    ("capital.updated", "Capital project details changed", ("project_id",)),
    ("capital.approval_asked", "Capital project sent for approval", ("project_id", "required")),
    ("capital.approved", "Capital project approval given", ("project_id", "approvals", "required")),
    ("capital.state_changed", "Capital project status changed", ("project_id", "from", "to")),
    (
        "capital.change_order_proposed",
        "Change order proposed",
        ("project_id", "amount_cents", "required"),
    ),
    ("capital.change_order_decided", "Change order decided", ("change_order_id", "decision")),
    ("capital.linked", "Record linked to a capital project", ("project_id", "kind")),
    ("capital.unlinked", "Record unlinked from a capital project", ("project_id", "kind")),
):
    audit_actions.register(audit_actions.ActionSpec(_name, _text, frozenset(_fields)))


class ProjectError(ValueError):
    """A refusal the page shows."""


# --- Adapter (switch at merge) -----------------------------------------------------------


def special_assessment_label(project: CapitalProject) -> str:
    """Switch to claude/finance-gaps at merge: link the special assessment
    record. Interim: the reference the board typed."""
    return project.special_assessment_ref


# --- Reading -------------------------------------------------------------------------------


def _threshold() -> int:
    return rule("capital.second_approval_cents") or 0


def approvals_needed(amount_cents: int) -> int:
    threshold = _threshold()
    return 2 if threshold and abs(amount_cents) >= threshold else 1


def active_links(project: CapitalProject) -> list[CapitalProjectLink]:
    links = list(
        project.links.select_related("contract__vendor", "vendor", "invoice__vendor", "work_order")
    )
    removed = {link.removes_id for link in links if link.removes_id}
    return [link for link in links if not link.removes_id and link.pk not in removed]


def change_order_status(co: ChangeOrder) -> str:
    decisions = list(co.decisions.all())
    if any(d.decision == ChangeOrderDecision.DECLINE for d in decisions):
        return "declined"
    if sum(1 for d in decisions if d.decision == ChangeOrderDecision.APPROVE) >= (
        co.required_approvals
    ):
        return "approved"
    return "pending"


def figures(project: CapitalProject, links=None) -> dict:
    links = active_links(project) if links is None else links
    approved_changes = sum(
        co.amount_cents
        for co in project.change_orders.prefetch_related("decisions")
        if change_order_status(co) == "approved"
    )
    budget = project.budget_cents + approved_changes
    committed = sum(
        link.committed_cents for link in links if link.kind in (LinkKind.CONTRACT, LinkKind.VENDOR)
    )
    invoice_ids = [link.invoice_id for link in links if link.kind == LinkKind.INVOICE]
    spent = (
        InvoicePayment.objects.filter(invoice_id__in=invoice_ids).aggregate(s=Sum("amount_cents"))[
            "s"
        ]
        or 0
    )
    return {
        "original": project.budget_cents,
        "changes": approved_changes,
        "budget": budget,
        "committed": committed,
        "spent": spent,
        "left": budget - spent,
        "spent_percent": min(100, max(0, spent * 100 // budget)) if budget > 0 else 0,
    }


def owner_summaries() -> list[dict]:
    """What owners see: the projects the board shows them, four facts each."""
    out = []
    for project in CapitalProject.objects.filter(owner_visible=True).exclude(
        state=ProjectState.CANCELLED
    ):
        f = figures(project)
        out.append(
            {
                "title": project.title,
                "status": project.get_state_display(),
                "state": project.state,
                "budget": f["budget"],
                "spent": f["spent"],
            }
        )
    return out


def spend_by_project() -> list[dict]:
    """For the books branch: each project's fund and the invoices it paid."""
    out = []
    for project in CapitalProject.objects.exclude(state=ProjectState.CANCELLED):
        links = active_links(project)
        out.append(
            {
                "project_id": project.pk,
                "fund": project.funding,
                "invoice_ids": [link.invoice_id for link in links if link.invoice_id],
                "spent": figures(project, links)["spent"],
            }
        )
    return out


# --- Changes --------------------------------------------------------------------------------


def _event(project, action, actor, *, from_state="", to_state="", note=""):
    CapitalProjectEvent.objects.create(
        project=project,
        action=action,
        from_state=from_state,
        to_state=to_state,
        note=note[:2000],
        by=actor,
    )


def create(
    *,
    title: str,
    budget_cents: int,
    funding: str,
    description: str = "",
    reserve_component=None,
    special_assessment_ref: str = "",
    actor,
    request=None,
) -> CapitalProject:
    require(actor, "capital_project.manage")
    if not title.strip():
        raise ProjectError("Give the project a title.")
    if budget_cents is None or budget_cents <= 0:
        raise ProjectError("The budget must be more than zero.")
    if funding not in Funding.values:
        raise ProjectError("Choose how it is funded.")
    if funding == Funding.SPECIAL and not special_assessment_ref.strip():
        raise ProjectError("Name the special assessment (its resolution) that funds it.")
    with transaction.atomic():
        project = CapitalProject.objects.create(
            title=title.strip()[:200],
            description=description.strip(),
            budget_cents=budget_cents,
            funding=funding,
            reserve_component=reserve_component,
            special_assessment_ref=special_assessment_ref.strip()[:200],
            created_by=actor,
        )
        _event(project, "created", actor, to_state=project.state)
    record_event(
        "capital.created",
        actor=actor,
        request=request,
        target=project,
        metadata={"project_id": project.pk, "budget_cents": budget_cents, "funding": funding},
    )
    return project


EDITABLE = ("title", "description", "reserve_component", "special_assessment_ref")


def update(project: CapitalProject, *, actor, request=None, **fields) -> CapitalProject:
    """Details, the owner summary toggle, progress, and (while planned) the budget."""
    require(actor, "capital_project.manage")
    if project.state == ProjectState.CANCELLED:
        raise ProjectError("A cancelled project is kept as it is.")
    changed = []
    for name in EDITABLE:
        if name in fields:
            value = fields[name]
            setattr(project, name, value.strip() if isinstance(value, str) else value)
            changed.append(name)
    if "owner_visible" in fields:
        project.owner_visible = bool(fields["owner_visible"])
        changed.append("owner_visible")
    if "percent_complete" in fields and fields["percent_complete"] is not None:
        pct = int(fields["percent_complete"])
        if not 0 <= pct <= 100:
            raise ProjectError("Percent complete is between 0 and 100.")
        project.percent_complete = pct
        changed.append("percent_complete")
    if fields.get("budget_cents") is not None and fields["budget_cents"] != project.budget_cents:
        if project.state != ProjectState.PLANNED:
            raise ProjectError("Once approved, the budget changes only by a change order.")
        if fields["budget_cents"] <= 0:
            raise ProjectError("The budget must be more than zero.")
        project.budget_cents = fields["budget_cents"]
        changed.append("budget_cents")
    if not project.title.strip():
        raise ProjectError("Give the project a title.")
    with transaction.atomic():
        project.save(update_fields=changed or None)
        _event(project, "updated", actor, note=", ".join(changed))
    record_event(
        "capital.updated",
        actor=actor,
        request=request,
        target=project,
        metadata={"project_id": project.pk},
    )
    return project


def ask_approval(project: CapitalProject, *, resolution_ref: str, actor, request=None):
    """The manager or a board member sends a planned project to the board."""
    require(actor, "capital_project.manage")
    if project.state != ProjectState.PLANNED:
        raise ProjectError("Only a planned project goes for approval.")
    if not resolution_ref.strip():
        raise ProjectError("Give the board resolution that approves it.")
    required = approvals_needed(project.budget_cents)
    CapitalProject.objects.filter(pk=project.pk).update(
        required_approvals=max(required, project.required_approvals or 0),
        resolution_ref=resolution_ref.strip()[:200],
    )
    project.refresh_from_db()
    _event(project, "approval_asked", actor, note=project.resolution_ref)
    record_event(
        "capital.approval_asked",
        actor=actor,
        request=request,
        target=project,
        metadata={"project_id": project.pk, "required": project.required_approvals},
    )
    return project


def current_approvals(project: CapitalProject):
    return project.approvals.filter(
        budget_cents=project.budget_cents, resolution_ref=project.resolution_ref
    )


def approve(project: CapitalProject, *, actor, request=None) -> CapitalProject:
    """One board member's approval; the project is approved once it has the
    number it needs (two different members at or above the threshold)."""
    require(actor, "capital_project.approve")
    with transaction.atomic():
        project = CapitalProject.objects.select_for_update().get(pk=project.pk)
        if project.state != ProjectState.PLANNED:
            raise ProjectError("Only a planned project can be approved.")
        if not project.required_approvals or not project.resolution_ref:
            raise ProjectError("Send it for approval with the board resolution first.")
        required = max(project.required_approvals, approvals_needed(project.budget_cents))
        if current_approvals(project).filter(approver=actor).exists():
            raise ProjectError("You have already approved this project.")
        try:
            with transaction.atomic():
                CapitalProjectApproval.objects.create(
                    project=project,
                    approver=actor,
                    budget_cents=project.budget_cents,
                    resolution_ref=project.resolution_ref,
                )
        except IntegrityError as exc:
            raise ProjectError("You have already approved this project.") from exc
        count = current_approvals(project).count()
        if count >= required:
            CapitalProject.objects.filter(pk=project.pk).update(
                state=ProjectState.APPROVED, state_changed_at=timezone.now()
            )
            _event(
                project,
                "approved",
                actor,
                from_state=ProjectState.PLANNED,
                to_state=ProjectState.APPROVED,
            )
        else:
            _event(project, "approval_given", actor, note=f"{count} of {required}")
    record_event(
        "capital.approved",
        actor=actor,
        request=request,
        target=project,
        metadata={"project_id": project.pk, "approvals": count, "required": required},
    )
    project.refresh_from_db()
    return project


TRANSITIONS = {
    # action: (sources, target, permission, note required)
    "start": ({ProjectState.APPROVED}, ProjectState.IN_PROGRESS, "capital_project.manage", False),
    "complete": (
        {ProjectState.IN_PROGRESS},
        ProjectState.COMPLETED,
        "capital_project.manage",
        False,
    ),
    "cancel": (
        {ProjectState.PLANNED, ProjectState.APPROVED, ProjectState.IN_PROGRESS},
        ProjectState.CANCELLED,
        "capital_project.approve",
        True,
    ),
}


def change_state(project: CapitalProject, action: str, *, actor, note: str = "", request=None):
    if action not in TRANSITIONS:
        raise ProjectError("Unknown step.")
    sources, target, permission, note_required = TRANSITIONS[action]
    require(actor, permission)
    if note_required and not note.strip():
        raise ProjectError("Please add a note explaining this step.")
    with transaction.atomic():
        project = CapitalProject.objects.select_for_update().get(pk=project.pk)
        if project.state not in sources:
            raise ProjectError(f"Not possible while {project.get_state_display().lower()}.")
        before = project.state
        updates = {"state": target, "state_changed_at": timezone.now()}
        if target == ProjectState.COMPLETED:
            updates["percent_complete"] = 100
        CapitalProject.objects.filter(pk=project.pk).update(**updates)
        _event(project, action, actor, from_state=before, to_state=target, note=note)
    record_event(
        "capital.state_changed",
        actor=actor,
        request=request,
        target=project,
        metadata={"project_id": project.pk, "from": before, "to": target},
    )
    project.refresh_from_db()
    return project


def available_steps(project: CapitalProject, user) -> list[str]:
    return [
        action
        for action, (sources, _t, permission, _n) in TRANSITIONS.items()
        if project.state in sources and can(user, permission)
    ]


# --- Change orders -------------------------------------------------------------------------


def propose_change(
    project: CapitalProject, *, amount_cents: int, reason: str, actor, request=None
) -> ChangeOrder:
    require(actor, "capital_project.manage")
    if project.state not in (ProjectState.APPROVED, ProjectState.IN_PROGRESS):
        raise ProjectError(
            "Change orders are for approved projects; change a planned project's budget."
        )
    if not amount_cents:
        raise ProjectError("A change order changes the budget by some amount.")
    if not reason.strip():
        raise ProjectError("Give the reason for the change.")
    if project.budget_cents + figures(project)["changes"] + amount_cents < 0:
        raise ProjectError("The budget cannot go below zero.")
    required = approvals_needed(amount_cents)
    co = ChangeOrder.objects.create(
        project=project,
        amount_cents=amount_cents,
        reason=reason.strip(),
        required_approvals=required,
        proposed_by=actor,
    )
    _event(project, "change_order", actor, note=f"#{co.pk}")
    record_event(
        "capital.change_order_proposed",
        actor=actor,
        request=request,
        target=project,
        metadata={"project_id": project.pk, "amount_cents": amount_cents, "required": required},
    )
    return co


def decide_change(co: ChangeOrder, *, decision: str, actor, note: str = "", request=None) -> str:
    require(actor, "capital_project.approve")
    if decision not in (ChangeOrderDecision.APPROVE, ChangeOrderDecision.DECLINE):
        raise ProjectError("Approve or decline.")
    if co.proposed_by_id == actor.pk:
        raise ProjectError("Another board member decides a change order you proposed.")
    if decision == ChangeOrderDecision.DECLINE and not note.strip():
        raise ProjectError("Give the reason for declining.")
    with transaction.atomic():
        # Lock the project (append-only rows cannot be locked FOR UPDATE by
        # the runtime role, which has no UPDATE on them).
        CapitalProject.objects.select_for_update().filter(pk=co.project_id).exists()
        if change_order_status(co) != "pending":
            raise ProjectError("This change order is already decided.")
        if co.project.state not in (ProjectState.APPROVED, ProjectState.IN_PROGRESS):
            raise ProjectError("The project is no longer open for changes.")
        try:
            with transaction.atomic():
                ChangeOrderDecision.objects.create(
                    change_order=co, decision=decision, by=actor, note=note.strip()
                )
        except IntegrityError as exc:
            raise ProjectError("You have already decided this change order.") from exc
    record_event(
        "capital.change_order_decided",
        actor=actor,
        request=request,
        target=co.project,
        metadata={"change_order_id": co.pk, "decision": decision},
    )
    return change_order_status(co)


# --- Links -----------------------------------------------------------------------------------

LINK_MODELS = {
    LinkKind.CONTRACT: Contract,
    LinkKind.VENDOR: Vendor,
    LinkKind.INVOICE: Invoice,
    LinkKind.WORK_ORDER: WorkOrder,
}


def link(
    project: CapitalProject,
    *,
    kind: str,
    object_id: int,
    committed_cents: int = 0,
    actor,
    request=None,
) -> CapitalProjectLink:
    require(actor, "capital_project.manage")
    if kind not in LINK_MODELS:
        raise ProjectError("Choose what to link.")
    if project.state == ProjectState.CANCELLED:
        raise ProjectError("A cancelled project is kept as it is.")
    obj = LINK_MODELS[kind].objects.filter(pk=object_id).first()
    if obj is None:
        raise ProjectError("That record was not found.")
    if any(getattr(lk, f"{kind}_id") == obj.pk for lk in active_links(project) if lk.kind == kind):
        raise ProjectError("It is already linked.")
    if (
        kind == LinkKind.INVOICE
        and CapitalProjectLink.objects.filter(
            kind=kind, invoice=obj, removes__isnull=True, removed_by__isnull=True
        )
        .exclude(project=project)
        .exists()
    ):
        raise ProjectError("That invoice is already linked to another project.")
    if committed_cents and kind not in (LinkKind.CONTRACT, LinkKind.VENDOR):
        raise ProjectError("Only contracts and vendors carry a committed amount.")
    if committed_cents < 0:
        raise ProjectError("The committed amount cannot be negative.")
    row = CapitalProjectLink.objects.create(
        project=project,
        kind=kind,
        committed_cents=committed_cents,
        linked_by=actor,
        **{kind: obj},
    )
    record_event(
        "capital.linked",
        actor=actor,
        request=request,
        target=project,
        metadata={"project_id": project.pk, "kind": kind},
    )
    return row


def unlink(row: CapitalProjectLink, *, actor, request=None) -> CapitalProjectLink:
    require(actor, "capital_project.manage")
    if row.removes_id or CapitalProjectLink.objects.filter(removes=row).exists():
        raise ProjectError("That link is already removed.")
    try:
        removal = CapitalProjectLink.objects.create(
            project=row.project, kind=row.kind, removes=row, linked_by=actor
        )
    except IntegrityError as exc:
        raise ProjectError("That link is already removed.") from exc
    record_event(
        "capital.unlinked",
        actor=actor,
        request=request,
        target=row.project,
        metadata={"project_id": row.project_id, "kind": row.kind},
    )
    return removal


# --- My items --------------------------------------------------------------------------------


def my_items(user) -> list:
    from django.urls import reverse

    from core.my_items import Item

    if not can(user, "capital_project.approve"):
        return []
    out = []
    for project in CapitalProject.objects.filter(
        state=ProjectState.PLANNED, required_approvals__isnull=False
    ):
        if current_approvals(project).filter(approver=user).exists():
            continue
        out.append(
            Item(
                "Second approvals",
                f"Approve capital project: {project.title}",
                f"Budget ${project.budget_cents // 100:,} · {project.resolution_ref}",
                reverse("capital_project_detail", args=[project.pk]),
                "warning",
                nav="capital_projects",
                icon="wrench",
            )
        )
    for co in (
        ChangeOrder.objects.filter(
            project__state__in=[ProjectState.APPROVED, ProjectState.IN_PROGRESS]
        )
        .exclude(proposed_by=user)
        .select_related("project")
        .prefetch_related("decisions")
    ):
        if change_order_status(co) != "pending" or any(
            d.by_id == user.pk for d in co.decisions.all()
        ):
            continue
        sign = "+" if co.amount_cents > 0 else "-"
        out.append(
            Item(
                "Second approvals",
                f"Change order for {co.project.title}",
                f"{sign}${abs(co.amount_cents) // 100:,}: {co.reason[:80]}",
                reverse("capital_project_detail", args=[co.project_id]) + "#changes",
                "warning",
                nav="capital_projects",
                icon="wrench",
            )
        )
    return out
