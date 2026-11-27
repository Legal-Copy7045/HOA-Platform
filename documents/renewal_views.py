"""Renewals pages (D72): board, manager and auditor see; board and manager
record; only the board decides a contract's renewal."""

from __future__ import annotations

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from core import policy
from core.money import MoneyField, format_cents
from documents import renewals
from documents.models import (
    InsurancePolicy,
    PolicyKind,
    RenewalChoice,
    RenewalType,
    VendorCertificate,
)
from documents.services import DocumentError


def _dollars(cents):
    """Cents as the form shows them ("1,000,000.00"); blank for none."""
    return "" if cents is None else format_cents(cents).lstrip("$")


def _date(**kw):
    return forms.DateField(widget=forms.DateInput(attrs={"type": "date"}), **kw)


class PolicyForm(forms.Form):
    kind = forms.ChoiceField(choices=PolicyKind.choices, label="Kind of policy")
    carrier = forms.CharField(max_length=200, label="Insurance company")
    policy_number = forms.CharField(max_length=60, required=False)
    coverage_cents = MoneyField(label="Coverage ($)", help_text="Per occurrence, or the bond.")
    aggregate_cents = MoneyField(label="Aggregate limit ($)", required=False)
    deductible_cents = MoneyField(label="Deductible ($)", required=False)
    premium_cents = MoneyField(label="Premium for the term ($)", required=False)
    starts_on = _date(label="Starts")
    ends_on = _date(label="Ends")
    notes = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))
    file = forms.FileField(
        required=False,
        label="Policy or declarations page",
        help_text="Filed in the document library (Insurance, board only).",
    )


class CertificateForm(forms.Form):
    expires_on = _date(label="Expires")
    insurer = forms.CharField(max_length=200, required=False, label="Insurance company")
    coverage_cents = MoneyField(label="Liability coverage ($)", required=False)
    file = forms.FileField(required=False, label="Certificate of insurance")


class TermsForm(forms.Form):
    renewal = forms.ChoiceField(choices=RenewalType.choices, label="Renewal")
    notice_days = forms.IntegerField(
        min_value=0,
        max_value=730,
        initial=0,
        label="Days' notice needed to cancel",
        help_text="From the contract's cancellation clause; 0 if none.",
    )
    renews_for_months = forms.IntegerField(
        min_value=1,
        max_value=120,
        required=False,
        label="Each renewal lasts (months)",
        help_text="Only for a contract that renews by itself.",
    )
    file = forms.FileField(required=False, label="Signed contract")


class DecisionForm(forms.Form):
    decision = forms.ChoiceField(choices=RenewalChoice.choices)
    note = forms.CharField(
        max_length=300,
        label="Minutes, resolution or notice sent",
        help_text="e.g. 'Minutes 2027-03-14, item 5' or 'Cancellation letter mailed 2027-02-01'.",
    )


@login_required
def renewals_list(request):
    policy.require(request.user, "renewals.view")
    from operations.models import Vendor

    items = renewals.tracked()
    return render(
        request,
        "documents/renewals.html",
        {
            "items": items,
            "expired": sum(1 for r in items if r.expired),
            "due": sum(1 for r in items if r.due and not r.expired),
            "no_certificate": [
                v
                for v in Vendor.objects.filter(active=True)
                if renewals.vendor_coi_expiry(v) is None
            ],
            "policies": InsurancePolicy.objects.select_related("renews"),
            "can_record": policy.can(request.user, "renewals.record"),
            "rules": {k: renewals.reminder_days(k) for k in renewals.RULES},
        },
    )


@login_required
def policy_new(request):
    policy.require(request.user, "renewals.record")
    renews = None
    raw = request.POST.get("renews") or request.GET.get("renews") or ""
    if raw:
        if not raw.isdigit():
            raise Http404
        renews = get_object_or_404(InsurancePolicy, pk=int(raw))
    initial = {}
    if renews is not None:
        initial = {
            "kind": renews.kind,
            "carrier": renews.carrier,
            "policy_number": renews.policy_number,
            "coverage_cents": _dollars(renews.coverage_cents),
            "starts_on": renews.ends_on,
        }
    form = PolicyForm(request.POST or None, request.FILES or None, initial=initial)
    if renews is not None:
        form.fields["kind"].disabled = True
    if request.method == "POST" and form.is_valid():
        data = dict(form.cleaned_data)
        file = data.pop("file")
        try:
            created = renewals.record_policy(
                actor=request.user, request=request, renews=renews, file=file, **data
            )
        except (renewals.RenewalError, DocumentError) as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(request, "Policy recorded.")
            return redirect("renewal_policy", pk=created.pk)
    return render(request, "documents/renewal_policy_new.html", {"form": form, "renews": renews})


