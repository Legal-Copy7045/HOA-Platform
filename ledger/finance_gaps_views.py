"""Pages for special assessments (D75), payment plans (D76) and budget
preparation (D78). Each service checks its own permission; these views
fetch records through scoped managers (or finance-reader pages) only."""

from __future__ import annotations

import datetime

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from community.rules import rule, rule_with_citation
from core import conflicts, policy
from core.money import MoneyField, format_cents
from core.scoping import system_access
from core.workflow import WorkflowError
from documents.attachments import pdf_response
from ledger import budget_prep, payment_plans, special_assessments
from ledger.models import (
    Budget,
    CollectionCase,
    PaymentPlan,
    SpecialAssessment,
    SpecialAssessmentFund,
)


def _date(**kw):
    return forms.DateField(widget=forms.DateInput(attrs={"type": "date"}), **kw)


def _add_months(day: datetime.date, months: int) -> datetime.date:
    month = day.month - 1 + months
    return datetime.date(day.year + month // 12, month % 12 + 1, min(day.day, 28))


# --- Special assessments (D75) --------------------------------------------------------------


class LevyForm(forms.Form):
    title = forms.CharField(label="Title", max_length=100, help_text='e.g. "Pond dredging 2027"')
    purpose = forms.CharField(
        label="Purpose", max_length=4000, widget=forms.Textarea(attrs={"rows": 3})
    )
    total_cents = MoneyField(label="Total for the community ($)")
    fund = forms.ChoiceField(label="Designated to", choices=SpecialAssessmentFund.choices)
    installments = forms.IntegerField(
        label="Number of installments", min_value=1, max_value=60, initial=1
    )
    first_due = _date(label="First installment due on")
    every_months = forms.TypedChoiceField(
        label="Then every",
        coerce=int,
        choices=[(1, "month"), (3, "3 months"), (6, "6 months"), (12, "year")],
        initial=1,
    )
    resolution = forms.TypedChoiceField(
        label="Board resolution", coerce=int, required=False, empty_value=None
    )
    resolution_ref = forms.CharField(
        label="Or the resolution's reference (adopted elsewhere)", max_length=120, required=False
    )
    vote = forms.TypedChoiceField(
        label="Owners' vote (when required)", coerce=int, required=False, empty_value=None
    )
    vote_ref = forms.CharField(
        label="Or the vote's reference (held elsewhere)", max_length=120, required=False
    )

    def __init__(self, *args, homes=(), equal=True, **kwargs):
        super().__init__(*args, **kwargs)
        from governance.models import Resolution, Vote

        self.fields["resolution"].choices = [("", "None recorded in the portal")] + [
            (r.pk, f"{r.number}: {r.title}"[:120]) for r in Resolution.objects.all()[:50]
        ]
        self.fields["vote"].choices = [("", "None")] + [
            (v.pk, v.title[:120]) for v in Vote.objects.filter(status="closed", kind="formal")[:50]
        ]
        self.homes = list(homes)
        if not equal:
            for lot in self.homes:
                self.fields[f"share_{lot.pk}"] = MoneyField(
                    label=f"{lot.label} ($)", required=False
                )

    def clean(self):
        data = super().clean()
        if not data.get("resolution") and not (data.get("resolution_ref") or "").strip():
            self.add_error("resolution_ref", "Choose the board resolution, or type its reference.")
        return data

    def due_dates(self) -> list[datetime.date]:
        d = self.cleaned_data
        return [
            _add_months(d["first_due"], i * d["every_months"]) for i in range(d["installments"])
        ]

    def custom(self) -> dict[int, int] | None:
        if not any(name.startswith("share_") for name in self.fields):
            return None
        return {lot.pk: self.cleaned_data.get(f"share_{lot.pk}") or 0 for lot in self.homes}


def _sa_view(request):
    policy.require(request.user, "special_assessment.view")


@login_required
def special_assessments_page(request):
    _sa_view(request)
    rows = [{"sa": sa, **special_assessments.summary(sa)} for sa in SpecialAssessment.objects.all()]
    return render(
        request,
        "ledger/special_assessments.html",
        {"rows": rows, "can_levy": policy.can(request.user, "special_assessment.levy")},
    )


@login_required
def special_assessment_new(request):
    policy.require(request.user, "special_assessment.levy")
    equal = bool(rule(special_assessments.EQUAL_RULE))
    homes = special_assessments.homes()
    form = LevyForm(request.POST or None, homes=homes, equal=equal)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        from governance.models import Resolution, Vote

        try:
            sa = special_assessments.levy(
                title=d["title"],
                purpose=d["purpose"],
                total_cents=d["total_cents"],
                due_dates=form.due_dates(),
                fund=d["fund"],
                resolution=Resolution.objects.filter(pk=d["resolution"]).first()
                if d["resolution"]
                else None,
                resolution_ref=d["resolution_ref"],
                vote=Vote.objects.filter(pk=d["vote"]).first() if d["vote"] else None,
                vote_ref=d["vote_ref"],
                custom=form.custom(),
                actor=request.user,
                request=request,
            )
        except special_assessments.SpecialAssessmentError as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(request, "Levied. Send the owner notice next.")
            return redirect("special_assessment_detail", pk=sa.pk)
    needed_cents, vote_cite = rule_with_citation(special_assessments.VOTE_CENTS_RULE)
    return render(
        request,
        "ledger/special_assessment_new.html",
        {
            "form": form,
            "equal": equal,
            "homes": len(homes),
            "equal_rule": rule_with_citation(special_assessments.EQUAL_RULE),
            "vote_cents": needed_cents,
            "vote_cite": vote_cite,
            "vote_percent": rule(special_assessments.VOTE_PERCENT_RULE),
            "late_fees": rule(special_assessments.LATE_FEE_RULE),
        },
        status=400 if request.method == "POST" else 200,
    )


@login_required
def special_assessment_detail(request, pk):
    _sa_view(request)
    sa = get_object_or_404(SpecialAssessment, pk=pk)
    info = special_assessments.summary(sa)
    posted = [r["installment"].number for r in info["installments"] if r["status"] == "posted"]
    return render(
        request,
        "ledger/special_assessment_detail.html",
        {
            "sa": sa,
            **info,
            "shares": special_assessments.shares(sa),
            "can_levy": policy.can(request.user, "special_assessment.levy"),
            "can_post": policy.can(request.user, "ledger.manage"),
            "can_late_fee": sa.late_fees and policy.can(request.user, "late_fee.post"),
            "first_cancellable": (max(posted) + 1) if posted else 1,
        },
    )


@login_required
@require_POST
def special_assessment_action(request, pk, action):
    _sa_view(request)
    sa = get_object_or_404(SpecialAssessment, pk=pk)
    try:
        if action == "notice":
            result = special_assessments.send_notice(sa, actor=request.user, request=request)
            messages.success(request, f"Notice emailed to {result['emailed']} address(es).")
            if result["paper"]:
                messages.warning(
                    request,
                    f"{result['paper']} owner(s) need a paper copy: prepare it on Paper mail.",
                )
        elif action == "cancel":
            special_assessments.cancel_future(
                sa,
                from_number=int(request.POST.get("from_number") or 0),
                reason=request.POST.get("reason", ""),
                actor=request.user,
                request=request,
            )
            messages.success(request, "Future installments cancelled.")
        elif action == "post":
            count = special_assessments.post_due(assessment=sa, actor=request.user, request=request)
            messages.success(request, f"{count} charge(s) posted.")
        elif action == "latefees":
            count = special_assessments.post_late_fees(actor=request.user, request=request)
            messages.success(request, f"{count} late fee(s) posted.")
        else:
            raise Http404
    except (special_assessments.SpecialAssessmentError, ValueError) as exc:
        messages.error(request, str(exc))
    return redirect("special_assessment_detail", pk=sa.pk)


# --- Payment plans (D76) -----------------------------------------------------------------


class PlanForm(forms.Form):
    count = forms.IntegerField(label="Number of installments", min_value=1, max_value=60, initial=6)
    first_due = _date(label="First installment due on")
    schedule = forms.CharField(
        label="Or your own schedule: one installment per line, date and amount",
        required=False,
        widget=forms.Textarea(attrs={"rows": 4, "placeholder": "2027-01-15 150.00"}),
        help_text="Leave empty for equal monthly installments.",
    )
    note = forms.CharField(
        label="Note for the board",
        required=False,
        max_length=2000,
        widget=forms.Textarea(attrs={"rows": 2}),
    )

    def installments(self, total: int):
        from core.money import parse_cents

        text = (self.cleaned_data.get("schedule") or "").strip()
        if not text:
            return payment_plans.even_installments(
                total, self.cleaned_data["count"], self.cleaned_data["first_due"]
            )
        out = []
        for line in text.splitlines():
            if not line.strip():
                continue
            day, _, amount = line.strip().partition(" ")
            try:
                out.append((datetime.date.fromisoformat(day.strip()), parse_cents(amount)))
            except ValueError as exc:
                raise payment_plans.PaymentPlanError(
                    f'Write each line as a date and an amount, e.g. "2027-01-15 150.00" '
                    f"({line.strip()[:40]})."
                ) from exc
        return out


def _finance(request):
    if not (policy.can(request.user, "finance.view") or policy.can(request.user, "ledger.manage")):
        policy.require(request.user, "finance.view")


@login_required
def payment_plans_page(request):
    _finance(request)
    plans = list(PaymentPlan.objects.for_user(request.user).select_related("lot", "case"))
    rows = [payment_plans.overview(p) for p in plans]
    return render(request, "ledger/payment_plans.html", {"rows": rows})


@login_required
def payment_plan_new(request, pk):
    policy.require(request.user, "payment_plan.prepare")
    case = get_object_or_404(CollectionCase.objects.for_user(request.user), pk=pk)
    charges, total, _account = payment_plans.covered_charges(case)
    form = PlanForm(
        request.POST or None,
        initial={"first_due": _add_months(timezone.localdate(), 1).replace(day=1)},
    )
    if request.method == "POST" and form.is_valid():
        try:
            plan = payment_plans.prepare(
                case,
                installments=form.installments(total),
                note=form.cleaned_data["note"],
                actor=request.user,
                request=request,
            )
        except payment_plans.PaymentPlanError as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(request, "Prepared. Another board member is asked to approve it.")
            return redirect("payment_plan_detail", pk=plan.pk)
    return render(
        request,
        "ledger/payment_plan_new.html",
        {
            "case": case,
            "charges": charges,
            "total": total,
            "form": form,
            **conflicts.page_context(request.user, case),
            "suspend": rule_with_citation(payment_plans.SUSPEND_RULE),
            "grace": rule_with_citation(payment_plans.GRACE_RULE),
            "default": rule_with_citation(payment_plans.DEFAULT_RULE),
        },
        status=400 if request.method == "POST" else 200,
    )


@login_required
def payment_plan_detail(request, pk):
    _finance(request)
    plan = get_object_or_404(
        PaymentPlan.objects.for_user(request.user).select_related("lot", "case"), pk=pk
    )
    info = payment_plans.overview(plan)
    own = conflicts.page_context(request.user, plan)
    user = request.user
    return render(
        request,
        "ledger/payment_plan_detail.html",
        {
            **info,
            **own,
            "can_decide": info["status"] == "proposed"
            and policy.can(user, "payment_plan.approve")
            and plan.prepared_by_id != user.pk
            and not own["own_matter"],
            "preparer": plan.prepared_by_id == user.pk,
            "can_cancel": info["status"] in ("proposed", "active")
            and policy.can(user, "payment_plan.approve")
            and not own["own_matter"],
            "state_labels": payment_plans.STATE_LABELS,
        },
    )


@login_required
@require_POST
def payment_plan_action(request, pk, action):
    _finance(request)
    plan = get_object_or_404(PaymentPlan.objects.for_user(request.user), pk=pk)
    note = request.POST.get("note", "")
    try:
        if action == "approve":
            payment_plans.approve(plan, note=note, actor=request.user, request=request)
            messages.success(request, "Approved. The collection case shows the plan.")
        elif action == "decline":
            payment_plans.decline(plan, note=note, actor=request.user, request=request)
            messages.success(request, "Declined.")
        elif action == "cancel":
            payment_plans.cancel(plan, note=note, actor=request.user, request=request)
            messages.success(request, "Cancelled. The collection case is back where it was.")
        else:
            raise Http404
    except (payment_plans.PaymentPlanError, WorkflowError) as exc:
        messages.error(request, str(exc))
    return redirect("payment_plan_detail", pk=plan.pk)


def _owner_plan(request, pk) -> PaymentPlan:
    """The plan, for the board or for someone who sees that account today."""
    plan = get_object_or_404(PaymentPlan.objects.for_user(request.user), pk=pk)
    if policy.can(request.user, "finance.view"):
        return plan
    from ledger.accounts import current_account_id

    roles = policy.roles_for(request.user)
    if (
        not policy.can(request.user, "ledger.view_own")
        or plan.lot_id not in roles.account_lot_ids
        or current_account_id(plan.lot_id) != plan.account_id
    ):
        raise Http404
    return plan


@login_required
def payment_plan_letter(request, pk):
    from audit.services import record_event
    from ledger import letters

    plan = _owner_plan(request, pk)
    with system_access("payment plan letter"):
        data = letters.payment_plan_pdf(plan)
    record_event(
        "payment_plan.letter",
        actor=request.user,
        request=request,
        target=plan,
        metadata={"plan_id": plan.pk},
    )
    return pdf_response(data, f"payment-plan-{plan.pk}.pdf")


@login_required
@require_POST
def payment_plan_acknowledge(request, pk):
    plan = _owner_plan(request, pk)
    try:
        payment_plans.acknowledge(plan, actor=request.user, request=request)
        messages.success(request, "Thank you: your agreement to the plan is recorded.")
    except payment_plans.PaymentPlanError as exc:
        messages.error(request, str(exc))
    return redirect("/my-account/#payment-plan")


def _signed_percent(bp) -> str:
    from community.rules import bp_display

    if bp is None:
        return ""
    return ("+" if bp >= 0 else "-") + bp_display(abs(bp)) + "%"


# --- Budget preparation (D78) ------------------------------------------------------------


@login_required
@require_POST
def budget_prepare(request):
    policy.require(request.user, "budget.manage")
    source = get_object_or_404(Budget, pk=request.POST.get("source") or 0)
    from community.rules import percent_to_bp

    try:
        increase = percent_to_bp(request.POST.get("increase") or "0")
    except ValueError:
        increase = -1
    try:
        budget = budget_prep.prepare_next(
            source,
            basis=request.POST.get("basis", "budget"),
            increase_bp=int(increase),
            include_reserve=bool(request.POST.get("reserve")),
            actor=request.user,
            request=request,
        )
    except (budget_prep.BudgetPrepError, ValueError) as exc:
        messages.error(request, str(exc))
        return redirect("budgets")
    messages.success(request, f"FY{budget.fiscal_year} draft budget prepared. Check each line.")
    return redirect("budget_compare", pk=budget.pk)


@login_required
def budget_compare(request, pk):
    _finance(request)
    budget = get_object_or_404(Budget, pk=pk)
    data = budget_prep.comparison(budget)
    ph = data["per_home"]
    return render(
        request,
        "ledger/budget_compare.html",
        {
            "budget": budget,
            **data,
            "change_percent": _signed_percent(ph["change_bp"]),
            "format_cents": format_cents,
        },
    )
