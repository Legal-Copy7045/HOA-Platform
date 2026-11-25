"""My items: one queue of everything waiting on the signed-in person, with a
count per area for the board home. Each item links to the page where the
real workflow step is taken (approve, decide, reply...), not just a comment.

Items come from each area's own scoped queries, so nobody sees an item for
something they could not open.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from django.urls import reverse
from django.utils import timezone

from core import policy


@dataclass
class Item:
    area: str
    title: str
    detail: str
    url: str
    tone: str = "info"  # info, warning, danger
    due: datetime.date | None = None

    @property
    def overdue(self) -> bool:
        return bool(self.due and self.due < timezone.localdate())


# Who has a queue page (staff); owners see items waiting on them on Home.
MY_ITEMS_PERMISSIONS = (
    "request.manage",
    "invoice.approve",
    "arc.intake",
    "arc.decide",
    "task.manage",
)

AREAS = [
    "Requests",
    "Approvals",
    "Architectural review",
    "Violations",
    "Collections",
    "Work orders",
    "Records requests",
    "Tasks",
]


def _due_tone(due, today) -> str:
    if due is None:
        return "info"
    if due < today:
        return "danger"
    return "warning" if due <= today + datetime.timedelta(days=2) else "info"


def items_for(user) -> list[Item]:
    roles = policy.roles_for(user)
    g = roles.groups
    can = policy.can
    today = timezone.localdate()
    out: list[Item] = []

    # Owner requests: assigned to me, or nobody's yet; overdue first.
    if can(user, "request.manage"):
        from django.db.models import Q

        from operations.models import OPEN_REQUEST_STATES, OwnerRequest

        mine = Q(assignee__isnull=True)
        if roles.person_id:
            mine |= Q(assignee_id=roles.person_id)
        for r in (
            OwnerRequest.objects.for_user(user)
            .filter(state__in=OPEN_REQUEST_STATES)
            .filter(mine)
            .exclude(state="waiting")
            .select_related("lot")
            .order_by("respond_by", "created_at")
        ):
            if r.first_response_at is None and r.respond_by:
                lead = "Response overdue since" if r.is_overdue else "Respond by"
                detail = f"{lead} {r.respond_by:%b %-d}"
                tone = "danger" if r.is_overdue else _due_tone(r.respond_by, today)
            else:
                detail, tone = r.get_state_display(), "info"
            who = "unassigned" if r.assignee_id is None else "assigned to you"
            out.append(
                Item(
                    "Requests",
                    f"#{r.pk} {r.subject} (lot {r.lot.number})",
                    f"{r.get_request_type_display()} · {who} · {detail}",
                    reverse("request_detail", args=[r.pk]),
                    tone,
                    r.respond_by if r.first_response_at is None else None,
                )
            )

    # Invoices I can approve (not entered by me, not approved by me yet).
    if can(user, "invoice.approve"):
        from operations.models import Invoice, InvoiceApproval

        done = InvoiceApproval.objects.filter(approver=user).values_list("invoice_id", flat=True)
        for inv in (
            Invoice.objects.filter(state="pending_approval")
            .exclude(entered_by=user)
            .exclude(pk__in=done)
            .select_related("vendor")
        ):
            out.append(
                Item(
                    "Approvals",
                    f"Invoice {inv}",
                    "Needs your approval",
                    reverse("invoice_detail", args=[inv.pk]),
                    "warning",
                    inv.due_date,
                )
            )
    if can(user, "role.sysadmin.approve"):
        from community.models import SysadminNomination

        for n in SysadminNomination.objects.for_user(user):
            if n.is_open and n.nominated_by_id != user.pk:
                out.append(
                    Item(
                        "Approvals",
                        "Sysadmin nomination",
                        "Needs a second board member",
                        reverse("approvals"),
                        "warning",
                    )
                )

    # Architectural review: intake, and requests I must still take a position on.
    if can(user, "arc.view_all"):
        from arc.models import ArcRequest, ReviewerVote
        from arc.services import is_recused

        arc = ArcRequest.objects.for_user(user)
        if can(user, "arc.intake"):
            for r in arc.filter(state="submitted"):
                out.append(
                    Item(
                        "Architectural review",
                        f"ARC #{r.pk} {r.title}",
                        "Check it is complete to start the review clock",
                        reverse("arc_detail", args=[r.pk]),
                        "info",
                    )
                )
        if can(user, "arc.decide"):
            taken = set(
                ReviewerVote.objects.filter(reviewer=user).values_list("request_id", flat=True)
            )
            for r in arc.filter(state="in_review").order_by("review_due_on"):
                if r.pk in taken or is_recused(user, r):
                    continue
                out.append(
                    Item(
                        "Architectural review",
                        f"ARC #{r.pk} {r.title}",
                        "Your position is needed"
                        + (f" · decision due {r.review_due_on:%b %-d}" if r.review_due_on else ""),
                        reverse("arc_detail", args=[r.pk]),
                        _due_tone(r.review_due_on, today),
                        r.review_due_on,
                    )
                )

    # Follow-ups due (today or earlier) and owner responses to review.
    if can(user, "violation.manage"):
        from compliance.models import Violation

        v = Violation.objects.for_user(user)
        for x in v.filter(follow_up_on__lte=today).exclude(
            state__in=["cured", "dismissed", "closed"]
        ):
            out.append(
                Item(
                    "Violations",
                    f"{x} (lot {x.lot.number})",
                    f"Follow up due {x.follow_up_on:%b %-d}",
                    reverse("violation_detail", args=[x.pk]),
                    _due_tone(x.follow_up_on, today),
                    x.follow_up_on,
                )
            )
        for x in v.filter(state="responded"):
            out.append(
                Item(
                    "Violations",
                    f"{x} (lot {x.lot.number})",
                    "The owner responded",
                    reverse("violation_detail", args=[x.pk]),
                    "info",
                )
            )
    if can(user, "collections.manage"):
        from ledger.models import CollectionCase

        for c in (
            CollectionCase.objects.for_user(user)
            .filter(follow_up_on__lte=today)
            .exclude(state="closed")
            .select_related("lot")
        ):
            out.append(
                Item(
                    "Collections",
                    f"{c} (lot {c.lot.number})",
                    f"Follow up due {c.follow_up_on:%b %-d}",
                    reverse("collection_detail", args=[c.pk]),
                    _due_tone(c.follow_up_on, today),
                    c.follow_up_on,
                )
            )
    if can(user, "workorder.manage"):
        from operations.models import WorkOrder

        for w in WorkOrder.objects.filter(follow_up_on__lte=today).exclude(
            state__in=["closed", "cancelled"]
        ):
            out.append(
                Item(
                    "Work orders",
                    str(w),
                    f"Follow up due {w.follow_up_on:%b %-d}",
                    reverse("work_order_detail", args=[w.pk]),
                    _due_tone(w.follow_up_on, today),
                    w.follow_up_on,
                )
            )
    if can(user, "records_request.manage"):
        from governance.models import RecordsRequest

        for r in RecordsRequest.objects.for_user(user).filter(
            state__in=["submitted", "in_progress"]
        ):
            out.append(
                Item(
                    "Records requests",
                    f"Records request #{r.pk}",
                    f"Due {r.due_on:%b %-d}" if r.due_on else "No due date",
                    reverse("records_request_detail", args=[r.pk]),
                    _due_tone(r.due_on, today),
                    r.due_on,
                )
            )
    if can(user, "task.manage") and roles.person_id:
        from operations.models import BoardTask

        for t in BoardTask.objects.filter(status="open", assignee_id=roles.person_id):
            out.append(
                Item(
                    "Tasks",
                    t.title,
                    f"Due {t.due_date:%b %-d}" if t.due_date else "Assigned to you",
                    reverse("tasks"),
                    _due_tone(t.due_date, today),
                    t.due_date,
                )
            )

    # Owner side: their requests waiting on them.
    if g & policy.RESIDENTS:
        from operations.models import OwnerRequest

        for r in OwnerRequest.objects.for_user(user).filter(state="waiting", requested_by=user):
            out.append(
                Item(
                    "Requests",
                    f"#{r.pk} {r.subject}",
                    "The board asked you for more information",
                    reverse("request_detail", args=[r.pk]),
                    "warning",
                )
            )

    order = {a: i for i, a in enumerate(AREAS)}
    out.sort(key=lambda i: (order.get(i.area, 99), not i.overdue, i.due or datetime.date.max))
    return out


def counts(items: list[Item]) -> list[dict]:
    """Per-area counts (with overdue counts) for the board home."""
    by_area: dict[str, dict] = {}
    for it in items:
        row = by_area.setdefault(it.area, {"area": it.area, "count": 0, "overdue": 0})
        row["count"] += 1
        row["overdue"] += it.overdue
    order = {a: i for i, a in enumerate(AREAS)}
    return sorted(by_area.values(), key=lambda r: order.get(r["area"], 99))
