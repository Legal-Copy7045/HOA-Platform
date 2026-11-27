"""Pages of the operations finance options: Capital projects (D83), the
owners' Community projects summary, and Insurance claims (D85). Each URL is
in its feature (core.features): switched off, 404."""

from __future__ import annotations

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render

from community.models import CommonArea
from core import policy
from core.money import MoneyField
from documents import attachments
from documents.services import DocumentError
from operations import capital_projects as cp
from operations import insurance_claims as ic
from operations.models import Contract, Invoice, Vendor, WorkOrder
from operations.models_options import (
    CapitalProject,
    CapitalProjectLink,
    ChangeOrder,
    ChangeOrderDecision,
    Funding,
    InsuranceClaim,
    InsuranceMoney,
    LinkKind,
    SettlementOffer,
)


def _date(**kw):
    return forms.DateField(widget=forms.DateInput(attrs={"type": "date"}), **kw)


# --- Capital projects (D83) -----------------------------------------------------------------


class ProjectForm(forms.Form):
    title = forms.CharField(max_length=200)
    description = forms.CharField(widget=forms.Textarea(attrs={"rows": 3}), required=False)
    budget_cents = MoneyField(label="Budget ($)")
    funding = forms.ChoiceField(choices=Funding.choices, label="Paid from")
    special_assessment_ref = forms.CharField(
        max_length=200, required=False, label="Special assessment (resolution), if any"
    )
    reserve_component = forms.ModelChoiceField(
        queryset=None, required=False, label="Reserve plan component (optional)"
    )

    def __init__(self, *args, **kwargs):
        from ledger.models import ReserveComponent

        super().__init__(*args, **kwargs)
        self.fields["reserve_component"].queryset = ReserveComponent.objects.filter(active=True)


@login_required
def capital_projects(request):
    policy.require(request.user, "capital_project.view")
    form = ProjectForm()
    if request.method == "POST":
        policy.require(request.user, "capital_project.manage")
        form = ProjectForm(request.POST)
        if form.is_valid():
            try:
                project = cp.create(actor=request.user, request=request, **form.cleaned_data)
            except cp.ProjectError as exc:
                form.add_error(None, str(exc))
            else:
                messages.success(request, "Project added.")
                return redirect("capital_project_detail", pk=project.pk)
    rows = [(p, cp.figures(p)) for p in CapitalProject.objects.all()]
    return render(
        request,
        "operations/capital_projects.html",
        {
            "rows": rows,
            "form": form,
            "can_manage": policy.can(request.user, "capital_project.manage"),
        },
        status=400 if form.errors else 200,
    )


