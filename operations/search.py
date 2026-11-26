"""Search providers: owner requests, work orders, vendors and invoices
(core.search).

* Owner requests: ``OwnerRequest.objects.for_user`` like the requests list
  (tenants: only their own; contacts and former owners: their own era).
  Thread messages follow comms.threads (no internal notes for the owner side).
* Work orders, vendors, invoices: the finance pages' own check
  (finance.view or vendor.manage); work order threads need workorder.manage,
  as comms.threads.subject_for does.
"""

from django.urls import reverse

from core import policy
from core.search import LIMIT, Result, matching, register, snippet, thread_results


def _request_title(r) -> str:
    return f"Request #{r.pk}: {r.subject}"


@register("requests", "Owner requests", url_name="requests")
def requests(user, q):
    from operations.models import OwnerRequest

    if not (policy.can(user, "request.manage") or policy.can(user, "request.view_own")):
        return []
    subjects = OwnerRequest.objects.for_user(user)
    rows = (
        subjects.filter(matching(q, "subject", "description"))
        .select_related("lot")
        .order_by("-created_at")[:LIMIT]
    )
    out = [
        Result(
            title=_request_title(r),
            url=reverse("request_detail", args=[r.pk]),
            snippet=snippet(q, r.description),
            date=r.created_at,
            detail=f"{r.lot.label} · {r.get_state_display()}",
        )
        for r in rows
    ]
    return out + thread_results("request", subjects, user, q, title=_request_title)


def _finance(user) -> bool:
    return policy.can(user, "finance.view") or policy.can(user, "vendor.manage")


def _work_order_title(wo) -> str:
    return str(wo)


@register("work_orders", "Work orders", url_name="work_orders")
def work_orders(user, q):
    from operations.models import WorkOrder

    if not _finance(user):
        return []
    subjects = WorkOrder.objects.all()
    rows = (
        subjects.filter(matching(q, "title", "description", "vendor__name"))
        .select_related("vendor")
        .order_by("-created_at")[:LIMIT]
    )
    out = [
        Result(
            title=_work_order_title(wo),
            url=reverse("work_order_detail", args=[wo.pk]),
            snippet=snippet(q, wo.description, wo.vendor.name if wo.vendor_id else ""),
            date=wo.created_at,
            detail=wo.state.replace("_", " ").capitalize(),
        )
        for wo in rows
    ]
    if policy.can(user, "workorder.manage"):
        out += thread_results("work_order", subjects, user, q, title=_work_order_title)
    return out


@register("vendors", "Vendors", url_name="vendors")
def vendors(user, q):
    from operations.models import Vendor

    if not _finance(user):
        return []
    rows = Vendor.objects.filter(matching(q, "name", "contact_name", "services", "notes")).order_by(
        "-created_at"
    )[:LIMIT]
    return [
        Result(
            title=v.name,
            url=reverse("vendor_detail", args=[v.pk]),
            snippet=snippet(q, v.services, v.contact_name, v.notes),
            date=v.created_at,
            detail="" if v.active else "Inactive",
        )
        for v in rows
    ]


@register("invoices", "Invoices", url_name="invoices")
def invoices(user, q):
    from operations.models import Invoice

    if not _finance(user):
        return []
    rows = (
        Invoice.objects.filter(matching(q, "number", "description", "vendor__name"))
        .select_related("vendor")
        .order_by("-invoice_date", "-pk")[:LIMIT]
    )
    return [
        Result(
            title=str(inv),
            url=reverse("invoice_detail", args=[inv.pk]),
            snippet=snippet(q, inv.description),
            date=inv.invoice_date,
            detail=inv.state.replace("_", " ").capitalize(),
        )
        for inv in rows
    ]
