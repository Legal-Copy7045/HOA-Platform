"""Board reports. Each report takes parameters (date range, as-of date or fiscal
year) and returns column headings and rows of plain values. Money columns are
integer cents and formatted for display/CSV by the view."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass

from django.db.models import Count, Sum
from django.utils import timezone

from core.money import format_cents
from core.scoping import system_access

RANGE, AS_OF, FISCAL_YEAR = "range", "as_of", "fiscal_year"


@dataclass(frozen=True)
class Report:
    key: str
    title: str
    description: str
    group: str
    params: str
    build: Callable


REPORTS: dict[str, Report] = {}


def report(key, title, description, group, params):
    def decorator(fn):
        REPORTS[key] = Report(key, title, description, group, params, fn)
        return fn

    return decorator


def money(cents):
    return format_cents(cents or 0)


def _events(workflow, start, end):
    from core.models import WorkflowEvent

    return WorkflowEvent.objects.filter(
        workflow=workflow, created_at__date__gte=start, created_at__date__lte=end
    )


# --- Association ------------------------------------------------------------------------


@report(
    "arc",
    "ARC report",
    "Every architectural request action in the date range.",
    "Association",
    RANGE,
)
def arc_report(start, end):
    from arc.models import ArcRequest

    with system_access("report"):
        arc_requests = {r.pk: r for r in ArcRequest.objects.select_related("lot")}
    rows = []
    for e in _events("arc", start, end).select_related("actor"):
        r = arc_requests.get(int(e.object_id))
        if r:
            rows.append(
                [
                    timezone.localdate(e.created_at),
                    r.pk,
                    r.lot.number,
                    r.title,
                    e.action,
                    r.get_state_display(),
                    r.review_due_on or "",
                ]
            )
    return ["Date", "Request", "Lot", "Project", "Action", "Current status", "Decision due"], rows


@report(
    "collections",
    "Collections report",
    "Collection cases and their latest steps.",
    "Association",
    RANGE,
)
def collections_report(start, end):
    from ledger.models import CollectionCase
    from ledger.services import balance

    with system_access("report"):
        cases = {c.pk: c for c in CollectionCase.objects.select_related("lot")}
    rows = []
    for e in _events("collection", start, end):
        c = cases.get(int(e.object_id))
        if c:
            rows.append(
                [
                    timezone.localdate(e.created_at),
                    c.pk,
                    c.lot.number,
                    e.action,
                    c.state,
                    money(balance(c.lot_id)),
                ]
            )
    return ["Date", "Case", "Lot", "Step", "Current status", "Balance now"], rows


@report(
    "open_invoices", "Open invoices", "Invoices entered but not yet paid.", "Association", AS_OF
)
def open_invoices(as_of):
    from operations.models import Invoice
    from operations.services import paid_cents

    rows = []
    for i in Invoice.objects.filter(
        state__in=["draft", "pending_approval", "approved"], invoice_date__lte=as_of
    ).select_related("vendor"):
        paid = paid_cents(i)
        rows.append(
            [
                i.vendor.name,
                i.number,
                i.invoice_date,
                i.due_date or "",
                i.state,
                money(i.amount_cents),
                money(paid),
                money(i.amount_cents - paid),
            ]
        )
    return ["Vendor", "Invoice", "Date", "Due", "Status", "Amount", "Paid", "Remaining"], rows


@report(
    "owner_transfers",
    "Owner transfer report",
    "Ownerships that started in the date range and who they replaced.",
    "Association",
    RANGE,
)
def owner_transfers(start, end):
    from community.models import Ownership

    rows = []
    with system_access("report"):
        for o in Ownership.objects.filter(
            start_date__gte=start, start_date__lte=end
        ).select_related("lot", "person"):
            previous = Ownership.objects.filter(lot=o.lot, end_date=o.start_date).select_related(
                "person"
            )
            rows.append(
                [
                    o.start_date,
                    o.lot.number,
                    o.person.display_name,
                    "; ".join(p.person.display_name for p in previous),
                    o.get_ownership_type_display(),
                ]
            )
    return ["Date", "Lot", "New owner", "Previous owner(s)", "Type"], rows


@report(
    "paid_by_vendor",
    "Paid invoices by service provider",
    "Vendor contact details and amounts paid in the date range.",
    "Association",
    RANGE,
)
def paid_by_vendor(start, end):
    from operations.models import InvoicePayment

    totals = (
        InvoicePayment.objects.filter(paid_on__gte=start, paid_on__lte=end)
        .values(
            "invoice__vendor__name",
            "invoice__vendor__contact_name",
            "invoice__vendor__email",
            "invoice__vendor__phone",
        )
        .annotate(total=Sum("amount_cents"), payments=Count("id"))
        .order_by("invoice__vendor__name")
    )
    rows = [
        [
            t["invoice__vendor__name"],
            t["invoice__vendor__contact_name"],
            t["invoice__vendor__email"],
            t["invoice__vendor__phone"],
            t["payments"],
            money(t["total"]),
        ]
        for t in totals
    ]
    return ["Vendor", "Contact", "Email", "Phone", "Payments", "Total paid"], rows


def _violation_rows(start, end, photos: bool):
    from compliance.models import Violation
    from documents.attachments import attachments_for

    with system_access("report"):
        violations = {v.pk: v for v in Violation.objects.select_related("lot")}
    rows = []
    for e in _events("violation", start, end):
        v = violations.get(int(e.object_id))
        if not v:
            continue
        row = [
            timezone.localdate(e.created_at),
            v.pk,
            v.lot.number,
            v.get_category_display(),
            e.action,
            v.get_state_display(),
            v.cure_by or "",
        ]
        if photos:
            row.append("; ".join(a.original_filename for a in attachments_for(v)))
        rows.append(row)
    return rows


@report(
    "violations",
    "Violations report",
    "Every violation step in the date range.",
    "Association",
    RANGE,
)
def violations_report(start, end):
    return (
        ["Date", "Violation", "Lot", "Category", "Step", "Current status", "Cure by"],
        _violation_rows(start, end, photos=False),
    )


@report(
    "violations_photos",
    "Violations report with photos",
    "As above, listing the photos on file for each violation.",
    "Association",
    RANGE,
)
def violations_photos(start, end):
    return (
        ["Date", "Violation", "Lot", "Category", "Step", "Current status", "Cure by", "Photos"],
        _violation_rows(start, end, photos=True),
    )


@report(
    "work_orders",
    "Work order summary",
    "Every work order step in the date range.",
    "Association",
    RANGE,
)
def work_orders_report(start, end):
    from operations.models import WorkOrder

    orders = {w.pk: w for w in WorkOrder.objects.select_related("vendor", "common_area")}
    rows = []
    for e in _events("work_order", start, end):
        w = orders.get(int(e.object_id))
        if w:
            rows.append(
                [
                    timezone.localdate(e.created_at),
                    f"WO-{w.pk}",
                    w.title,
                    w.common_area.name if w.common_area else "",
                    w.vendor.name if w.vendor else "",
                    e.action,
                    w.state,
                ]
            )
    return ["Date", "Work order", "Work", "Area", "Vendor", "Step", "Current status"], rows


# --- Financials ------------------------------------------------------------------------------


@report(
    "ar_aging",
    "AR aging",
    "Owners with a balance, by how long charges have been open.",
    "Financials",
    AS_OF,
)
def ar_aging(as_of):
    from community.models import Lot, Ownership
    from ledger.services import BUCKET_LABELS, BUCKETS, aging, balance

    rows = []
    with system_access("report"):
        for lot in Lot.objects.all():
            bal = balance(lot.pk, as_of)
            if bal == 0:
                continue
            a = aging(lot.pk, as_of)
            owners = "; ".join(
                o.person.display_name
                for o in Ownership.objects.filter(lot=lot).active(as_of).select_related("person")
            )
            rows.append(
                [
                    lot.number,
                    owners,
                    *(money(a[b]) for b in BUCKETS),
                    money(-a["credit"]),
                    money(bal),
                ]
            )
    labels = [BUCKET_LABELS[b] for b in BUCKETS]
    return ["Lot", "Owner(s)", *labels, "Credit", "Balance"], rows


def _budget(fiscal_year):
    from ledger.models import Budget

    return Budget.objects.filter(fiscal_year=fiscal_year).first()


@report("budget", "Budget", "The budget for the fiscal year.", "Financials", FISCAL_YEAR)
def budget_report(fiscal_year):
    budget = _budget(fiscal_year)
    if budget is None:
        return ["Line", "Kind", "Annual"], []
    rows = [
        [line.name, line.get_kind_display(), money(line.annual_cents)]
        for line in budget.lines.all()
    ]
    return ["Line", "Kind", "Annual budget"], rows


@report(
    "budget_spread",
    "Budget spread",
    "Monthly budget, actual and variance for the fiscal year.",
    "Financials",
    FISCAL_YEAR,
)
def budget_spread(fiscal_year):
    from ledger.services import budget_vs_actual

    budget = _budget(fiscal_year)
    if budget is None:
        return ["Line"], []
    report_data = budget_vs_actual(budget)
    months = [f"{m:02d}/{y}" for y, m in report_data["months"]]
    head = ["Line", "Measure", *months, "Total"]
    rows = []
    for r in report_data["rows"]:
        variance = [a - p for a, p in zip(r["actual"], r["planned"], strict=True)]
        for label, values in (
            ("Budget", r["planned"]),
            ("Actual", r["actual"]),
            ("Variance", variance),
        ):
            rows.append([r["line"].name, label, *[money(v) for v in values], money(sum(values))])
    return head, rows


def grouped():
    groups = defaultdict(list)
    for r in REPORTS.values():
        groups[r.group].append(r)
    return dict(groups)


def default_range():
    today = timezone.localdate()
    return today.replace(month=1, day=1), today
