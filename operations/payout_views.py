"""Pages for expense reimbursements and vendor checks (D77). The services
check each step's permission and the own-matter and D66 rules; claims are
fetched only through their scoped manager."""

from __future__ import annotations

import datetime

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from community.rules import rule_with_citation
from core import conflicts, policy
from core.money import MoneyField
from core.scoping import own_records, wants_mine
from documents import attachments
from documents.attachments import pdf_response
from ledger import check_numbers
from operations import reimbursements as rb
from operations import vendor_checks as vc
from operations.models import ExpenseCategory, ReimbursementClaim, VendorCheck


def _date(**kw):
    return forms.DateField(widget=forms.DateInput(attrs={"type": "date"}), **kw)


class ClaimForm(forms.Form):
    amount_cents = MoneyField(label="Amount you paid ($)")
    spent_on = _date(label="Date of the expense")
    payee = forms.CharField(label="Paid to (shop or vendor)", max_length=120)
    category = forms.ModelChoiceField(
        label="Expense category", queryset=ExpenseCategory.objects.none()
    )
    description = forms.CharField(
        label="What it was for", max_length=2000, widget=forms.Textarea(attrs={"rows": 3})
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["category"].queryset = ExpenseCategory.objects.filter(active=True).order_by(
            "code"
        )


# --- Reimbursements -----------------------------------------------------------------------


def _staff(user) -> bool:
    return policy.can(user, "reimbursement.view_all")


@login_required
def reimbursements(request):
    user = request.user
    if not (_staff(user) or policy.can(user, "reimbursement.submit")):
        policy.require(user, "reimbursement.submit")
    claims = ReimbursementClaim.objects.for_user(user)
    mine = wants_mine(request) or not _staff(user)
    if mine:
        person_id = policy.roles_for(user).person_id
        claims = own_records(claims, user).filter(claimant_id=person_id)
    return render(
        request,
        "operations/reimbursements.html",
        {
            "rows": rb.rows(claims),
            "mine": mine,
            "can_submit": policy.can(user, "reimbursement.submit"),
            "labels": rb.STATUS_LABELS,
        },
    )


@login_required
def reimbursement_new(request):
    policy.require(request.user, "reimbursement.submit")
    form = ClaimForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        try:
            claim = rb.submit(
                amount_cents=d["amount_cents"],
                spent_on=d["spent_on"],
                payee=d["payee"],
                category=d["category"],
                description=d["description"],
                files=request.FILES.getlist("receipts"),
                actor=request.user,
                request=request,
            )
        except rb.ReimbursementError as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(request, "Submitted. Board members other than you approve it.")
            return redirect("reimbursement_detail", pk=claim.pk)
    threshold, citation = rule_with_citation(rb.SECOND_APPROVAL_RULE)
    return render(
        request,
        "operations/reimbursement_new.html",
        {"form": form, "threshold": threshold, "citation": citation},
        status=400 if request.method == "POST" else 200,
    )


def _claim(request, pk) -> ReimbursementClaim:
    return get_object_or_404(
        ReimbursementClaim.objects.for_user(request.user).select_related("claimant", "category"),
        pk=pk,
    )


@login_required
def reimbursement_detail(request, pk):
    claim = _claim(request, pk)
    user = request.user
    st = rb.state(claim)
    own = conflicts.page_context(user, claim)
    files = [{"file": f, "scan": attachments.current_scan(f)} for f in rb.receipts(claim)]
    refusal = rb.payment_refusal(user, claim)
    return render(
        request,
        "operations/reimbursement_detail.html",
        {
            "claim": claim,
            "st": st,
            "label": rb.STATUS_LABELS[st["status"]],
            "files": files,
            **own,
            "can_decide": st["status"] == "awaiting"
            and policy.can(user, "reimbursement.approve")
            and not own["own_matter"]
            and claim.submitted_by_id != user.pk
            and user.pk not in st["approvers"],
            "clean_receipt": rb.has_clean_receipt(claim),
            "can_withdraw": st["status"] == "awaiting" and claim.submitted_by_id == user.pk,
            "can_pay": st["status"] == "approved"
            and policy.can(user, "reimbursement.pay")
            and not refusal,
            "pay_refusal": refusal if st["status"] == "approved" else "",
            "today": timezone.localdate(),
        },
    )


@login_required
@require_POST
def reimbursement_action(request, pk, action):
    claim = _claim(request, pk)
    note = request.POST.get("note", "")
    try:
        if action == "approve":
            rb.approve(claim, note=note, actor=request.user, request=request)
            messages.success(request, "Approval recorded.")
        elif action == "decline":
            rb.decline(claim, note=note, actor=request.user, request=request)
            messages.success(request, "Declined. The claimant sees why.")
        elif action == "withdraw":
            rb.withdraw(claim, actor=request.user, request=request)
            messages.success(request, "Withdrawn.")
        elif action == "transfer":
            try:
                paid_on = datetime.date.fromisoformat(request.POST.get("paid_on", ""))
            except ValueError as exc:
                raise rb.ReimbursementError("Give the date of the transfer.") from exc
            rb.pay_by_transfer(
                claim,
                paid_on=paid_on,
                reference=request.POST.get("reference", ""),
                actor=request.user,
                request=request,
            )
            messages.success(request, "Recorded as paid by bank transfer.")
        else:
            raise Http404
    except rb.ReimbursementError as exc:
        messages.error(request, str(exc))
    return redirect("reimbursement_detail", pk=claim.pk)


@login_required
def reimbursement_file(request, pk, file_id):
    return attachments.serve(request, _claim(request, pk), file_id)


# --- Vendor checks ---------------------------------------------------------------------------


@login_required
def vendor_checks(request):
    user = request.user
    policy.require(user, "vendor_check.view")
    can_issue = policy.can(user, "vendor_check.issue")
    if request.method == "POST":
        policy.require(user, "vendor_check.issue")
        try:
            issued_on = datetime.date.fromisoformat(request.POST.get("issued_on", ""))
        except ValueError:
            messages.error(request, "Give the date on the checks.")
            return redirect("vendor_checks")
        try:
            checks = vc.issue(
                request.POST.getlist("invoice"),
                first_number=request.POST.get("first_number", ""),
                issued_on=issued_on,
                actor=user,
                request=request,
            )
        except vc.VendorCheckError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, f"{len(checks)} check(s) recorded; the invoices are paid.")
        return redirect("vendor_checks")
    register = vc.register()
    for row in register:
        claim = None
        from operations.reimbursements import claim_for_invoice

        claim = claim_for_invoice(row["check"].invoice)
        row["refusal"] = rb.payment_refusal(user, claim) if claim is not None else ""
    issued = [r["check"] for r in register if r["status"] == "issued"]
    suggestions = vc.clearing_suggestions(issued) if issued else {}
    for row in register:
        row["suggestion"] = suggestions.get(row["check"].pk)
    outstanding = vc.outstanding_checks(timezone.localdate())
    return render(
        request,
        "operations/vendor_checks.html",
        {
            "payables": vc.payables(user) if can_issue else [],
            "register": register,
            "outstanding_cents": sum(c.amount_cents for c in outstanding if c.cleared_on is None),
            "can_issue": can_issue,
            "can_void": policy.can(user, "vendor_check.void"),
            "next_number": check_numbers.suggest_next(),
            "today": timezone.localdate(),
        },
    )


