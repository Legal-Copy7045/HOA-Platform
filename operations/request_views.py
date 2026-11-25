"""Owner requests: raise, follow, and (board/manager) work them."""

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from comms import threads
from community.models import CommonArea, Person
from community.services import staff_people
from core import policy
from core.lists import paginate
from core.workflow import WorkflowError
from operations import services
from operations.models import OPEN_REQUEST_STATES, OwnerRequest, RequestType


class RequestForm(forms.Form):
    lot = forms.ChoiceField(label="Home")
    request_type = forms.ChoiceField(choices=RequestType.choices, label="Kind of request")
    subject = forms.CharField(max_length=200)
    description = forms.CharField(widget=forms.Textarea(attrs={"rows": 6}), max_length=10_000)
    common_area = forms.ModelChoiceField(
        queryset=CommonArea.objects.none(), required=False, label="Common area (if any)"
    )

    def __init__(self, *args, lots, **kwargs):
        super().__init__(*args, **kwargs)
        self.lots = {str(lot.pk): lot for lot in lots}
        self.fields["lot"].choices = [(k, str(v)) for k, v in self.lots.items()]
        self.fields["common_area"].queryset = CommonArea.objects.filter(active=True)


@login_required
def requests_list(request):
    user = request.user
    staff = policy.can(user, "request.manage")
    if not staff and not policy.can(user, "request.view_own"):
        policy.require(user, "request.view_own")
    qs = OwnerRequest.objects.for_user(user).select_related("lot", "assignee")
    show = request.GET.get("show", "open")
    if show == "open":
        qs = qs.filter(state__in=OPEN_REQUEST_STATES)
    elif show == "closed":
        qs = qs.exclude(state__in=OPEN_REQUEST_STATES)
    if staff and request.GET.get("mine") == "1":
        person = Person.objects.for_user(user).filter(user=user).first()
        qs = qs.filter(assignee=person)
    kind = request.GET.get("type")
    if kind in RequestType.values:
        qs = qs.filter(request_type=kind)
    form = None
    if policy.can(user, "request.submit"):
        lots = services.request_lots(user)
        if lots:
            form = RequestForm(request.POST or None, lots=lots)
            if request.method == "POST" and form.is_valid():
                try:
                    req = services.submit_request(
                        lot=form.lots[form.cleaned_data["lot"]],
                        request_type=form.cleaned_data["request_type"],
                        subject=form.cleaned_data["subject"],
                        description=form.cleaned_data["description"],
                        common_area=form.cleaned_data["common_area"],
                        actor=user,
                        request=request,
                    )
                except services.OperationsError as exc:
                    messages.error(request, str(exc))
                else:
                    messages.success(request, "Request sent. You'll get an email when we reply.")
                    return redirect("request_detail", pk=req.pk)
    elif request.method == "POST":
        policy.require(user, "request.submit")
    return render(
        request,
        "operations/requests.html",
        {
            "page": paginate(request, qs),
            "show": show,
            "staff": staff,
            "form": form,
            "types": RequestType.choices,
            "type": kind,
        },
    )


@login_required
def request_detail(request, pk):
    req = get_object_or_404(
        OwnerRequest.objects.for_user(request.user).select_related("lot", "assignee", "work_order"),
        pk=pk,
    )
    staff = policy.can(request.user, "request.manage")
    acc = threads.access("request", req, request.user)
    return render(
        request,
        "operations/request_detail.html",
        {
            "obj": req,
            "staff": staff,
            "actions": services.OWNER_REQUEST.available(req, request.user),
            "history": services.OWNER_REQUEST.history(req),
            "action_url_name": "request_action",
            "thread_kind": "request",
            "thread": threads.messages_for("request", req, request.user),
            "thread_access": acc,
            "assignees": staff_people("request.manage") if staff else [],
            "can_make_work_order": (
                policy.can(request.user, "workorder.manage")
                and req.work_order_id is None
                and req.request_type in (RequestType.MAINTENANCE, RequestType.COMMON_AREA)
                and req.is_open
            ),
            "can_see_work_order": policy.can(request.user, "workorder.manage"),
        },
    )


@login_required
@require_POST
def request_action(request, pk, action):
    req = get_object_or_404(OwnerRequest.objects.for_user(request.user), pk=pk)
    try:
        if action not in services.OWNER_REQUEST.transitions:
            raise WorkflowError("Unknown action.")
        services.OWNER_REQUEST.apply(
            req, action, actor=request.user, note=request.POST.get("note", ""), request=request
        )
        if action in ("ask_owner", "resolve"):
            threads.notify_owner_side("request", req, exclude=request.user)
        elif action == "reopen":
            threads.notify_staff("request", req, exclude=request.user)
    except WorkflowError as exc:
        messages.error(request, str(exc))
    return redirect("request_detail", pk=req.pk)


@login_required
@require_POST
def request_assign(request, pk):
    policy.require(request.user, "request.manage")
    req = get_object_or_404(OwnerRequest.objects.for_user(request.user), pk=pk)
    person = None
    if request.POST.get("person"):
        person = get_object_or_404(Person.objects.for_user(request.user), pk=request.POST["person"])
    try:
        services.assign_request(req, person, actor=request.user, request=request)
    except services.OperationsError as exc:
        messages.error(request, str(exc))
    return redirect("request_detail", pk=req.pk)


@login_required
@require_POST
def request_work_order(request, pk):
    policy.require(request.user, "workorder.manage")
    req = get_object_or_404(OwnerRequest.objects.for_user(request.user), pk=pk)
    try:
        wo = services.request_to_work_order(req, actor=request.user, request=request)
    except services.OperationsError as exc:
        messages.error(request, str(exc))
        return redirect("request_detail", pk=req.pk)
    messages.success(request, f"Work order WO-{wo.pk} created.")
    return redirect("request_detail", pk=req.pk)
