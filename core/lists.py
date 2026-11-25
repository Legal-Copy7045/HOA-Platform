"""List tools shared by list pages: status and date filters, paging, and
CSV export (formula-safe, audited)."""

from __future__ import annotations

import csv
import datetime

from django.core.paginator import Paginator
from django.http import HttpResponse
from django.utils import timezone

PAGE_SIZE = 25
EXPORT_MAX_ROWS = 5000

FORMULA_START = set("=+-@\t\r\n") | {chr(c) for c in (0xFF1D, 0xFF0B, 0xFF0D, 0xFF20)}


def safe_cell(value):
    """Stop spreadsheet formula injection (security review C5).

    Text whose first non-blank character could start a formula (=, +, -, @,
    their full-width forms, or a tab/CR/LF) gets a leading quote. Plain
    numbers such as -12.50 or $1,000 are left alone.
    """
    text = "" if value is None else str(value)
    stripped = text.lstrip(" \t\r\n" + chr(0xA0))
    if not stripped or (stripped[0] not in FORMULA_START and text[:1] not in FORMULA_START):
        return text
    if stripped.lstrip("-").replace(".", "").replace("$", "").replace(",", "").isdigit():
        return text
    return "'" + text


def paginate(request, queryset, per_page: int = PAGE_SIZE):
    """A page of ``queryset`` from ``?page=``; bad numbers give the last or
    first page rather than an error."""
    return Paginator(queryset, per_page).get_page(request.GET.get("page"))


def _date(value: str):
    try:
        return datetime.date.fromisoformat(value) if value else None
    except ValueError:
        return None


def filter_list(request, queryset, *, date_field: str, open_states=None, default_show="all"):
    """Apply ?show=open|closed|all and ?from=YYYY-MM-DD&to=YYYY-MM-DD.

    Returns (queryset, context) where context feeds partials/list_tools.html.
    ``date_field`` may be a DateField or a DateTimeField (compared by date).
    """
    show = request.GET.get("show", default_show)
    if open_states is not None:
        if show == "open":
            queryset = queryset.filter(state__in=list(open_states))
        elif show == "closed":
            queryset = queryset.exclude(state__in=list(open_states))
        else:
            show = "all"
    start, end = _date(request.GET.get("from", "")), _date(request.GET.get("to", ""))
    field = queryset.model._meta.get_field(date_field)
    lookup = f"{date_field}__date" if field.get_internal_type() == "DateTimeField" else date_field
    if start:
        queryset = queryset.filter(**{f"{lookup}__gte": start})
    if end:
        queryset = queryset.filter(**{f"{lookup}__lte": end})
    return queryset, {
        "show": show,
        "has_states": open_states is not None,
        "date_from": start.isoformat() if start else "",
        "date_to": end.isoformat() if end else "",
    }


def wants_csv(request) -> bool:
    return request.GET.get("export") == "csv"


def csv_response(request, name: str, header: list[str], rows, *, audit_kind: str):
    """A formula-safe CSV download, recorded as an export in the audit log."""
    from audit.services import record_event

    rows = list(rows)[:EXPORT_MAX_ROWS]
    record_event(
        "list.exported",
        request=request,
        actor=request.user,
        metadata={"list": audit_kind, "rows": len(rows)},
    )
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    stamp = timezone.localdate().isoformat()
    response["Content-Disposition"] = f'attachment; filename="{name}-{stamp}.csv"'
    writer = csv.writer(response)
    writer.writerow([safe_cell(h) for h in header])
    for row in rows:
        writer.writerow([safe_cell(c) for c in row])
    return response
