import csv
import datetime

from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse
from django.shortcuts import render
from django.utils import timezone

from audit.services import record_event
from core import policy
from reports.registry import AS_OF, FISCAL_YEAR, RANGE, REPORTS, grouped


def _date(value, default):
    try:
        return datetime.date.fromisoformat(value) if value else default
    except ValueError:
        return default


def _safe_cell(value):
    """Stop spreadsheet formula injection: text starting =, +, -, @ gets a quote."""
    text = "" if value is None else str(value)
    if (
        text
        and text[0] in "=+-@\t\r"
        and not text.lstrip("-").replace(".", "").replace("$", "").replace(",", "").isdigit()
    ):
        return "'" + text
    return text


@login_required
def index(request):
    policy.require(request.user, "reports.view")
    return render(request, "reports/index.html", {"groups": grouped()})


@login_required
def run(request, key):
    policy.require(request.user, "reports.view")
    rep = REPORTS.get(key)
    if rep is None:
        raise Http404
    today = timezone.localdate()
    params = {}
    if rep.params == RANGE:
        params = {
            "start": _date(request.GET.get("start"), today.replace(month=1, day=1)),
            "end": _date(request.GET.get("end"), today),
        }
    elif rep.params == AS_OF:
        params = {"as_of": _date(request.GET.get("as_of"), today)}
    elif rep.params == FISCAL_YEAR:
        try:
            params = {"fiscal_year": int(request.GET.get("fiscal_year") or today.year)}
        except ValueError:
            params = {"fiscal_year": today.year}
    head, rows = rep.build(**params)
    if request.GET.get("format") == "csv":
        record_event(
            "report.exported",
            request=request,
            actor=request.user,
            metadata={"report": key, "rows": len(rows)},
        )
        response = HttpResponse(content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="{key}-{today}.csv"'
        response["Cache-Control"] = "private, no-store"
        writer = csv.writer(response)
        writer.writerow(head)
        for row in rows:
            writer.writerow([_safe_cell(c) for c in row])
        return response
    record_event(
        "report.viewed",
        request=request,
        actor=request.user,
        metadata={"report": key, "rows": len(rows)},
    )
    return render(
        request, "reports/run.html", {"rep": rep, "head": head, "rows": rows, "params": params}
    )