@login_required
def policy_detail(request, pk):
    policy.require(request.user, "renewals.view")
    obj = get_object_or_404(InsurancePolicy.objects.select_related("document", "renews"), pk=pk)
    can_record = policy.can(request.user, "renewals.record")
    form = PolicyForm(
        request.POST or None,
        request.FILES or None,
        initial={
            f: _dollars(getattr(obj, f)) if f.endswith("_cents") else getattr(obj, f)
            for f in renewals.POLICY_FIELDS
        },
    )
    if obj.renews_id:
        form.fields["kind"].disabled = True
    if request.method == "POST":
        policy.require(request.user, "renewals.record")
        if form.is_valid():
            data = dict(form.cleaned_data)
            file = data.pop("file")
            try:
                renewals.update_policy(obj, actor=request.user, request=request, file=file, **data)
            except (renewals.RenewalError, DocumentError) as exc:
                form.add_error(None, str(exc))
            else:
                messages.success(request, "Policy saved.")
                return redirect("renewal_policy", pk=obj.pk)
    chain, earlier = [], obj.renews
    while earlier is not None and len(chain) < 50:
        chain.append(earlier)
        earlier = earlier.renews
    renewed_by = InsurancePolicy.objects.filter(renews=obj).first()
    return render(
        request,
        "documents/renewal_policy.html",
        {
            "obj": obj,
            "form": form,
            "can_record": can_record,
            "earlier": chain,
            "renewed_by": renewed_by,
            "item": next((r for r in renewals.tracked() if r.key == ("policy", obj.pk)), None),
            "history": obj.history.select_related("history_user")[:20],
        },
    )


@login_required
def vendor_detail(request, pk):
    policy.require(request.user, "renewals.view")
    from operations.models import Vendor

    vendor = get_object_or_404(Vendor, pk=pk)
    form = CertificateForm(request.POST or None, request.FILES or None)
    if request.method == "POST":
        policy.require(request.user, "renewals.record")
        if form.is_valid():
            try:
                renewals.record_vendor_certificate(
                    vendor, actor=request.user, request=request, **form.cleaned_data
                )
            except (renewals.RenewalError, DocumentError) as exc:
                form.add_error(None, str(exc))
            else:
                messages.success(request, "Certificate recorded.")
                return redirect("renewal_vendor", pk=vendor.pk)
    return render(
        request,
        "documents/renewal_vendor.html",
        {
            "vendor": vendor,
            "form": form,
            "certificates": VendorCertificate.objects.filter(vendor=vendor).select_related(
                "document", "recorded_by"
            ),
            "can_record": policy.can(request.user, "renewals.record"),
            "today": timezone.localdate(),
            "expires": renewals.vendor_coi_expiry(vendor),
            "item": next((r for r in renewals.tracked() if r.key == ("vendor", vendor.pk)), None),
        },
    )


def _terms_form(contract, data=None, files=None):
    terms = getattr(contract, "renewal_terms", None)
    initial = (
        {
            "renewal": terms.renewal,
            "notice_days": terms.notice_days,
            "renews_for_months": terms.renews_for_months,
        }
        if terms
        else {"renewal": RenewalType.MANUAL}
    )
    return TermsForm(data, files, initial=initial)


def _contract_page(request, contract, *, form=None, decision_form=None):
    return render(
        request,
        "documents/renewal_contract.html",
        {
            "contract": contract,
            "terms": getattr(contract, "renewal_terms", None),
            "form": form or _terms_form(contract),
            "decision_form": decision_form or DecisionForm(),
            "term": renewals.current_contract_term(contract),
            "decisions": contract.renewal_decisions.select_related("decided_by"),
            "can_record": policy.can(request.user, "renewals.record"),
            "can_decide": policy.can(request.user, "renewals.decide"),
        },
    )


@login_required
def contract_detail(request, pk):
    policy.require(request.user, "renewals.view")
    from operations.models import Contract

    contract = get_object_or_404(Contract.objects.select_related("vendor"), pk=pk)
    form = None
    if request.method == "POST":
        policy.require(request.user, "renewals.record")
        form = _terms_form(contract, request.POST, request.FILES)
        if form.is_valid():
            try:
                renewals.save_contract_terms(
                    contract, actor=request.user, request=request, **form.cleaned_data
                )
            except (renewals.RenewalError, DocumentError) as exc:
                form.add_error(None, str(exc))
            else:
                messages.success(request, "Renewal terms saved.")
                return redirect("renewal_contract", pk=contract.pk)
    return _contract_page(request, contract, form=form)


@login_required
@require_POST
def contract_decide(request, pk):
    policy.require(request.user, "renewals.decide")
    from operations.models import Contract

    contract = get_object_or_404(Contract.objects.select_related("vendor"), pk=pk)
    form = DecisionForm(request.POST)
    if form.is_valid():
        try:
            renewals.decide_contract(
                contract, actor=request.user, request=request, **form.cleaned_data
            )
        except renewals.RenewalError as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(request, "Decision recorded.")
            return redirect("renewal_contract", pk=contract.pk)
    return _contract_page(request, contract, decision_form=form)
