"""Vendors, work orders and invoices (board, manager; auditor read-only)."""

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from community.models import CommonArea
from core import policy
from core.money import MoneyField
from core.workflow import WorkflowError
from documents import attachments
from documents.services import DocumentError
from operations import services
from operations.models import (
    ExpenseCategory,
    Invoice,
    PaymentMethod,
    Priority,
    Vendor,
    WorkOrder,
)


def _date(**kw):
    return forms.DateField(widget=forms.DateInput(attrs={"type": "date"}), **kw)


class VendorForm(forms.Form):
    name = forms.CharField(max_length=200)
    contact_name = forms.CharField(max_length=120, required=False)
    email = forms.EmailField(required=False)
    phone = forms.CharField(max_length=40, required=False)
    services = forms.CharField(max_length=200, required=False)
    insurance_expires_on = _date(required=False)
    w9_on_file = forms.BooleanField(required=False)


class WorkOrderForm(forms.Form):
    title = forms.CharField(max_length=200)
    description = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 3}))
    common_area = forms.ModelChoiceField(
        queryset=CommonArea.objects.filter(active=True), required=False
    )
    vendor = forms.ModelChoiceField(queryset=Vendor.objects.filter(active=True), required=False)
    priority = forms.ChoiceField(choices=Priority.choices, initial=Priority.NORMAL)
    due_on = _date(required=False)


class InvoiceForm(forms.Form):
    vendor = forms.ModelChoiceField(queryset=Vendor.objects.filter(active=True))
    number = forms.CharField(max_length=60, label="Invoice number")
    invoice_date = _date(initial=timezone.localdate)
    due_date = _date(required=False)
    amount_cents = MoneyField(label="Amount ($)")
    category = forms.ModelChoiceField(queryset=ExpenseCategory.objects.filter(active=True))
    work_order = forms.ModelChoiceField(
        queryset=WorkOrder.objects.exclude(state__in=["cancelled"]), required=False
    )
    description = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))


class PaymentForm(forms.Form):
    paid_on = _date(initial=timezone.localdate)
    method = forms.ChoiceField(choices=PaymentMethod.choices)
    amount_cents = MoneyField(label="Amount ($)")
    reference = forms.CharField(
        max_length=40,
        required=False,
        help_text="Check number or short reference. Never account numbers.",
    )


def _view_finance(request):
    if not (policy.can(request.user, "finance.view") or policy.can(request.user, "vendor.manage")):
        policy.require(request.user, "finance.view")


# --- Vendors -----------------------------------------------------------------------------


@login_required
def vendors(request):
    _view_finance(request)
    form = VendorForm(request.POST or None)
    if request.method == "POST":
        if form.is_valid():
            vendor = services.save_vendor(actor=request.user, request=request, **form.cleaned_data)
            return redirect("vendor_detail", pk=vendor.pk)
        messages.error(request, "Check the form.")
        return redirect("vendors")
    soon = timezone.localdate() + timezone.timedelta(days=30)
    return render(
        request,
        "operations/vendors.html",
        {
            "vendors": Vendor.objects.all(),
            "form": form,
            "soon": soon,
            "today": timezone.localdate(),
            "can_manage": policy.can(request.user, "vendor.manage"),
        },
    )


@login_required
def vendor_detail(request, pk):
    _view_finance(request)
    vendor = get_object_or_404(Vendor, pk=pk)
    form = VendorForm(
        request.POST or None, initial={f: getattr(vendor, f) for f in VendorForm.base_fields}
    )
    if request.method == "POST":
        if form.is_valid():
            services.save_vendor(
                vendor=vendor, actor=request.user, request=request, **form.cleaned_data
            )
            messages.success(request, "Vendor saved.")
        return redirect("vendor_detail", pk=vendor.pk)
    return render(
        request,
        "operations/vendor_detail.html",
        {
            "vendor": vendor,
            "form": form,
            "invoices": vendor.invoices.all()[:100],
            "can_manage": policy.can(request.user, "vendor.manage"),
        },
    )


# --- Work orders --------------------------------------------------------------------------


@login_required
def work_orders(request):
    _view_finance(request)
    form = WorkOrderForm(request.POST or None)
    if request.method == "POST":
        if form.is_valid():
            wo = services.create_work_order(
                actor=request.user, request=request, **form.cleaned_data
            )
            return redirect("work_order_detail", pk=wo.pk)
        messages.error(request, "Check the form.")
        return redirect("work_orders")
    return render(
        request,
        "operations/work_orders.html",
        {
            "rows": WorkOrder.objects.select_related("vendor", "common_area"),
            "form": form,
            "can_manage": policy.can(request.user, "workorder.manage"),
        },
    )