def _link_choices():
    return {
        LinkKind.CONTRACT: Contract.objects.select_related("vendor").exclude(state="discarded"),
        LinkKind.VENDOR: Vendor.objects.filter(active=True),
        LinkKind.INVOICE: Invoice.objects.select_related("vendor").exclude(
            state__in=["void", "rejected"]
        )[:200],
        LinkKind.WORK_ORDER: WorkOrder.objects.exclude(state="cancelled")[:200],
    }


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@login_required
def capital_project_detail(request, pk):
    policy.require(request.user, "capital_project.view")
    project = get_object_or_404(CapitalProject, pk=pk)
    user = request.user
    if request.method == "POST":
        action = request.POST.get("do", "")
        note = request.POST.get("note", "")
        try:
            if action == "update":
                form = ProjectForm(request.POST)
                form.is_valid()
                data = {k: v for k, v in form.cleaned_data.items() if k != "funding"}
                pct = _int(request.POST.get("percent_complete"))
                cp.update(
                    project,
                    actor=user,
                    request=request,
                    owner_visible=request.POST.get("owner_visible") == "on",
                    percent_complete=pct,
                    **data,
                )
            elif action == "ask_approval":
                cp.ask_approval(
                    project,
                    resolution_ref=request.POST.get("resolution_ref", ""),
                    actor=user,
                    request=request,
                )
            elif action == "approve":
                cp.approve(project, actor=user, request=request)
            elif action in cp.TRANSITIONS:
                cp.change_state(project, action, actor=user, note=note, request=request)
            elif action == "change":
                from core.money import parse_cents

                try:
                    amount = parse_cents(request.POST.get("amount", ""))
                except ValueError as exc:
                    raise cp.ProjectError("Enter the change as an amount, e.g. -500.00.") from exc
                cp.propose_change(
                    project,
                    amount_cents=amount,
                    reason=request.POST.get("reason", ""),
                    actor=user,
                    request=request,
                )
            elif action in ("approve_change", "decline_change"):
                co = get_object_or_404(ChangeOrder, pk=request.POST.get("change"), project=project)
                cp.decide_change(
                    co,
                    decision=ChangeOrderDecision.APPROVE
                    if action == "approve_change"
                    else ChangeOrderDecision.DECLINE,
                    note=note,
                    actor=user,
                    request=request,
                )
            elif action == "link":
                from core.money import parse_cents

                raw = (request.POST.get("committed") or "").strip()
                try:
                    committed = parse_cents(raw) if raw else 0
                except ValueError as exc:
                    raise cp.ProjectError("Enter the committed amount, e.g. 12000.00.") from exc
                cp.link(
                    project,
                    kind=request.POST.get("kind", ""),
                    object_id=_int(request.POST.get("object_id")) or 0,
                    committed_cents=committed,
                    actor=user,
                    request=request,
                )
            elif action == "unlink":
                row = get_object_or_404(
                    CapitalProjectLink, pk=request.POST.get("link"), project=project
                )
                cp.unlink(row, actor=user, request=request)
            elif action == "upload":
                policy.require(user, "capital_project.manage")
                uploads = request.FILES.getlist("file")
                if not uploads:
                    raise cp.ProjectError("Choose a file first.")
                attachments.attach_many(request, project, uploads, actor=user, label=cp.FILE_LABEL)
                return redirect("capital_project_detail", pk=project.pk)
            else:
                raise Http404
        except (cp.ProjectError, DocumentError) as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, "Saved.")
        return redirect("capital_project_detail", pk=project.pk)
    links = cp.active_links(project)
    changes = [
        (co, cp.change_order_status(co), list(co.decisions.all()))
        for co in project.change_orders.prefetch_related("decisions__by").select_related(
            "proposed_by"
        )
    ]
    approvals = list(cp.current_approvals(project).select_related("approver"))
    can_approve = policy.can(user, "capital_project.approve")
    return render(
        request,
        "operations/capital_project_detail.html",
        {
            "obj": project,
            "f": cp.figures(project, links),
            "links": links,
            "changes": changes,
            "approvals": approvals,
            "events": project.events.select_related("by"),
            "files": attachments.attachments_for(project),
            "steps": cp.available_steps(project, user),
            "can_manage": policy.can(user, "capital_project.manage")
            and project.state != "cancelled",
            "can_approve": can_approve,
            "approved_by_me": any(a.approver_id == user.pk for a in approvals),
            "edit_form": ProjectForm(
                initial={
                    "title": project.title,
                    "description": project.description,
                    "budget_cents": f"{project.budget_cents / 100:.2f}",
                    "funding": project.funding,
                    "special_assessment_ref": project.special_assessment_ref,
                    "reserve_component": project.reserve_component_id,
                }
            ),
            "choices": _link_choices(),
            "link_kinds": LinkKind.choices,
            "special_label": cp.special_assessment_label(project),
            "me": user.pk,
        },
    )


@login_required
def capital_project_file(request, pk, file_id):
    policy.require(request.user, "capital_project.view")
    return attachments.serve(request, get_object_or_404(CapitalProject, pk=pk), file_id)


@login_required
def community_projects(request):
    """Owners: the projects the board shares, title, status, budget and spent."""
    policy.require(request.user, "capital_project.view_summary")
    return render(request, "operations/community_projects.html", {"projects": cp.owner_summaries()})


# --- Insurance claims (D85) ---------------------------------------------------------------


class ClaimForm(forms.Form):
    carrier = forms.CharField(max_length=200, label="Insurance company")
    policy_number = forms.CharField(max_length=80, required=False)
    incident_on = _date(label="Date of the loss")
    discovered_on = _date(required=False, label="Date found")
    description = forms.CharField(widget=forms.Textarea(attrs={"rows": 3}))
    location = forms.CharField(max_length=200, required=False)
    common_area = forms.ModelChoiceField(
        queryset=CommonArea.objects.filter(active=True), required=False
    )
    claim_number = forms.CharField(max_length=80, required=False)
    adjuster_name = forms.CharField(max_length=120, required=False, label="Adjuster")
    adjuster_contact = forms.CharField(
        max_length=200, required=False, label="Adjuster's phone or email"
    )
    estimate_cents = MoneyField(required=False, label="Estimate ($)")
    deductible_cents = MoneyField(required=False, label="Deductible ($)")
    proof_of_loss_due = _date(required=False, label="Proof of loss due")
    appeal_deadline = _date(required=False, label="Appeal deadline")
    work_orders = forms.ModelMultipleChoiceField(
        queryset=WorkOrder.objects.all(), required=False, label="Related work orders"
    )


def _claim_values(form) -> dict:
    data = dict(form.cleaned_data)
    for key in ("estimate_cents", "deductible_cents"):
        if data.get(key) in ("", None):
            data[key] = None
    return data


