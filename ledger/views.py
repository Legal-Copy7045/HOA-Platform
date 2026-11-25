from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from audit.services import record_event
from community.models import Lot, Ownership
from core import policy
from core.money import MoneyField
from core.scoping import system_access
from core.workflow import WorkflowError
from documents import attachments
from documents.attachments import pdf_response
from ledger import services
from ledger.models import (
    Budget,
    CollectionCase,
    EntryKind,
    IncomeSource,
    LedgerEntry,
    LineKind,
    ResaleRequest,
)
from operations.models import ExpenseCategory
from payments.providers import provider


def _date(**kw):
    return forms.DateField(widget=forms.DateInput(attrs={"type": "date"}), **kw)


class PaymentForm(forms.Form):
    amount_cents = MoneyField(label="Amount ($)")
    received_on = _date(initial=timezone.localdate)
    method = forms.ChoiceField(
        choices=[
            ("check", "Check"),
            ("ach", "ACH"),
            ("cash", "Cash"),
            ("money_order", "Money order"),
            ("other", "Other"),
        ]
    )
    reference = forms.CharField(
        max_length=40, required=False, help_text="Check number - never account numbers."
    )


class ChargeForm(forms.Form):
    kind = forms.ChoiceField(
        choices=[
            (EntryKind.OTHER_CHARGE, "Other charge"),
            (EntryKind.LATE_FEE, "Late fee (disabled pending review)"),
        ]
    )
    amount_cents = MoneyField(label="Amount ($)")
    description = forms.CharField(max_length=200)


class CreditForm(forms.Form):
    amount_cents = MoneyField(label="Amount ($)")
    description = forms.CharField(max_length=200, label="Reason")


def _flash(request, fn, ok):
    try:
        result = fn()
    except (services.LedgerError, WorkflowError) as exc:
        messages.error(request, str(exc))
        return None
    messages.success(request, ok)
    return result


# --- Owner -----------------------------------------------------------------------------


def _ownership_start(user, lot_id):
    with system_access("ownership start for statement"):
        o = (
            Ownership.objects.filter(person__user=user, lot_id=lot_id)
            .active()
            .order_by("start_date")
            .first()
        )
    return o.start_date if o else None


@login_required
def my_account(request):
    policy.require(request.user, "ledger.view_own")
    accounts = []
    for lot in Lot.objects.for_user(request.user).filter(
        pk__in=policy.roles_for(request.user).owned_lot_ids
    ):
        since = _ownership_start(request.user, lot.pk)
        accounts.append({"lot": lot, "statement": services.owner_statement(lot.pk, since)})
    return render(
        request, "ledger/my_account.html", {"accounts": accounts, "online": provider().available}
    )


@login_required
def my_statement_pdf(request, lot_id):
    policy.require(request.user, "ledger.view_own")
    if lot_id not in policy.roles_for(request.user).owned_lot_ids:
        raise Http404
    lot = get_object_or_404(Lot.objects.for_user(request.user), pk=lot_id)
    record_event(
        "ledger.statement_downloaded",
        request=request,
        actor=request.user,
        target=lot,
        metadata={"lot_id": lot.pk},
    )
    return pdf_response(
        services.statement_pdf(lot, since=_ownership_start(request.user, lot.pk)),
        f"statement-lot-{lot.number}.pdf",
    )


# --- Board ledger ----------------------------------------------------------------------------


def _finance(request):
    if not (policy.can(request.user, "finance.view") or policy.can(request.user, "ledger.manage")):
        policy.require(request.user, "finance.view")


@login_required
def ledger_home(request):
    _finance(request)
    if request.method == "POST":
        year, month = int(request.POST.get("year", 0)), int(request.POST.get("month", 0))
        _flash(
            request,
            lambda: services.post_monthly_assessments(
                year=year, month=month, actor=request.user, request=request
            ),
            "Assessments posted (lots already charged for that month were skipped).",
        )
        return redirect("ledger_home")
    bal = services.balances()
    lots = Lot.objects.for_user(request.user)
    today = timezone.localdate()
    return render(
        request,
        "ledger/home.html",
        {
            "rows": [(lot, bal.get(lot.pk, 0)) for lot in lots],
            "total_owed": sum(v for v in bal.values() if v > 0),
            "total_credit": -sum(v for v in bal.values() if v < 0),
            "year": today.year,
            "month": today.month,
            "can_manage": policy.can(request.user, "ledger.manage"),
        },
    )