@login_required
def vendor_checks_pdf(request):
    policy.require(request.user, "vendor_check.issue")
    raw = request.GET.get("on") or request.GET.get("issued_on") or ""
    try:
        on = datetime.date.fromisoformat(raw) if raw else timezone.localdate()
    except ValueError as exc:
        raise Http404 from exc
    try:
        data = vc.checks_pdf(
            request.GET.getlist("invoice"), on=on, actor=request.user, request=request
        )
    except vc.VendorCheckError as exc:
        messages.error(request, str(exc))
        return redirect("vendor_checks")
    return pdf_response(data, "checks.pdf")


@login_required
@require_POST
def vendor_check_action(request, pk, action):
    policy.require(request.user, "vendor_check.view")
    check = get_object_or_404(VendorCheck.objects.select_related("invoice"), pk=pk)
    try:
        if action == "clear":
            bank_line = None
            if request.POST.get("bank_line"):
                from ledger.models import BankTransaction

                bank_line = get_object_or_404(BankTransaction, pk=request.POST["bank_line"])
            on = request.POST.get("on")
            vc.mark_cleared(
                check,
                on=datetime.date.fromisoformat(on) if on else None,
                bank_transaction=bank_line,
                actor=request.user,
                request=request,
            )
            messages.success(request, f"Check #{check.number} marked cleared.")
        elif action == "void":
            vc.void_check(
                check, reason=request.POST.get("reason", ""), actor=request.user, request=request
            )
            messages.success(request, f"Check #{check.number} voided; the invoice is open again.")
        else:
            raise Http404
    except (vc.VendorCheckError, ValueError) as exc:
        messages.error(request, str(exc))
    return redirect("vendor_checks")