@login_required
def work_order_detail(request, pk):
    _view_finance(request)
    wo = get_object_or_404(WorkOrder, pk=pk)
    if request.method == "POST":
        vendor = get_object_or_404(Vendor, pk=request.POST.get("vendor"))
        services.set_work_order_vendor(wo, vendor, actor=request.user, request=request)
        return redirect("work_order_detail", pk=wo.pk)
    return render(
        request,
        "operations/work_order_detail.html",
        {
            "obj": wo,
            "actions": services.WORK_ORDER.available(wo, request.user),
            "history": services.WORK_ORDER.history(wo),
            "action_url_name": "work_order_action",
            "vendors": Vendor.objects.filter(active=True),
            "invoices": wo.invoices.all(),
            "can_manage": policy.can(request.user, "workorder.manage"),
        },
    )


@login_required
@require_POST
def work_order_action(request, pk, action):
    wo = get_object_or_404(WorkOrder, pk=pk)
    try:
        if action not in services.WORK_ORDER.transitions:
            raise WorkflowError("Unknown action.")
        services.WORK_ORDER.apply(
            wo, action, actor=request.user, note=request.POST.get("note", ""), request=request
        )
    except WorkflowError as exc:
        messages.error(request, str(exc))
    return redirect("work_order_detail", pk=wo.pk)


# --- Invoices ------------------------------------------------------------------------------


@login_required
def invoices(request):
    _view_finance(request)
    form = InvoiceForm(request.POST or None)
    if request.method == "POST":
        if form.is_valid():
            try:
                inv = services.create_invoice(
                    actor=request.user, request=request, **form.cleaned_data
                )
            except services.OperationsError as exc:
                messages.error(request, str(exc))
            else:
                messages.success(
                    request,
                    "Invoice entered as a draft. Attach the PDF, then submit it for approval.",
                )
                return redirect("invoice_detail", pk=inv.pk)
        else:
            messages.error(request, "Check the form.")
        return redirect("invoices")
    state = request.GET.get("state", "")
    rows = Invoice.objects.select_related("vendor", "category")
    if state:
        rows = rows.filter(state=state)
    return render(
        request,
        "operations/invoices.html",
        {
            "rows": rows[:500],
            "form": form,
            "state": state,
            "states": ["draft", "pending_approval", "approved", "paid", "rejected", "void"],
            "can_enter": policy.can(request.user, "invoice.enter"),
            "has_categories": ExpenseCategory.objects.exists(),
        },
    )


@login_required
def invoice_detail(request, pk):
    _view_finance(request)
    inv = get_object_or_404(Invoice.objects.select_related("vendor", "category"), pk=pk)
    user = request.user
    already = user.pk in services.approvers(inv)
    return render(
        request,
        "operations/invoice_detail.html",
        {
            "obj": inv,
            "approvals": inv.approvals.select_related("approver"),
            "payments": inv.payments.all(),
            "paid": services.paid_cents(inv),
            "needed": services.approvals_required(inv),
            "files": attachments.attachments_for(inv),
            "actions": [
                t for t in services.INVOICE.available(inv, user) if t.action in ("submit", "void")
            ],
            "history": services.INVOICE.history(inv),
            "action_url_name": "invoice_action",
            "can_approve": (
                inv.state == "pending_approval"
                and policy.can(user, "invoice.approve")
                and inv.entered_by_id != user.pk
                and not already
            ),
            "can_pay": inv.state == "approved" and policy.can(user, "invoice.record_payment"),
            "can_attach": policy.can(user, "invoice.enter") and inv.state not in ("paid", "void"),
            "payment_form": PaymentForm(
                initial={
                    "amount_cents": f"{(inv.amount_cents - services.paid_cents(inv)) / 100:.2f}"
                }
            ),
        },
    )


@login_required
@require_POST
def invoice_action(request, pk, action):
    inv = get_object_or_404(Invoice, pk=pk)
    note = request.POST.get("note", "")
    try:
        if action == "approve":
            services.approve(inv, note=note, actor=request.user, request=request)
        elif action == "reject":
            services.reject(inv, note=note, actor=request.user, request=request)
        elif action == "pay":
            form = PaymentForm(request.POST)
            if not form.is_valid():
                raise services.OperationsError("Check the payment details.")
            services.record_payment(inv, actor=request.user, request=request, **form.cleaned_data)
        elif action == "attach":
            policy.require(request.user, "invoice.enter")
            attachments.attach(inv, request.FILES["file"], actor=request.user, request=request)
        elif action in ("submit", "void"):
            services.INVOICE.apply(inv, action, actor=request.user, note=note, request=request)
        else:
            raise services.OperationsError("Unknown action.")
    except (services.OperationsError, WorkflowError, DocumentError, KeyError) as exc:
        messages.error(request, str(exc) or "Something was missing.")
    return redirect("invoice_detail", pk=inv.pk)


@login_required
def invoice_file(request, pk, file_id):
    _view_finance(request)
    return attachments.serve(request, get_object_or_404(Invoice, pk=pk), file_id)
