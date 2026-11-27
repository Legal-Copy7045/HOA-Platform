"""Pages of the ledger's finance options: Interest on overdue balances (D80),
Cash-flow forecast (D81) and the legal steps of a collection case (D82).
Each page's URL is in its feature (core.features): switched off, 404."""

from __future__ import annotations

import datetime

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from audit.services import record_event
from core import policy
from core.lists import csv_response
from core.money import MoneyField, parse_cents
from documents import attachments
from documents.attachments import pdf_response
from ledger import cashflow, interest, legal
from ledger.models import CollectionCase, EntryKind, LedgerEntry
from ledger.models_options import Court, LegalKind, LienFiling


def _finance(request):
    policy.require(request.user, "finance.view")


# --- Interest on overdue balances (D80) ----------------------------------------------------


@login_required
def interest_page(request):
    _finance(request)
    user = request.user
    today = timezone.localdate()
    if request.method == "POST":
        action = request.POST.get("do")
        try:
            if action == "post":
                count = interest.post_interest(
                    year=today.year, month=today.month, actor=user, request=request
                )
                messages.success(
                    request, f"Interest posted on {count} account{'s' if count != 1 else ''}."
                )
            elif action == "reverse":
                entry = get_object_or_404(
                    LedgerEntry.objects.unscoped("interest reversal"),
                    pk=request.POST.get("entry"),
                    kind=EntryKind.INTEREST,
                )
                interest.reverse_interest(
                    entry, reason=request.POST.get("reason", ""), actor=user, request=request
                )
                messages.success(request, "Interest charge reversed.")
            else:
                raise Http404
        except interest.InterestError as exc:
            messages.error(request, str(exc))
        return redirect("interest")
    current = interest.current_policy()
    on = interest.posting_date(today.year, today.month, current)
    return render(
        request,
        "ledger/interest.html",
        {
            "policy": current,
            "monthly": interest.monthly_rate_text(current.rate_bp) if current.rate_bp else "",
            "posting_on": on,
            "due": on <= today,
            "candidates": interest.candidates(year=today.year, month=today.month, policy=current),
            "postings": interest.recent_postings(),
            "can_post": policy.can(user, "interest.post"),
            "can_reverse": policy.can(user, "interest.reverse"),
            "notice": interest.owner_notice(request),
        },
    )


# --- Cash-flow forecast (D81) ---------------------------------------------------------------


def _scenario(request) -> cashflow.Scenario:
    def cents(name):
        raw = (request.GET.get(name) or "").strip()
        if not raw:
            return 0
        try:
            value = parse_cents(raw)
        except ValueError:
            return 0
        return max(0, min(value, 100_000_000))

    try:
        rate = int(request.GET.get("rate") or 0)
    except ValueError:
        rate = 0
    return cashflow.Scenario(
        rate_change_pct=max(-100, min(rate, 0)),
        extra_monthly_cents=cents("extra"),
        one_off_cents=cents("oneoff"),
    )


@login_required
def cashflow_page(request):
    _finance(request)
    scenario = _scenario(request)
    forecast = cashflow.build(scenario=scenario)
    return render(
        request,
        "ledger/cashflow.html",
        {
            "forecast": forecast,
            "scenario": scenario,
            "query": request.GET.urlencode(),
            "rates": [0, -5, -10, -20],
            "extra": request.GET.get("extra", ""),
            "oneoff": request.GET.get("oneoff", ""),
        },
    )


@login_required
def cashflow_export(request):
    _finance(request)
    scenario = _scenario(request)
    forecast = cashflow.build(scenario=scenario)
    fmt = "pdf" if request.GET.get("format") == "pdf" else "csv"
    record_event(
        "cashflow.exported",
        request=request,
        actor=request.user,
        metadata={"format": fmt, "scenario": scenario.describe()},
    )
    if fmt == "pdf":
        title = "Cash-flow forecast" + (" (what-if)" if scenario.active else "")
        return pdf_response(cashflow.pdf(forecast, title=title), "cash-flow-forecast.pdf")
    return csv_response(
        request,
        "cash-flow-forecast",
        cashflow.HEADER,
        cashflow.table_rows(forecast),
        audit_kind="cashflow",
    )


