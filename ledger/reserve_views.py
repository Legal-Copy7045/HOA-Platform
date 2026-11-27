"""Reserve plan pages (optional feature "reserves").

Board, manager and auditor: the plan with its year-by-year projection
(Finance -> Reserve plan). Only the board changes or adopts it. Owners: the
adopted plan's summary under Community info; never the draft or board notes.
"""

from __future__ import annotations

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core import validators
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from community.models import Lot
from core import policy
from core.money import MoneyField, format_cents, parse_cents
from ledger import reserve_plan, reserves
from ledger.models import ReserveCategory, ReserveComponent, ReservePlanSettings


class RateField(forms.CharField):
    """A percent typed like "3" or "3.25%", cleaned to basis points (325)."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("max_length", 8)
        super().__init__(*args, **kwargs)
        # The length limit is on the typed text (below), not the cleaned integer.
        self.validators = [
            v for v in self.validators if not isinstance(v, validators.MaxLengthValidator)
        ]

    def to_python(self, value):
        value = (super().to_python(value) or "").strip().rstrip("%").strip()
        if not value:
            return None
        if len(value) > self.max_length:
            raise forms.ValidationError("Enter a shorter percent.")
        try:
            bp = parse_cents(value)
        except ValueError as exc:
            raise forms.ValidationError("Enter a percent like 3 or 2.5") from exc
        if bp < 0:
            raise forms.ValidationError("Enter a percent of 0 or more.")
        return bp


def _percent_text(bp: int | None) -> str:
    if bp is None:
        return ""
    return f"{bp // 100}.{bp % 100:02d}".rstrip("0").rstrip(".")


class ComponentForm(forms.Form):
    name = forms.CharField(label="Name", max_length=120)
    category = forms.ChoiceField(label="Category", choices=ReserveCategory.choices)
    quantity = forms.CharField(
        label='Quantity and unit (e.g. "1,200 sq yd")', max_length=60, required=False
    )
    replacement_cost_cents = MoneyField(label="Replacement cost today (dollars)")
    useful_life_years = forms.IntegerField(
        label="Useful life (years)", min_value=1, max_value=reserve_plan.MAX_LIFE
    )
    remaining_life_years = forms.IntegerField(
        label="Years left (0 = due this year)",
        min_value=0,
        max_value=reserve_plan.MAX_LIFE,
        required=False,
    )
    last_replaced_year = forms.IntegerField(
        label="Or the year it was last replaced", min_value=1900, required=False
    )
    notes = forms.CharField(
        label="Notes (board only)", required=False, widget=forms.Textarea(attrs={"rows": 2})
    )
    active = forms.BooleanField(label="Include in the plan", required=False, initial=True)

    def clean(self):
        data = super().clean()
        if (
            data.get("remaining_life_years") is None
            and data.get("last_replaced_year") is None
            and "remaining_life_years" not in self.errors
        ):
            self.add_error(
                "remaining_life_years", "Enter the years left, or the year it was last replaced."
            )
        return data

    @classmethod
    def for_component(cls, c: ReserveComponent, **kw):
        return cls(
            initial={
                "name": c.name,
                "category": c.category,
                "quantity": c.quantity,
                "replacement_cost_cents": format_cents(c.replacement_cost_cents).lstrip("$"),
                "useful_life_years": c.useful_life_years,
                # Aged to this year, so saving unchanged keeps the due year.
                "remaining_life_years": (
                    c.years_left_now if c.remaining_life_years is not None else None
                ),
                "last_replaced_year": c.last_replaced_year,
                "notes": c.notes,
                "active": c.active,
            },
            **kw,
        )


class SettingsForm(forms.Form):
    starting_balance_cents = MoneyField(
        label="Reserve balance (dollars; leave blank to use the resale certificate figure)",
        required=False,
        allow_negative=True,
    )
    balance_as_of = forms.DateField(
        label="Balance as of (its year is the plan's first year)",
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    annual_contribution_cents = MoneyField(label="Annual contribution to reserves (dollars)")
    inflation_bp = RateField(label="Inflation per year (%)")
    interest_bp = RateField(label="Interest earned per year (%)")
    horizon_years = forms.IntegerField(
        label="Years to plan ahead",
        min_value=reserve_plan.MIN_HORIZON,
        max_value=reserve_plan.MAX_HORIZON,
    )

    @classmethod
    def for_settings(cls, plan: ReservePlanSettings, **kw):
        return cls(
            initial={
                "starting_balance_cents": format_cents(plan.starting_balance_cents).replace(
                    "$", ""
                ),
                "balance_as_of": plan.balance_as_of,
                "annual_contribution_cents": format_cents(plan.annual_contribution_cents).lstrip(
                    "$"
                ),
                "inflation_bp": _percent_text(plan.inflation_bp),
                "interest_bp": _percent_text(plan.interest_bp),
                "horizon_years": plan.horizon_years,
            },
            **kw,
        )


def _lot_count() -> int:
    return Lot.objects.unscoped("reserve plan per-lot figure").count()


def _render_plan(request, *, settings_form=None, component_form=None):
    plan = ReservePlanSettings.get()
    result, balance = reserve_plan.current_projection()
    lots = _lot_count()
    can_manage = policy.can(request.user, "reserves.manage")
    return render(
        request,
        "ledger/reserve_plan.html",
        {
            "plan": plan,
            "result": result,
            # The years that matter (WT-036): a replacement due or below zero.
            "key_years": [r for r in result.rows if r.expense_cents or r.negative],
            "balance": balance,
            "inflation": _percent_text(plan.inflation_bp),
            "interest": _percent_text(plan.interest_bp),
            "lots": lots,
            "needed_per_lot_month": reserves.per_lot_monthly(
                result.needed_contribution_cents, lots
            ),
            "components": ReserveComponent.objects.all(),
            "snapshots": reserve_plan.ReserveStudySnapshot.objects.select_related("adopted_by")[
                :10
            ],
            "can_manage": can_manage,
            "settings_form": settings_form or SettingsForm.for_settings(plan),
            "component_form": component_form or ComponentForm(initial={"active": True}),
            "open_settings": settings_form is not None,
            "open_component": component_form is not None,
        },
    )


@login_required
def reserve_plan_page(request):
    policy.require(request.user, "reserves.view")
    if request.method != "POST":
        return _render_plan(request)
    policy.require(request.user, "reserves.manage")
    if request.POST.get("section") == "settings":
        form = SettingsForm(request.POST)
        if form.is_valid():
            try:
                reserve_plan.save_settings(actor=request.user, request=request, **form.cleaned_data)
            except reserve_plan.ReserveError as exc:
                form.add_error(None, str(exc))
            else:
                messages.success(request, "Reserve plan settings saved.")
                return redirect("reserve_plan")
        return _render_plan(request, settings_form=form)
    form = ComponentForm(request.POST)
    if form.is_valid():
        try:
            reserve_plan.save_component(actor=request.user, request=request, **form.cleaned_data)
        except reserve_plan.ReserveError as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(request, "Component added.")
            return redirect("reserve_plan")
    return _render_plan(request, component_form=form)


@login_required
def reserve_component(request, pk):
    policy.require(request.user, "reserves.view")
    component = get_object_or_404(ReserveComponent, pk=pk)
    can_manage = policy.can(request.user, "reserves.manage")
    if request.method == "POST":
        policy.require(request.user, "reserves.manage")
        form = ComponentForm(request.POST)
        if form.is_valid():
            try:
                reserve_plan.save_component(
                    component, actor=request.user, request=request, **form.cleaned_data
                )
            except reserve_plan.ReserveError as exc:
                form.add_error(None, str(exc))
            else:
                messages.success(request, "Component saved.")
                return redirect("reserve_plan")
    else:
        form = ComponentForm.for_component(component)
    year = reserve_plan.start_year(ReservePlanSettings.get())
    return render(
        request,
        "ledger/reserve_component.html",
        {
            "component": component,
            "form": form,
            "can_manage": can_manage,
            "remaining": component.remaining_life(year),
            "start_year": year,
            "history": component.history.select_related("history_user")[:25],
        },
    )


@login_required
@require_POST
def reserve_adopt(request):
    policy.require(request.user, "reserves.manage")
    try:
        snapshot = reserve_plan.adopt(actor=request.user, request=request)
    except reserve_plan.ReserveError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(
            request, f"Reserve plan recorded as adopted on {snapshot.adopted_on:%B %-d, %Y}."
        )
    return redirect("reserve_plan")


@login_required
def reserve_export(request):
    policy.require(request.user, "reserves.view")
    data = reserve_plan.export_csv(actor=request.user, request=request)
    response = HttpResponse(data, content_type="text/csv")
    response["Content-Disposition"] = (
        f'attachment; filename="reserve-plan-{timezone.localdate():%Y-%m-%d}.csv"'
    )
    return response


@login_required
def reserve_summary(request):
    """The old Reserve plan summary page: now part of Budget and reserves."""
    policy.require(request.user, "reserves.view_summary")
    return redirect("owner_budget")