@login_required
def lot_ledger(request, pk):
    _finance(request)
    lot = get_object_or_404(Lot.objects.for_user(request.user), pk=pk)
    entries = (
        LedgerEntry.objects.for_user(request.user).filter(lot=lot).select_related("reversed_by")
    )
    return render(
        request,
        "ledger/lot.html",
        {
            "lot": lot,
            "entries": entries,
            "balance": services.balance(lot.pk),
            "aging": services.aging(lot.pk),
            "can_manage": policy.can(request.user, "ledger.manage"),
            "payment_form": PaymentForm(),
            "charge_form": ChargeForm(),
            "credit_form": CreditForm(),
            "open_case": CollectionCase.objects.for_user(request.user)
            .filter(lot=lot)
            .exclude(state="closed")
            .first(),
        },
    )


@login_required
@require_POST
def lot_ledger_action(request, pk, action):
    _finance(request)
    lot = get_object_or_404(Lot.objects.for_user(request.user), pk=pk)
    actor = request.user
    if action == "payment":
        form = PaymentForm(request.POST)
        if form.is_valid():
            _flash(
                request,
                lambda: services.record_payment(
                    lot=lot, actor=actor, request=request, **form.cleaned_data
                ),
                "Payment recorded.",
            )
    elif action == "charge":
        form = ChargeForm(request.POST)
        if form.is_valid():
            _flash(
                request,
                lambda: services.post_charge(
                    lot=lot, actor=actor, request=request, **form.cleaned_data
                ),
                "Charge posted.",
            )
    elif action == "credit":
        form = CreditForm(request.POST)
        if form.is_valid():
            _flash(
                request,
                lambda: services.post_credit(
                    lot=lot, actor=actor, request=request, **form.cleaned_data
                ),
                "Credit posted.",
            )
    elif action == "reverse":
        entry = get_object_or_404(
            LedgerEntry.objects.for_user(actor), pk=request.POST.get("entry"), lot=lot
        )
        _flash(
            request,
            lambda: services.reverse_entry(
                entry, reason=request.POST.get("reason", ""), actor=actor, request=request
            ),
            "Reversal posted.",
        )
    elif action == "collection":
        case = _flash(
            request,
            lambda: services.open_case(lot=lot, actor=actor, request=request),
            "Collection case opened.",
        )
        if case:
            return redirect("collection_detail", pk=case.pk)
    else:
        raise Http404
    return redirect("lot_ledger", pk=lot.pk)


@login_required
def lot_statement_pdf(request, pk, kind):
    _finance(request)
    lot = get_object_or_404(Lot.objects.for_user(request.user), pk=pk)
    record_event(
        "ledger.statement_downloaded",
        request=request,
        actor=request.user,
        target=lot,
        metadata={"lot_id": lot.pk},
    )
    return pdf_response(
        services.statement_pdf(lot, reminder=kind == "reminder"), f"{kind}-lot-{lot.number}.pdf"
    )


# --- Collections -----------------------------------------------------------------------------


@login_required
def collections(request):
    _finance(request)
    return render(
        request,
        "ledger/collections.html",
        {
            "cases": CollectionCase.objects.for_user(request.user).select_related("lot"),
            "delinquent": services.delinquent_lots(),
        },
    )


@login_required
def collection_detail(request, pk):
    _finance(request)
    case = get_object_or_404(CollectionCase.objects.for_user(request.user), pk=pk)
    return render(
        request,
        "ledger/collection_detail.html",
        {
            "obj": case,
            "balance": services.balance(case.lot_id),
            "actions": services.COLLECTION.available(case, request.user),
            "history": services.COLLECTION.history(case),
            "action_url_name": "collection_action",
        },
    )


@login_required
@require_POST
def collection_action(request, pk, action):
    case = get_object_or_404(CollectionCase.objects.for_user(request.user), pk=pk)
    if action not in services.COLLECTION.transitions:
        raise Http404
    _flash(
        request,
        lambda: services.COLLECTION.apply(
            case, action, actor=request.user, note=request.POST.get("note", ""), request=request
        ),
        "Updated.",
    )
    return redirect("collection_detail", pk=case.pk)


# --- Budgets ---------------------------------------------------------------------------------


class LineForm(forms.Form):
    kind = forms.ChoiceField(choices=LineKind.choices)
    name = forms.CharField(max_length=120)
    annual_cents = MoneyField(label="Annual amount ($)")
    category = forms.ModelChoiceField(
        queryset=ExpenseCategory.objects.filter(active=True),
        required=False,
        help_text="Expense lines",
    )
    income_source = forms.ChoiceField(
        choices=[("", "-"), *IncomeSource.choices], required=False, help_text="Income lines"
    )


