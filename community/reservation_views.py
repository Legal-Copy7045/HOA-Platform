"""Common-area reservation pages (optional feature "reservations").

Bookings are fetched through ``Reservation.objects.for_user`` (out of scope
is 404); every change goes through community.reservations.
"""

from __future__ import annotations

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from community import reservations
from community.models import CommonArea, CommonAreaKind, Lot, Reservation, ReservationSettings
from core import policy
from core.money import MoneyField, format_cents

_DT = {"type": "datetime-local"}


class ReservationForm(forms.Form):
    area = forms.ChoiceField(label="Area")
    lot = forms.ChoiceField(label="For my home")
    starts_at = forms.DateTimeField(label="Starts", widget=forms.DateTimeInput(attrs=_DT))
    ends_at = forms.DateTimeField(label="Ends", widget=forms.DateTimeInput(attrs=_DT))
    purpose = forms.CharField(
        label="What it is for (optional; only the board and manager see this)",
        max_length=200,
        required=False,
    )

    def __init__(self, *args, areas, lots, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["area"].choices = [(a.pk, a.name) for a, _rules in areas]
        self.fields["lot"].choices = [(lot.pk, lot.label) for lot in lots]


class AreaRulesForm(forms.Form):
    bookable = forms.BooleanField(label="Owners can book this area", required=False)
    needs_approval = forms.BooleanField(
        label="Each booking needs approval by the board or manager", required=False
    )
    max_hours = forms.IntegerField(label="Longest booking (hours)", min_value=1, required=False)
    advance_days = forms.IntegerField(
        label="How many days ahead owners may book", min_value=0, required=False
    )
    monthly_limit_per_lot = forms.IntegerField(
        label="Bookings per home per month", min_value=1, required=False
    )
    fee_cents = MoneyField(label="Fee in dollars (shown only, not charged here)", required=False)
    deposit_cents = MoneyField(
        label="Deposit in dollars (shown only, not charged here)", required=False
    )
    allow_authorized = forms.BooleanField(
        label="Authorized contacts on an account may book", required=False
    )
    allow_tenants = forms.BooleanField(label="Tenants may book", required=False)
    terms = forms.CharField(
        label="Rules shown to owners when booking",
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    citation = forms.CharField(
        label='Where these rules come from (e.g. "Rules and Regulations §4.2")',
        max_length=200,
        required=False,
    )


class AreaForm(AreaRulesForm):
    """One common area and its booking rules, edited together (streamlining):
    the area itself (name, kind, notes, in use) above its rules."""

    name = forms.CharField(label="Name", max_length=120)
    kind = forms.ChoiceField(label="Kind", choices=CommonAreaKind.choices)
    notes = forms.CharField(
        label="Notes (board only)", required=False, widget=forms.Textarea(attrs={"rows": 2})
    )
    active = forms.BooleanField(
        label="In use (uncheck for an area the community no longer has)", required=False
    )
    field_order = ["name", "kind", "notes", "active"]

    AREA_FIELDS = ("name", "kind", "notes", "active")

    def area_data(self) -> dict:
        return {f: self.cleaned_data[f] for f in self.AREA_FIELDS}

    def rules_data(self) -> dict:
        return {k: v for k, v in self.cleaned_data.items() if k not in self.AREA_FIELDS}


def _form_errors(request, form):
    for errors in form.errors.values():
        for error in errors:
            messages.error(request, error)


@login_required
def reservations_page(request):
    policy.require(request.user, "reservation.view")
    user = request.user
    areas = reservations.bookable_areas()
    can_request = policy.can(user, "reservation.request")
    lot_ids: set[int] = set()
    bookable = []
    if can_request:
        for area, rules in areas:
            ids = reservations.bookable_lot_ids(user, rules)
            if ids:
                bookable.append((area, rules))
                lot_ids |= ids
    lots = list(Lot.objects.unscoped("lots the user may book for").filter(pk__in=lot_ids))
    form = None
    if bookable:
        form = ReservationForm(request.POST or None, areas=bookable, lots=lots)
    if request.method == "POST":
        policy.require(user, "reservation.request")
        if form is None or not form.is_valid():
            if form is not None:
                _form_errors(request, form)
            else:
                messages.error(request, "There is nothing you can book right now.")
            return redirect("reservations")
        area = get_object_or_404(CommonArea, pk=form.cleaned_data["area"])
        try:
            res = reservations.request_reservation(
                area=area,
                lot_id=int(form.cleaned_data["lot"]),
                starts_at=form.cleaned_data["starts_at"],
                ends_at=form.cleaned_data["ends_at"],
                purpose=form.cleaned_data["purpose"],
                actor=user,
                request=request,
            )
        except reservations.ReservationError as exc:
            messages.error(request, str(exc))
            return redirect("reservations")
        messages.success(
            request,
            "Booked." if res.state == "confirmed" else "Requested. You will get an email.",
        )
        return redirect("reservation_detail", pk=res.pk)
    mine = []
    if can_request or policy.can(user, "lot.view_own"):
        from core.scoping import own_records

        mine = list(
            own_records(Reservation.objects.for_user(user), user)
            .select_related("area", "lot")
            .order_by("-starts_at")[:20]
        )
    return render(
        request,
        "community/reservations.html",
        {
            "areas": [(a, r, reservations.rule_lines(r)) for a, r in areas],
            "form": form,
            "schedule": reservations.schedule(user),
            "mine": mine,
            "pending": list(reservations.pending_for_staff(user)),
            "is_staff": policy.can(user, "reservation.manage"),
            "can_configure": policy.can(user, "reservation.configure"),
        },
    )


@login_required
def reservation_detail(request, pk):
    policy.require(request.user, "reservation.view")
    res = get_object_or_404(
        Reservation.objects.for_user(request.user).select_related("area", "lot"), pk=pk
    )
    staff = policy.can(request.user, "reservation.manage")
    rules = reservations.settings_for(res.area)
    return render(
        request,
        "community/reservation_detail.html",
        {
            "res": res,
            "is_staff": staff,
            "can_cancel": reservations.can_cancel(res, request.user),
            "rule_lines": reservations.rule_lines(rules),
            "rules": rules,
            "requester": getattr(res.requested_by, "person", None) if staff else None,
        },
    )


@login_required
@require_POST
def reservation_action(request, pk, action):
    policy.require(request.user, "reservation.view")
    res = get_object_or_404(Reservation.objects.for_user(request.user), pk=pk)
    note = request.POST.get("note", "")
    try:
        if action == "approve":
            reservations.approve(res, actor=request.user, request=request)
            done = "Booking confirmed."
        elif action == "decline":
            reservations.decline(res, note=note, actor=request.user, request=request)
            done = "Booking declined."
        else:
            reservations.cancel(res, note=note, actor=request.user, request=request)
            done = "Booking cancelled."
    except reservations.ReservationError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, done)
    return redirect("reservation_detail", pk=res.pk)


@login_required
def reservation_settings(request, bound=None):
    """Common area booking rules: each area and its rules on one form. Areas
    no longer in use are listed last so they can be brought back."""
    policy.require(request.user, "reservation.configure")
    from community import forms as community_forms

    areas = CommonArea.objects.order_by("-active", "name")
    saved = {r.area_id: r for r in ReservationSettings.objects.filter(area__in=areas)}
    rows = []
    for area in areas:
        rules = saved.get(area.pk) or ReservationSettings(area=area)
        if bound is not None and bound.prefix == f"a{area.pk}":
            rows.append((area, rules, bound))
            continue
        initial = {
            f: getattr(rules, f)
            for f in AreaRulesForm.base_fields
            if f not in ("fee_cents", "deposit_cents")
        }
        for f in ("fee_cents", "deposit_cents"):
            value = getattr(rules, f)
            initial[f] = format_cents(value).lstrip("$") if value is not None else ""
        initial.update({f: getattr(area, f) for f in AreaForm.AREA_FIELDS})
        rows.append((area, rules, AreaForm(initial=initial, prefix=f"a{area.pk}")))
    return render(
        request,
        "community/reservation_settings.html",
        {
            "rows": rows,
            "area_form": community_forms.CommonAreaForm(),
            "can_add_area": policy.can(request.user, "community.manage_rules"),
        },
        status=400 if bound is not None else 200,
    )


@login_required
@require_POST
def reservation_area_save(request, pk):
    """Save one area and its booking rules together (both board only)."""
    from django.db import transaction

    from community import services as community

    policy.require(request.user, "reservation.configure")
    policy.require(request.user, "community.manage_rules")
    area = get_object_or_404(CommonArea, pk=pk)
    form = AreaForm(request.POST, prefix=f"a{area.pk}")
    if form.is_valid():
        try:
            with transaction.atomic():
                community.save_common_area(
                    area=area, actor=request.user, request=request, **form.area_data()
                )
                reservations.save_area_settings(
                    area, actor=request.user, request=request, **form.rules_data()
                )
        except (reservations.ReservationError, community.CommunityError) as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(request, f"Saved {area.name} and its booking rules.")
            return redirect(reverse("reservation_settings") + f"#area-{area.pk}")
    # Invalid: the page again with what was typed and the errors.
    return reservation_settings(request, bound=form)
