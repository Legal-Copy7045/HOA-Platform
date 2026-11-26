"""Disbursement pages (Finance -> Disbursements, optional feature
"disbursements", D52). Board, manager and auditor see them; the service
functions in ledger/disbursements.py check each step's own permission
(the manager prepares and records, only the board approves and voids)."""

from __future__ import annotations

import datetime

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from community.models import Account, home_sort_key
from community.rules import rule_with_citation
from core import policy
from core.money import MoneyField, format_cents
from core.scoping import system_access
from documents.attachments import pdf_response
from ledger import disbursements as disb
from ledger.models import (
    CheckPrintSettings,
    DisbursementBatch,
    DisbursementCheck,
    DisbursementKind,
    DisbursementLine,
)


def _date(**kw):
    return forms.DateField(widget=forms.DateInput(attrs={"type": "date"}), **kw)


class SurplusForm(forms.Form):
    total_cents = MoneyField(label="Total to distribute ($)")
    year = forms.IntegerField(
        label="Surplus from the year",
        min_value=2000,
        max_value=2100,
        initial=lambda: timezone.localdate().year,
    )
    resolution = forms.TypedChoiceField(
        label="Board resolution", coerce=int, required=False, empty_value=None
    )
    resolution_ref = forms.CharField(
        label="Or the resolution's reference (adopted elsewhere)",
        max_length=120,
        required=False,
        help_text='e.g. "Resolution 2026-14, board meeting of December 3"',
    )
    by_check = forms.TypedMultipleChoiceField(
        label="Pay these homes by check instead of a statement credit",
        coerce=int,
        required=False,
        widget=forms.CheckboxSelectMultiple,
    )
    note = forms.CharField(
        label="Note for the board",
        required=False,
        max_length=2000,
        widget=forms.Textarea(attrs={"rows": 2}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from governance.models import Resolution

        self.fields["resolution"].choices = [("", "None recorded in the portal")] + [
            (r.pk, f"{r.number}: {r.title}"[:120]) for r in Resolution.objects.all()[:50]
        ]
        self.fields["by_check"].choices = [(a.lot_id, a.lot.label) for a in disb.surplus_accounts()]

    def clean(self):
        data = super().clean()
        if not data.get("resolution") and not (data.get("resolution_ref") or "").strip():
            self.add_error("resolution_ref", "Choose the board resolution, or type its reference.")
        return data


class RefundForm(forms.Form):
    amount_cents = MoneyField(label="Amount to refund ($)")
    payee_name = forms.CharField(label="Pay to the order of", max_length=120)
    forwarding_address = forms.CharField(
        label="Mailing address (forwarding address for a former owner)",
        max_length=300,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="Where the check goes. A former owner no longer lives at the home they sold.",
    )
    note = forms.CharField(
        label="Note for the board",
        required=False,
        max_length=2000,
        widget=forms.Textarea(attrs={"rows": 2}),
    )


class DecisionForm(forms.Form):
    note = forms.CharField(label="Note", required=False, max_length=1000)


class IssueForm(forms.Form):
    first_number = forms.CharField(
        label="First check number", max_length=12, help_text="Numbered in the order listed."
    )
    issued_on = _date(label="Date on the checks", initial=timezone.localdate)


class ClearForm(forms.Form):
    on = _date(label="Cleared on", initial=timezone.localdate)


class VoidForm(forms.Form):
    reason = forms.CharField(label="Reason", max_length=150)


def _settings_form_class():
    fields = {}
    for name, label in disb.PRINT_FIELDS:
        fields[f"{name}_x"] = forms.IntegerField(
            label=f"{label}: across", min_value=0, max_value=850
        )
        fields[f"{name}_y"] = forms.IntegerField(
            label=f"{label}: down", min_value=0, max_value=1100
        )
    fields["stub1_y"] = forms.IntegerField(
        label="First stub: down (0 = none)", min_value=0, max_value=1100
    )
    fields["stub2_y"] = forms.IntegerField(
        label="Second stub: down (0 = none)", min_value=0, max_value=1100
    )
    fields["nudge_x"] = forms.IntegerField(
        label="Move everything right (negative: left)", min_value=-100, max_value=100
    )
    fields["nudge_y"] = forms.IntegerField(
        label="Move everything down (negative: up)", min_value=-100, max_value=100
    )
    return type("CheckPrintForm", (forms.Form,), fields)


CheckPrintForm = _settings_form_class()


def _view(request):
    policy.require(request.user, "disbursement.view")


@login_required
def disbursements(request):
    _view(request)
    with system_access("disbursement list page"):
        credits = disb.dormant_credits()
        stale = disb.stale_checks()
    return render(
        request,
        "ledger/disbursements.html",
        {
            "batches": disb.batches(),
            "surplus": disb.reserve_surplus(),
            "credits": credits,
            "stale": stale,
            "unclaimed_note": disb.UNCLAIMED_NOTE,
            "can_propose": policy.can(request.user, "disbursement.propose"),
            "can_issue": policy.can(request.user, "disbursement.issue"),
        },
    )


@login_required
def disbursement_new(request):
    policy.require(request.user, "disbursement.propose")
    kind = request.GET.get("kind") or request.POST.get("kind") or "surplus"
    if kind == DisbursementKind.REFUND:
        return _new_refund(request)
    if kind != DisbursementKind.SURPLUS:
        raise Http404
    form = SurplusForm(request.POST or None)
    preview = None
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        try:
            if request.POST.get("action") == "propose":
                resolution = None
                if d["resolution"]:
                    from governance.models import Resolution

                    resolution = Resolution.objects.filter(pk=d["resolution"]).first()
                batch = disb.propose_surplus(
                    total_cents=d["total_cents"],
                    year=d["year"],
                    resolution=resolution,
                    resolution_ref=d["resolution_ref"],
                    by_check=d["by_check"],
                    note=d["note"],
                    actor=request.user,
                    request=request,
                )
                messages.success(request, "Proposed. Other board members are asked to approve it.")
                return redirect("disbursement_detail", pk=batch.pk)
            preview = disb.surplus_split(d["total_cents"], by_check=d["by_check"])
        except disb.DisbursementError as exc:
            form.add_error(None, str(exc))
    return render(
        request,
        "ledger/disbursement_new.html",
        {
            "kind": "surplus",
            "form": form,
            "preview": preview,
            "preview_total": sum(p["amount_cents"] for p in preview or []),
            "surplus": disb.reserve_surplus(),
            "equal_rule": rule_with_citation(disb.EQUAL_RULE),
        },
    )


def _new_refund(request):
    try:
        account_id = int(request.GET.get("account") or request.POST.get("account") or "")
    except ValueError as exc:
        raise Http404 from exc
    account = get_object_or_404(
        Account.objects.for_user(request.user).select_related("lot"), pk=account_id
    )
    defaults = disb.refund_defaults(account)
    initial = {
        "amount_cents": format_cents(defaults["available_cents"]).lstrip("$"),
        "payee_name": defaults["payee_name"],
        "forwarding_address": defaults["mail_to"],
    }
    form = RefundForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        try:
            batch = disb.propose_refund(
                account=account,
                amount_cents=d["amount_cents"],
                payee_name=d["payee_name"],
                forwarding_address=d["forwarding_address"],
                note=d["note"],
                actor=request.user,
                request=request,
            )
        except disb.DisbursementError as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(request, "Proposed. Another board member is asked to approve it.")
            return redirect("disbursement_detail", pk=batch.pk)
    return render(
        request,
        "ledger/disbursement_new.html",
        {"kind": "refund", "form": form, "account": account, "defaults": defaults},
    )


def _detail_context(request, batch, **forms_):
    state = disb.batch_state(batch)
    lines = list(
        DisbursementLine.objects.for_user(request.user)
        .filter(batch=batch)
        .select_related("lot", "account")
    )
    lines.sort(key=lambda line: home_sort_key(line.lot))
    rows = []
    checks = []
    with system_access("disbursement detail"):
        for line in lines:
            line_checks = list(DisbursementCheck.objects.filter(line=line).order_by("id"))
            for c in line_checks:
                c.status = disb.check_status(c)
            checks += [c for c in line_checks if c.status == "issued"]
            rows.append(
                {
                    "line": line,
                    "status": disb.line_status(line, state),
                    "label": disb.LINE_LABELS[disb.line_status(line, state)],
                    "checks": line_checks,
                }
            )
        suggestions = disb.clearing_suggestions(checks)
        recipients = disb.recipient_person_ids(batch)
    for c in checks:
        c.suggestion = suggestions.get(c.pk)
    user = request.user
    person_id = policy.roles_for(user).person_id
    can_approve = (
        state.status == "awaiting"
        and policy.can(user, "disbursement.approve")
        and batch.proposed_by_id != user.pk
        and not any(d.approver_id == user.pk for d in state.approvals)
        and not (batch.kind == DisbursementKind.REFUND and person_id in recipients)
    )
    to_issue = [r for r in rows if r["status"] == "to_issue"]
    return {
        "batch": batch,
        "state": state,
        "rows": rows,
        "to_issue": to_issue,
        "is_recipient": person_id in recipients,
        "can_approve": can_approve,
        "can_decline": state.status == "awaiting" and policy.can(user, "disbursement.approve"),
        "can_issue": policy.can(user, "disbursement.issue"),
        "can_void": policy.can(user, "disbursement.void"),
        "decision_form": forms_.get("decision_form") or DecisionForm(),
        "issue_form": forms_.get("issue_form") or IssueForm(),
        "clear_form": ClearForm(),
        "void_form": VoidForm(),
        "error": forms_.get("error", ""),
        "today": timezone.localdate(),
        "unclaimed_note": disb.UNCLAIMED_NOTE,
        "stale_days": disb.STALE_DAYS,
    }


@login_required
def disbursement_detail(request, pk):
    _view(request)
    batch = get_object_or_404(
        DisbursementBatch.objects.select_related("proposed_by", "resolution"), pk=pk
    )
    return render(request, "ledger/disbursement_detail.html", _detail_context(request, batch))


def _check_of(request, batch) -> DisbursementCheck:
    try:
        check_id = int(request.POST.get("check") or "")
    except ValueError as exc:
        raise Http404 from exc
    return get_object_or_404(
        DisbursementCheck.objects.for_user(request.user).select_related("line"),
        pk=check_id,
        line__batch=batch,
    )


@login_required
@require_POST
def disbursement_action(request, pk, action):
    _view(request)
    batch = get_object_or_404(DisbursementBatch, pk=pk)
    forms_: dict = {}
    try:
        if action in ("approve", "decline"):
            form = forms_["decision_form"] = DecisionForm(request.POST)
            if form.is_valid():
                if action == "approve":
                    state = disb.approve(
                        batch, note=form.cleaned_data["note"], actor=request.user, request=request
                    )
                    messages.success(
                        request,
                        "Approved: the statement credits are posted."
                        if state.status == "approved"
                        else f"Approval recorded ({state.have} of {state.needed}).",
                    )
                else:
                    disb.decline(
                        batch, note=form.cleaned_data["note"], actor=request.user, request=request
                    )
                    messages.success(request, "Declined. Nothing was posted.")
                return redirect("disbursement_detail", pk=batch.pk)
        elif action == "issue":
            form = forms_["issue_form"] = IssueForm(request.POST)
            if form.is_valid():
                line_ids = request.POST.getlist("line") or None
                made = disb.issue_checks(
                    batch,
                    first_number=form.cleaned_data["first_number"],
                    issued_on=form.cleaned_data["issued_on"],
                    line_ids=line_ids,
                    actor=request.user,
                    request=request,
                )
                messages.success(
                    request, f"{len(made)} check{'' if len(made) == 1 else 's'} recorded as issued."
                )
                return redirect("disbursement_detail", pk=batch.pk)
        elif action == "clear":
            check = _check_of(request, batch)
            form = ClearForm(request.POST)
            if form.is_valid():
                bank_line = None
                if request.POST.get("bank_line"):
                    from ledger.models import BankTransaction

                    bank_line = get_object_or_404(
                        BankTransaction, pk=int(request.POST["bank_line"])
                    )
                disb.mark_cleared(
                    check,
                    on=form.cleaned_data["on"],
                    bank_transaction=bank_line,
                    actor=request.user,
                    request=request,
                )
                messages.success(request, f"Check #{check.number} marked cleared.")
                return redirect("disbursement_detail", pk=batch.pk)
            forms_["error"] = "Enter the date the check cleared."
        elif action == "void":
            check = _check_of(request, batch)
            form = VoidForm(request.POST)
            if form.is_valid():
                disb.void_check(
                    check, reason=form.cleaned_data["reason"], actor=request.user, request=request
                )
                messages.success(request, f"Check #{check.number} voided.")
                return redirect("disbursement_detail", pk=batch.pk)
            forms_["error"] = "Give the reason for voiding the check."
        else:
            raise Http404
    except disb.DisbursementError as exc:
        forms_["error"] = str(exc)
    return render(
        request,
        "ledger/disbursement_detail.html",
        _detail_context(request, batch, **forms_),
        status=200,
    )


@login_required
def disbursement_register(request, pk):
    _view(request)
    batch = get_object_or_404(DisbursementBatch, pk=pk)
    if request.GET.get("format") == "pdf":
        return pdf_response(
            disb.register_pdf(batch, actor=request.user, request=request),
            f"check-register-{batch.pk}.pdf",
        )
    response = HttpResponse(
        disb.register_csv(batch, actor=request.user, request=request), content_type="text/csv"
    )
    response["Content-Disposition"] = f'attachment; filename="check-register-{batch.pk}.csv"'
    response["Cache-Control"] = "private, no-store"
    return response


@login_required
def disbursement_checks_pdf(request, pk):
    policy.require(request.user, "disbursement.issue")
    batch = get_object_or_404(DisbursementBatch, pk=pk)
    try:
        on = datetime.date.fromisoformat(request.GET.get("on") or "")
    except ValueError:
        on = timezone.localdate()
    try:
        data = disb.checks_pdf(
            batch,
            on=on,
            line_ids=request.GET.getlist("line") or None,
            actor=request.user,
            request=request,
        )
    except disb.DisbursementError as exc:
        messages.error(request, str(exc))
        return redirect("disbursement_detail", pk=batch.pk)
    return pdf_response(data, f"checks-{batch.pk}.pdf")


@login_required
def check_print_settings(request):
    policy.require(request.user, "disbursement.issue")
    settings = CheckPrintSettings.get()
    initial = {
        f.name: getattr(settings, f.name)
        for f in CheckPrintSettings._meta.fields
        if f.name.endswith(("_x", "_y"))
    }
    form = CheckPrintForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        try:
            disb.save_print_settings(form.cleaned_data, actor=request.user, request=request)
        except disb.DisbursementError as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(request, "Saved. Print the alignment test to check it.")
            return redirect("check_print_settings")
    return render(request, "ledger/check_print_settings.html", {"form": form, "settings": settings})


@login_required
def check_alignment_pdf(request):
    policy.require(request.user, "disbursement.issue")
    return pdf_response(disb.alignment_pdf(actor=request.user), "check-alignment-test.pdf")