# --- Legal steps on a collection case (D82) ------------------------------------------------


def _date_field(**kw):
    return forms.DateField(widget=forms.DateInput(attrs={"type": "date"}), **kw)


class LegalForm(forms.Form):
    kind = forms.ChoiceField(
        choices=[(k, v) for k, v in LegalKind.choices if k != LegalKind.COST_POSTED],
        label="What happened",
    )
    occurred_on = _date_field(label="Date", initial=datetime.date.today)
    firm = forms.CharField(max_length=200, required=False, label="Law firm")
    attorney = forms.CharField(max_length=120, required=False, label="Attorney")
    reference = forms.CharField(
        max_length=80, required=False, label="Counsel's reference or docket number"
    )
    fee_arrangement = forms.CharField(max_length=200, required=False, label="Fee arrangement")
    court = forms.ChoiceField(choices=[("", "-"), *list(Court.choices)], required=False)
    lien_filing = forms.ChoiceField(
        choices=[("", "-"), *list(LienFiling.choices)], required=False, label="What was filed"
    )
    amount_cents = MoneyField(required=False, label="Amount ($)")
    chargeable = forms.ChoiceField(
        choices=[("", "-"), ("yes", "Yes"), ("no", "No")],
        required=False,
        label="Chargeable to the owner under the Declaration (costs)",
    )
    due_on = _date_field(required=False, label="Hearing date")
    detail = forms.CharField(widget=forms.Textarea(attrs={"rows": 2}), required=False)


def _case(request, pk):
    policy.require(request.user, "legal.view")
    return get_object_or_404(CollectionCase.objects.for_user(request.user), pk=pk)


@login_required
@require_POST
def collection_legal(request, pk):
    case = _case(request, pk)
    action = request.POST.get("do", "record")
    back = reverse("collection_detail", args=[case.pk]) + "#legal"
    try:
        if action == "record":
            form = LegalForm(request.POST)
            if not form.is_valid():
                messages.error(request, "Check the legal step's details.")
                return redirect(back)
            data = dict(form.cleaned_data)
            chargeable = {"yes": True, "no": False}.get(data.pop("chargeable") or "")
            amount = data.pop("amount_cents")
            event = legal.record(
                case,
                actor=request.user,
                request=request,
                chargeable=chargeable,
                amount_cents=amount if amount not in ("", None) else None,
                **data,
            )
            uploads = request.FILES.getlist("file")
            if uploads:
                attachments.attach_many(
                    request,
                    case,
                    uploads,
                    actor=request.user,
                    label=f"{legal.FILE_LABEL} #{event.pk}",
                )
            messages.success(request, "Recorded on the case.")
        elif action == "post_cost":
            from ledger.models_options import LegalEvent

            cost = get_object_or_404(
                LegalEvent.objects.for_user(request.user), pk=request.POST.get("cost"), case=case
            )
            legal.post_cost(cost, actor=request.user, request=request)
            messages.success(request, "The cost is now a charge on the owner's account.")
        else:
            raise Http404
    except legal.LegalError as exc:
        messages.error(request, str(exc))
    return redirect(back)


@login_required
def collection_legal_file(request, pk, file_id):
    return attachments.serve(request, _case(request, pk), file_id)


def legal_panel_context(request, case) -> dict:
    """For the collection case page (templatetag legal_panel)."""
    rows = legal.events(case)
    files = list(attachments.attachments_for(case).filter(label__startswith=legal.FILE_LABEL))
    from core import conflicts

    own = conflicts.own_matter(request.user, case)
    return {
        "case": case,
        "rows": rows,
        "info": legal.summary(case, rows),
        "files": files,
        "form": LegalForm(),
        "can_record": policy.can(request.user, "legal.record") and not own,
        "can_post": policy.can(request.user, "legal.post_costs") and not own,
        "own_matter": own,
        "release_due": legal.release_due(case),
        "with_counsel": legal.is_with_counsel(case),
    }
