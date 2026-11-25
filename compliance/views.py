from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from audit.services import record_event
from community.models import Lot
from compliance import services
from compliance.models import Violation, ViolationCategory
from core import policy
from core.workflow import WorkflowError
from documents import attachments
from documents.services import DocumentError


class ReportForm(forms.Form):
    lot = forms.ModelChoiceField(queryset=Lot.objects.none())
    category = forms.ChoiceField(choices=ViolationCategory.choices)
    description = forms.CharField(widget=forms.Textarea(attrs={"rows": 4}))
    observed_on = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["lot"].queryset = Lot.objects.for_user(user)


def _get(request, pk):
    return get_object_or_404(Violation.objects.for_user(request.user), pk=pk)


@login_required
def violations(request):
    can_manage = policy.can(request.user, "violation.manage")
    if not (
        can_manage
        or policy.can(request.user, "violation.view_all")
        or policy.can(request.user, "lot.view_own")
    ):
        policy.require(request.user, "violation.view_all")
    form = ReportForm(request.POST or None, user=request.user)
    if request.method == "POST":
        policy.require(request.user, "violation.manage")
        if form.is_valid():
            try:
                v = services.report(actor=request.user, request=request, **form.cleaned_data)
            except services.ComplianceError as exc:
                messages.error(request, str(exc))
            else:
                return redirect("violation_detail", pk=v.pk)
        else:
            messages.error(request, "Check the form.")
        return redirect("violations")
    from core.scoping import own_records, wants_mine

    staff = policy.can(request.user, "violation.view_all")
    mine = wants_mine(request) or not staff
    rows = Violation.objects.for_user(request.user).select_related("lot")
    if mine and staff:
        rows = own_records(rows, request.user)
    return render(
        request,
        "compliance/list.html",
        {
            "rows": rows,
            "form": form,
            "can_manage": can_manage and not mine,
            "mine": mine,
            "staff": staff,
        },
    )


@login_required
def violation_detail(request, pk):
    v = _get(request, pk)
    actions = [t for t in services.VIOLATION.available(v, request.user) if t.action != "respond"]
    return render(
        request,
        "compliance/detail.html",
        {
            "obj": v,
            "actions": actions,
            "history": services.VIOLATION.history(v),
            "action_url_name": "violation_action",
            # Owners see only their own written responses, never an earlier
            # owner's (review A4); the board sees them all.
            "responses": v.responses.all()
            if policy.can(request.user, "lot.view_all")
            else v.responses.filter(person_id=policy.roles_for(request.user).person_id),
            "photos": attachments.attachments_for(v),
            "can_respond": any(
                t.action == "respond" for t in services.VIOLATION.available(v, request.user)
            ),
            "can_manage": policy.can(request.user, "violation.manage"),
        },
    )


@login_required
@require_POST
def violation_action(request, pk, action):
    v = _get(request, pk)
    if action == "respond" or action not in services.VIOLATION.transitions:
        messages.error(request, "Use the response form.")
        return redirect("violation_detail", pk=v.pk)
    try:
        services.VIOLATION.apply(
            v, action, actor=request.user, note=request.POST.get("note", ""), request=request
        )
    except WorkflowError as exc:
        messages.error(request, str(exc))
    return redirect("violation_detail", pk=v.pk)


@login_required
@require_POST
def violation_respond(request, pk):
    v = _get(request, pk)
    try:
        services.respond(v, text=request.POST.get("text", ""), actor=request.user, request=request)
    except (services.ComplianceError, WorkflowError) as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Your response was sent to the board.")
    return redirect("violation_detail", pk=v.pk)


@login_required
@require_POST
def violation_photo(request, pk):
    v = _get(request, pk)
    policy.require(request.user, "violation.manage")
    try:
        attachments.attach(
            v,
            request.FILES["file"],
            actor=request.user,
            request=request,
            label=request.POST.get("label", ""),
        )
    except (DocumentError, KeyError) as exc:
        messages.error(request, str(exc) or "Choose a photo.")
    return redirect("violation_detail", pk=v.pk)


@login_required
def violation_file(request, pk, file_id):
    return attachments.serve(request, _get(request, pk), file_id)


@login_required
def violation_letter(request, pk):
    v = _get(request, pk)
    record_event("violation.letter_downloaded", request=request, actor=request.user, target=v)
    return attachments.pdf_response(services.notice_letter(v), f"violation-notice-{v.pk}.pdf")
