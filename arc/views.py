from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from arc import services
from arc.models import ArcRequest, ArcState, Decision, Position
from audit.services import record_event
from community.models import Lot
from core import policy
from core.workflow import WorkflowError
from documents import attachments
from documents.services import DocumentError


class NewRequestForm(forms.Form):
    lot = forms.ModelChoiceField(queryset=Lot.objects.none())
    title = forms.CharField(max_length=200, help_text="e.g. 'Rear fence, 4 ft black aluminium'")
    project_type = forms.ChoiceField(label="Kind of project", required=False)
    description = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 5}),
        help_text="Materials, colours, dimensions, location, contractor.",
    )
    planned_start = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["lot"].queryset = Lot.objects.for_user(user).filter(
            pk__in=policy.roles_for(user).owned_lot_ids
        )
        types = services.active_project_types()
        if types:
            self.fields["project_type"].required = True
            self.fields["project_type"].choices = [("", "Choose...")] + [
                (t.name, t.name) for t in types
            ]
            guidance = "; ".join(f"{t.name}: {t.guidance}" for t in types if t.guidance)
            self.fields["project_type"].help_text = guidance[:600]
        else:
            self.fields["project_type"] = forms.CharField(
                max_length=60, required=False, label="Kind of project"
            )


class DecisionForm(forms.Form):
    outcome = forms.ChoiceField(
        choices=[
            (ArcState.APPROVED, "Approve"),
            (ArcState.APPROVED_WITH_CONDITIONS, "Approve with conditions"),
            (ArcState.DENIED, "Deny"),
        ]
    )
    conditions = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 3}))
    reasons = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 3}))
    citation = forms.CharField(
        max_length=200, required=False, help_text="Governing section, e.g. Declaration §9.3"
    )


def _affected_numbers(req) -> list[int]:
    from core.scoping import system_access

    with system_access("affected lot numbers for intake form"):
        return list(req.affected_lots.values_list("number", flat=True))


def _get(request, pk):
    return get_object_or_404(ArcRequest.objects.for_user(request.user), pk=pk)


@login_required
def arc_list(request):
    can_submit = policy.can(request.user, "arc.submit")
    can_view_all = policy.can(request.user, "arc.view_all")
    if not (can_submit or can_view_all):
        policy.require(request.user, "arc.view_all")
    form = NewRequestForm(request.POST or None, user=request.user)
    if request.method == "POST":
        policy.require(request.user, "arc.submit")
        if form.is_valid():
            try:
                req = services.create_request(
                    actor=request.user, request=request, **form.cleaned_data
                )
            except services.ArcError as exc:
                messages.error(request, str(exc))
            else:
                messages.success(request, "Draft created. Attach plans or photos, then submit.")
                return redirect("arc_detail", pk=req.pk)
        else:
            messages.error(request, "Check the form.")
        return redirect("arc_list")
    from core.lists import csv_response, filter_list, paginate, wants_csv
    from core.scoping import own_records, wants_mine

    mine = wants_mine(request) or not can_view_all
    rows = ArcRequest.objects.for_user(request.user).select_related("lot")
    if mine and can_view_all:
        rows = own_records(rows, request.user)
    rows, tools = filter_list(
        request,
        rows,
        date_field="created_at",
        open_states=("draft", "submitted", "completeness_check", "in_review", "appeal"),
    )
    if wants_csv(request) and can_view_all and not mine:
        return csv_response(
            request,
            "arc-requests",
            ["Number", "Project", "Type", "Lot", "Status", "Decision due", "Created"],
            (
                [
                    f"ARC-{r.pk}",
                    r.title,
                    r.project_type,
                    r.lot.number,
                    r.get_state_display(),
                    r.review_due_on or "",
                    r.created_at.date(),
                ]
                for r in rows
            ),
            audit_kind="arc",
        )
    page = paginate(request, rows)
    rows = page.object_list
    return render(
        request,
        "arc/list.html",
        {
            "rows": rows,
            "form": form,
            "can_submit": can_submit and mine,
            "can_view_all": can_view_all,
            "mine": mine,
            "page": page,
            "tools": tools,
            "can_export": can_view_all and not mine,
        },
    )


@login_required
def arc_detail(request, pk):
    req = _get(request, pk)
    user = request.user
    actions = [
        t for t in services.ARC.available(req, user) if t.action not in services.DECISION_ACTIONS
    ]
    can_decide = (
        req.state == ArcState.IN_REVIEW
        and policy.can(user, "arc.decide")
        and not services.is_recused(user, req)
    )
    can_decide_appeal = (
        req.state == ArcState.APPEAL
        and policy.can(user, "arc.appeal_decide")
        and not services.is_recused(user, req)
    )
    can_attach = (services.owns_lot(user, req) and req.state == ArcState.DRAFT) or (
        policy.can(user, "arc.intake") and req.state not in (ArcState.COMPLETED, ArcState.WITHDRAWN)
    )
    return render(
        request,
        "arc/detail.html",
        {
            "obj": req,
            "actions": actions,
            "history": services.ARC.history(req),
            "action_url_name": "arc_action",
            "decisions": req.decisions.all(),
            "files": attachments.attachments_for(req),
            "can_attach": can_attach,
            "deliberation": services.deliberation(req, user),
            "recused": services.is_recused(user, req) and policy.can(user, "arc.view_all"),
            "can_decide": can_decide,
            "can_decide_appeal": can_decide_appeal,
            "decision_form": DecisionForm(),
            "positions": Position.choices,
            "can_set_affected": policy.can(user, "arc.intake")
            and req.state in (ArcState.SUBMITTED, ArcState.IN_REVIEW),
            "affected": _affected_numbers(req),
        },
    )