@login_required
def insurance_claims(request):
    policy.require(request.user, "insurance.view")
    form = ClaimForm()
    if request.method == "POST":
        policy.require(request.user, "insurance.record")
        form = ClaimForm(request.POST)
        if form.is_valid():
            data = _claim_values(form)
            work_orders = data.pop("work_orders")
            try:
                claim = ic.create(
                    actor=request.user, request=request, work_orders=work_orders, **data
                )
            except ic.ClaimError as exc:
                form.add_error(None, str(exc))
            else:
                messages.success(request, "Claim recorded.")
                return redirect("insurance_claim_detail", pk=claim.pk)
    claims = list(InsuranceClaim.objects.all())
    rows = [(c, ic.money_totals(c), ic.deadlines(c), ic.policy_label(c)) for c in claims]
    return render(
        request,
        "operations/insurance_claims.html",
        {"rows": rows, "form": form, "can_record": policy.can(request.user, "insurance.record")},
        status=400 if form.errors else 200,
    )


@login_required
def insurance_claim_detail(request, pk):
    policy.require(request.user, "insurance.view")
    claim = get_object_or_404(InsuranceClaim, pk=pk)
    user = request.user
    if request.method == "POST":
        action = request.POST.get("do", "")
        note = request.POST.get("note", "")
        from core.money import parse_cents

        try:
            if action == "update":
                form = ClaimForm(request.POST)
                if not form.is_valid():
                    raise ic.ClaimError(
                        "Check the claim's details: "
                        + "; ".join(f"{k}: {v[0]}" for k, v in form.errors.items())
                    )
                data = _claim_values(form)
                work_orders = data.pop("work_orders")
                ic.update(claim, actor=user, request=request, work_orders=work_orders, **data)
            elif action in ic.TRANSITIONS:
                ic.change_state(claim, action, actor=user, note=note, request=request)
            elif action == "offer":
                try:
                    amount = parse_cents(request.POST.get("amount", ""))
                except ValueError as exc:
                    raise ic.ClaimError("Enter the offer, e.g. 12000.00.") from exc
                ic.record_offer(claim, amount_cents=amount, note=note, actor=user, request=request)
            elif action == "approve_offer":
                offer = get_object_or_404(
                    SettlementOffer, pk=request.POST.get("offer"), claim=claim
                )
                ic.approve_offer(offer, actor=user, request=request)
            elif action == "money":
                try:
                    amount = parse_cents(request.POST.get("amount", ""))
                except ValueError as exc:
                    raise ic.ClaimError("Enter the amount, e.g. 12000.00.") from exc
                import datetime

                try:
                    on = datetime.date.fromisoformat(request.POST.get("on", ""))
                except ValueError as exc:
                    raise ic.ClaimError("Give the date.") from exc
                ic.record_money(
                    claim,
                    kind=request.POST.get("kind", ""),
                    amount_cents=amount,
                    on=on,
                    reference=request.POST.get("reference", ""),
                    actor=user,
                    request=request,
                )
            elif action == "upload":
                policy.require(user, "insurance.record")
                uploads = request.FILES.getlist("file")
                if not uploads:
                    raise ic.ClaimError("Choose a file first.")
                attachments.attach_many(request, claim, uploads, actor=user, label=ic.FILE_LABEL)
                return redirect("insurance_claim_detail", pk=claim.pk)
            else:
                raise Http404
        except (ic.ClaimError, DocumentError) as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, "Saved.")
        return redirect("insurance_claim_detail", pk=claim.pk)
    offers = [
        (o, ic.offer_status(o), [a.approver_id for a in o.approvals.all()])
        for o in claim.offers.prefetch_related("approvals").select_related("recorded_by")
    ]
    latest = offers[-1][0] if offers else None
    initial = {f: getattr(claim, f) for f in ic.FIELDS}
    for key in ("estimate_cents", "deductible_cents"):
        if initial[key] is not None:
            initial[key] = f"{initial[key] / 100:.2f}"
    initial["common_area"] = claim.common_area_id
    initial["work_orders"] = list(claim.work_orders.values_list("pk", flat=True))
    return render(
        request,
        "operations/insurance_claim_detail.html",
        {
            "obj": claim,
            "policy_label": ic.policy_label(claim),
            "totals": ic.money_totals(claim),
            "money": claim.money.select_related("recorded_by"),
            "offers": offers,
            "latest_offer": latest,
            "deadlines": ic.deadlines(claim),
            "events": claim.events.select_related("by"),
            "files": attachments.attachments_for(claim),
            "steps": ic.available_steps(claim, user),
            "can_record": policy.can(user, "insurance.record") and claim.state != "closed",
            "can_decide": policy.can(user, "insurance.decide"),
            "offer_open": claim.state in ("reported", "under_review"),
            "edit_form": ClaimForm(initial=initial),
            "work_orders": claim.work_orders.all(),
            "money_kinds": [
                (InsuranceMoney.EXPECTED, "Expected"),
                (InsuranceMoney.RECEIVED, "Received"),
            ],
            "me": user.pk,
        },
    )


@login_required
def insurance_claim_file(request, pk, file_id):
    policy.require(request.user, "insurance.view")
    return attachments.serve(request, get_object_or_404(InsuranceClaim, pk=pk), file_id)
