from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from arc import services
from arc.models import ArcRequest, ArcState, Decision, Position
from arc.signoff_views import asked_of_me_waiting, panel_context
from audit.services import record_event
from community.models import Lot
from core import conflicts, policy
from core.workflow import WorkflowError
from documents import attachments


class _AddressChoice(forms.ModelChoiceField):
    """Owners know their street address, not their lot number."""

    def label_from_instance(self, obj):
        return obj.label


LOCATIONS = [
    ("", "Choose..."),
    ("front", "Front"),
    ("side", "Side"),
    ("rear", "Back (rear)"),
    ("other", "Other"),
]


class ProjectForm(forms.Form):
    """What the owner tells the committee about the project. Kind first, so
    its guidance (drawings, samples...) shows before they start writing."""

    project_type = forms.ChoiceField(label="Kind of project", required=False)
    title = forms.CharField(
        max_length=200,
        label="Short name for the project",
        help_text="For example: Rear fence, 4 ft black aluminum.",
    )
    description = forms.CharField(
        label="What you plan to do",
        widget=forms.Textarea(attrs={"rows": 5}),
        help_text="Sizes, colors and anything the committee should know.",
    )
    location = forms.ChoiceField(required=False, label="Where on your property", choices=LOCATIONS)
    location_detail = forms.CharField(max_length=200, required=False, label="If other, where?")
    planned_start = forms.DateField(
        label="Planned start",
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    planned_completion = forms.DateField(
        required=False,
        label="Approximate completion",
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    materials = forms.CharField(
        required=False,
        label="Materials and contractors",
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="Materials (or shrubs) to be installed or removed, quantities, contractors.",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        types = services.active_project_types()
        # Shown one at a time under the choice (static/js/forms.js); all of
        # them without JavaScript. Escaped like any other text.
        self.type_guidance = [(t.name, t.guidance, t.citation) for t in types if t.guidance]
        if types:
            self.fields["project_type"].required = True
            self.fields["project_type"].choices = [("", "Choose...")] + [
                (t.name, t.name) for t in types
            ]
        else:
            # The board retired every type: still a choice, never free text.
            self.fields["project_type"].choices = [("", "Choose..."), ("Other", "Other")]

    def clean(self):
        data = super().clean()
        if data.get("location") != "other":
            data["location_detail"] = ""
        start, end = data.get("planned_start"), data.get("planned_completion")
        if start and end and end < start:
            self.add_error("planned_completion", "Completion cannot be before the start.")
        return data


class NewRequestForm(ProjectForm):
    lot = _AddressChoice(queryset=Lot.objects.none(), label="Property", empty_label="Choose...")

    field_order = ["lot", *ProjectForm.base_fields]

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        lots = Lot.objects.for_user(user).filter(pk__in=policy.roles_for(user).owned_lot_ids)
        self.fields["lot"].queryset = lots
        self.only_lot = lots.first() if lots.count() == 1 else None
        if self.only_lot:
            # One home (nearly everyone): no dropdown, the address is shown as text.
            self.fields["lot"].widget = forms.HiddenInput()
            self.fields["lot"].initial = self.only_lot.pk


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


def _affected_labels(req) -> list[str]:
    from core.scoping import system_access

    with system_access("affected homes for intake form"):
        return [lot.label for lot in req.affected_lots.all()]


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
                form.add_error(None, str(exc))
            else:
                messages.success(
                    request,
                    f"Request #{req.pk} is saved as a draft. Next, add plans or photos, "
                    "then send it on (the steps are below).",
                )
                return redirect(reverse("arc_detail", args=[req.pk]) + "#steps")
        # Invalid: the page is shown again with what was typed, a summary at
        # the top of the form and the errors beside each field.
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
        open_states=(
            "draft",
            "neighbors",
            "submitted",
            "completeness_check",
            "in_review",
            "appeal",
        ),
    )
    if wants_csv(request) and can_view_all and not mine:
        return csv_response(
            request,
            "arc-requests",
            ["Address", "Number", "Project", "Type", "Status", "Decision due", "Created"],
            (
                [
                    r.lot.label,
                    f"ARC-{r.pk}",
                    r.title,
                    r.project_type,
                    r.get_state_display(),
                    r.review_due_on or "",
                    timezone.localdate(r.created_at),
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
            # Owner side: tabs "Mine" / "Asked of me" (neighbor acknowledgements).
            "arc_tab": "mine" if mine and can_submit else "",
            "asked_count": asked_of_me_waiting(request.user) if mine and can_submit else 0,
        },
    )


# Steps the owner sends from the draft checklist rather than "Next steps".
CHECKLIST_ACTIONS = {"submit", "request_acknowledgements"}

# What the owner (or the board) is told after each step, in plain sentences.
DONE_MESSAGES = {
    "submit": "Sent to the committee. We'll email you when there's a decision.",
    "request_acknowledgements": (
        "Sent to your neighbors. When they have all responded, your request goes to "
        "the committee automatically."
    ),
    "withdraw": "Your request has been withdrawn.",
    "appeal": "Your appeal has been sent to the board.",
    "accept": "Marked complete. The review clock has started.",
    "return_incomplete": "Returned to the owner as incomplete, with your note.",
    "complete": "The project is marked as complete.",
}


def _files(req):
    """Plans and photos, with a thumbnail for images the scanner has cleared."""
    from documents.services import scan_allows_download

    files = list(attachments.attachments_for(req))
    for f in files:
        f.thumbnail = f.content_type.startswith("image/") and scan_allows_download(
            attachments.current_scan(f)
        )
    return files


def _checklist(req, user, actions, files, signoff) -> dict | None:
    """Draft only, for an owner of the lot: describe, add files, neighbors,
    send. The step that sends it is the one the workflow allows now."""
    if not services.can_edit(user, req):
        return None
    send = next((t for t in actions if t.action in CHECKLIST_ACTIONS), None)
    blocked = {
        t.action: reason
        for t, reason in services.ARC.blocked(req, user)
        if t.action in CHECKLIST_ACTIONS
    }
    return {
        "has_files": bool(files),
        "neighbors_waiting": signoff["counts"].get("pending", 0),
        "send": send,
        "blocked_reason": None if send else next(iter(blocked.values()), None),
    }


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
    # Own matters (D65): a recused person (their own home's request, or an
    # affected neighbor's) sees the homeowner's page, without the Board badge.
    own = conflicts.page_context(user, req)
    can_attach = (services.owns_lot(user, req) and req.state == ArcState.DRAFT) or (
        policy.can(user, "arc.intake")
        and not own["own_matter"]
        and req.state not in (ArcState.COMPLETED, ArcState.WITHDRAWN)
    )
    panel = panel_context(user, req)
    files = _files(req)
    checklist = _checklist(req, user, actions, files, panel["signoff"])
    if checklist:
        actions = [t for t in actions if t.action not in CHECKLIST_ACTIONS]
    blocked = [
        (t, reason)
        for t, reason in services.ARC.blocked(req, user)
        if t.action not in services.DECISION_ACTIONS
        and not (checklist and t.action in CHECKLIST_ACTIONS)
    ]
    roles = policy.roles_for(user)
    return render(
        request,
        "arc/detail.html",
        {
            **panel,
            **own,
            "obj": req,
            "actions": actions,
            "blocked_steps": blocked,
            "checklist": checklist,
            "can_edit": services.can_edit(user, req),
            "owner_view": req.lot_id in roles.owned_lot_ids or not policy.can(user, "arc.view_all"),
            "history": services.ARC.history(req),
            "action_url_name": "arc_action",
            "decisions": req.decisions.all(),
            "files": files,
            "revisions": req.revisions.all(),
            "can_attach": can_attach,
            "deliberation": services.deliberation(req, user),
            "recused": services.is_recused(user, req) and policy.can(user, "arc.view_all"),
            "can_decide": can_decide,
            "can_decide_appeal": can_decide_appeal,
            "decision_form": DecisionForm(),
            "positions": Position.choices,
            "can_set_affected": policy.can(user, "arc.intake")
            and not own["own_matter"]
            and req.state in (ArcState.SUBMITTED, ArcState.IN_REVIEW),
            "affected": _affected_numbers(req),
            "affected_labels": _affected_labels(req),
        },
    )


@login_required
def arc_edit(request, pk):
    """The owner corrects their draft (title, description, dates...)."""
    req = _get(request, pk)
    if not services.can_edit(request.user, req):
        raise PermissionDenied
    initial = {f: getattr(req, f) for f in services.EDITABLE_FIELDS}
    form = ProjectForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        try:
            services.edit_request(req, actor=request.user, request=request, **form.cleaned_data)
        except services.ArcError as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(request, "Your changes are saved.")
            return redirect("arc_detail", pk=req.pk)
    return render(request, "arc/edit.html", {"obj": req, "form": form})


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
        messages.success(
            request,
            DONE_MESSAGES.get(action, f"{services.ARC.transitions[action].label}: done."),
        )
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
    by_owner = services.owns_lot(request.user, req) and req.state == ArcState.DRAFT
    allowed = by_owner or (
        policy.can(request.user, "arc.intake")
        and req.state in services.AFFECTED_LOTS_STATES
        and not services.is_recused(request.user, req)
    )
    uploads = request.FILES.getlist("file")
    if not allowed or not uploads:
        messages.error(request, "Files can be added to a draft by the owner, or by intake.")
        return redirect("arc_detail", pk=req.pk)
    label = request.POST.get("label", "")
    if by_owner:
        # Through the service, so the neighbors are asked again (BUG-001).
        try:
            services.add_owner_files(req, uploads, actor=request.user, request=request, label=label)
        except services.ArcError as exc:
            messages.error(request, str(exc))
    else:
        attachments.attach_many(request, req, uploads, actor=request.user, label=label)
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
    # Intake roles name neighboring lots by number; only the numbers are used.
    lots = list(Lot.objects.unscoped("affected lots by number").filter(number__in=numbers))
    try:
        services.set_affected_lots(req, lots, actor=request.user, request=request)
    except services.ArcError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Affected homes recorded.")
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