@login_required
@require_POST
def arc_action(request, pk, action):
    req = _get(request, pk)
    try:
        services.assert_decision_action_not_generic(action)
        if action not in services.ARC.transitions:
            raise WorkflowError("Unknown action.")
        services.ARC.apply(
            req, action, actor=request.user, note=request.POST.get("note", ""), request=request
        )
    except WorkflowError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"{services.ARC.transitions[action].label}: done.")
    return redirect("arc_detail", pk=req.pk)


@login_required
@require_POST
def arc_decide(request, pk, kind):
    req = _get(request, pk)
    form = DecisionForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Check the decision form.")
        return redirect("arc_detail", pk=req.pk)
    data = form.cleaned_data
    try:
        if kind == "appeal":
            services.decide_appeal(
                req,
                outcome=data["outcome"],
                reasons=data["reasons"],
                citation=data["citation"],
                actor=request.user,
                request=request,
            )
        else:
            services.decide(req, actor=request.user, request=request, **data)
    except (services.ArcError, WorkflowError) as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Decision recorded. The decision letter is ready.")
    return redirect("arc_detail", pk=req.pk)


@login_required
@require_POST
def arc_position(request, pk):
    req = _get(request, pk)
    try:
        services.record_position(
            req,
            position=request.POST.get("position", ""),
            comment=request.POST.get("comment", ""),
            actor=request.user,
            request=request,
        )
    except services.ArcError as exc:
        messages.error(request, str(exc))
    return redirect("arc_detail", pk=req.pk)


@login_required
@require_POST
def arc_attach(request, pk):
    req = _get(request, pk)
    # The owner adds files to their draft; intake adds them while the request
    # is open, and never to a request they are recused from (review A5).
    allowed = (services.owns_lot(request.user, req) and req.state == ArcState.DRAFT) or (
        policy.can(request.user, "arc.intake")
        and req.state in services.AFFECTED_LOTS_STATES
        and not services.is_recused(request.user, req)
    )
    if not allowed or "file" not in request.FILES:
        messages.error(request, "Files can be added to a draft by the owner, or by intake.")
        return redirect("arc_detail", pk=req.pk)
    try:
        attachments.attach(
            req,
            request.FILES["file"],
            actor=request.user,
            label=request.POST.get("label", ""),
            request=request,
        )
    except DocumentError as exc:
        messages.error(request, str(exc))
    return redirect("arc_detail", pk=req.pk)


@login_required
def arc_file(request, pk, file_id):
    return attachments.serve(request, _get(request, pk), file_id)


@login_required
def arc_letter(request, pk, decision_id):
    req = _get(request, pk)
    decision = Decision.objects.filter(request=req, pk=decision_id).first()
    if decision is None:
        raise Http404
    record_event("arc.letter_downloaded", request=request, actor=request.user, target=req)
    return attachments.pdf_response(
        services.decision_letter(req, decision), f"arc-decision-{req.pk}-{decision.pk}.pdf"
    )


@login_required
@require_POST
def arc_affected(request, pk):
    req = _get(request, pk)
    policy.require(request.user, "arc.intake")
    numbers = [
        int(n) for n in request.POST.get("lots", "").replace(",", " ").split() if n.isdigit()
    ]
    # Intake roles name neighbouring lots by number; only the numbers are used.
    lots = list(Lot.objects.unscoped("affected lots by number").filter(number__in=numbers))
    try:
        services.set_affected_lots(req, lots, actor=request.user, request=request)
    except services.ArcError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Affected lots recorded.")
    return redirect("arc_detail", pk=req.pk)


@login_required
def project_types(request):
    """Board: the project types owners choose from."""
    from arc.models import ArcProjectType

    policy.require(request.user, "arc.configure")
    if request.method == "POST":
        existing = None
        if request.POST.get("pk"):
            existing = get_object_or_404(ArcProjectType, pk=request.POST["pk"])
        try:
            services.save_project_type(
                name=request.POST.get("name", ""),
                guidance=request.POST.get("guidance", ""),
                citation=request.POST.get("citation", ""),
                active=request.POST.get("active") == "on",
                sort_order=request.POST.get("sort_order") or 100,
                project_type=existing,
                actor=request.user,
                request=request,
            )
        except (services.ArcError, ValueError) as exc:
            messages.error(
                request,
                str(exc) if isinstance(exc, services.ArcError) else "Check the order number.",
            )
        else:
            messages.success(request, "Project type saved.")
        return redirect("arc_project_types")
    return render(request, "arc/project_types.html", {"types": ArcProjectType.objects.all()})