@login_required
def budgets(request):
    _finance(request)
    if request.method == "POST":
        policy.require(request.user, "budget.manage")
        if "code" in request.POST:
            from operations import services as ops

            try:
                ops.save_category(
                    code=request.POST.get("code", ""),
                    name=request.POST.get("name", ""),
                    actor=request.user,
                    request=request,
                )
                messages.success(request, "Category added.")
            except ops.OperationsError as exc:
                messages.error(request, str(exc))
            return redirect("budgets")
        budget = _flash(
            request,
            lambda: services.create_budget(
                fiscal_year=int(request.POST.get("fiscal_year") or 0),
                actor=request.user,
                request=request,
            ),
            "Budget created.",
        )
        return redirect("budget_detail", pk=budget.pk) if budget else redirect("budgets")
    return render(
        request,
        "ledger/budgets.html",
        {
            "budgets": Budget.objects.all(),
            "categories": ExpenseCategory.objects.all(),
            "year": timezone.localdate().year + 1,
            "can_manage": policy.can(request.user, "budget.manage"),
        },
    )


@login_required
def budget_detail(request, pk):
    _finance(request)
    budget = get_object_or_404(Budget, pk=pk)
    if request.method == "POST":
        policy.require(request.user, "budget.manage")
        if request.POST.get("adopt"):
            _flash(
                request,
                lambda: services.adopt_budget(budget, actor=request.user, request=request),
                "Budget adopted.",
            )
        else:
            form = LineForm(request.POST)
            if form.is_valid():
                _flash(
                    request,
                    lambda: services.add_line(
                        budget, actor=request.user, request=request, **form.cleaned_data
                    ),
                    "Line added.",
                )
            else:
                messages.error(request, "Check the line.")
        return redirect("budget_detail", pk=budget.pk)
    return render(
        request,
        "ledger/budget_detail.html",
        {
            "budget": budget,
            "report": services.budget_vs_actual(budget),
            "form": LineForm(),
            "can_manage": policy.can(request.user, "budget.manage"),
        },
    )


# --- Resale certificates -------------------------------------------------------------------


class ResaleForm(forms.Form):
    lot = forms.ModelChoiceField(queryset=Lot.objects.none())
    requester_name = forms.CharField(max_length=120, label="Requested by (name)")
    requester_email = forms.EmailField(required=False)
    expected_settlement = _date(required=False)

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        lots = Lot.objects.for_user(user)
        if not policy.can(user, "resale.manage"):
            lots = lots.filter(pk__in=policy.roles_for(user).owned_lot_ids)
        self.fields["lot"].queryset = lots


@login_required
def resales(request):
    policy.require(request.user, "resale.request")
    form = ResaleForm(request.POST or None, user=request.user)
    if request.method == "POST":
        if form.is_valid():
            obj = _flash(
                request,
                lambda: services.request_resale(
                    actor=request.user, request=request, **form.cleaned_data
                ),
                "Request received.",
            )
            if obj:
                return redirect("resale_detail", pk=obj.pk)
        return redirect("resales")
    return render(
        request,
        "ledger/resales.html",
        {
            "rows": ResaleRequest.objects.for_user(request.user).select_related("lot"),
            "form": form,
            "can_manage": policy.can(request.user, "resale.manage"),
        },
    )


@login_required
def resale_detail(request, pk):
    obj = get_object_or_404(ResaleRequest.objects.for_user(request.user), pk=pk)
    can_manage = policy.can(request.user, "resale.manage")
    if request.method == "POST" and can_manage:
        _flash(
            request,
            lambda: services.save_disclosures(
                obj, request.POST.get("other_disclosures", ""), actor=request.user, request=request
            ),
            "Disclosures saved.",
        )
        return redirect("resale_detail", pk=obj.pk)
    return render(
        request,
        "ledger/resale_detail.html",
        {
            "obj": obj,
            "can_manage": can_manage,
            "actions": services.RESALE.available(obj, request.user),
            "history": services.RESALE.history(obj),
            "action_url_name": "resale_action",
        },
    )


@login_required
@require_POST
def resale_action(request, pk, action):
    obj = get_object_or_404(ResaleRequest.objects.for_user(request.user), pk=pk)
    if action not in services.RESALE.transitions:
        raise Http404
    _flash(
        request,
        lambda: services.RESALE.apply(
            obj, action, actor=request.user, note=request.POST.get("note", ""), request=request
        ),
        "Updated.",
    )
    return redirect("resale_detail", pk=obj.pk)


@login_required
def resale_pdf(request, pk):
    obj = get_object_or_404(ResaleRequest.objects.for_user(request.user), pk=pk)
    if obj.state != "issued" and not policy.can(request.user, "resale.manage"):
        raise Http404
    record_event(
        "resale.certificate_downloaded",
        request=request,
        actor=request.user,
        target=obj,
        metadata={"lot_id": obj.lot_id},
    )
    if obj.state == "issued":
        stored = services.issued_certificate(obj)
        if stored is None:
            raise Http404
        return attachments.serve(request, obj, stored.pk)
    return pdf_response(services.resale_certificate(obj), f"resale-certificate-{obj.pk}.pdf")
