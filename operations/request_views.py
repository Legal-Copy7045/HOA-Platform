"""Owner requests: raise, follow, and (board/manager) work them."""

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from comms import threads
from community.models import CommonArea, Person
from community.services import staff_people
from core import policy
from core.lists import paginate
from core.workflow import WorkflowError
from documents import attachments
from operations import services
from operations.models import OPEN_REQUEST_STATES, OwnerRequest, RequestType

# One line each, so owners know which to pick.
TYPE_HELP = {
    RequestType.GENERAL: "A question about the rules, the community or this website.",
    RequestType.BILLING: "Your dues, a charge or a payment on your account.",
    RequestType.MAINTENANCE: "Something that needs fixing, such as a streetlight, sign or drain.",
    RequestType.COMMON_AREA: "A problem in a shared space, such as damage, misuse or a hazard.",
}


def _address(lot) -> str:
    """Owners know their street address, not their lot number."""
    first = (lot.street_address or "").splitlines()[0].strip() if lot.street_address else ""
    return first or str(lot)


class RequestForm(forms.Form):
    lot = forms.ChoiceField(label="Home")
    request_type = forms.ChoiceField(
        choices=RequestType.choices, label="What is it about?", widget=forms.RadioSelect
    )
    subject = forms.CharField(max_length=200)
    description = forms.CharField(
        label="Details", widget=forms.Textarea(attrs={"rows": 6}), max_length=10_000
    )
    common_area = forms.ModelChoiceField(
        queryset=CommonArea.objects.none(), required=False, label="Common area (if any)"
    )

    def __init__(self, *args, lots, **kwargs):
        super().__init__(*args, **kwargs)
        self.lots = {str(lot.pk): lot for lot in lots}
        self.fields["lot"].choices = [(k, _address(v)) for k, v in self.lots.items()]
        self.only_lot = next(iter(self.lots.values())) if len(self.lots) == 1 else None
        if self.only_lot:
            # One home (nearly everyone): no dropdown, the address is shown as text.
            self.fields["lot"].widget = forms.HiddenInput()
            self.fields["lot"].initial = str(self.only_lot.pk)
        self.fields["common_area"].queryset = CommonArea.objects.filter(active=True)
        self.type_help = [
            (value, label, TYPE_HELP.get(value, "")) for value, label in RequestType.choices
        ]


@login_required
def requests_list(request):
    user = request.user
    staff = policy.can(user, "request.manage")
    if not staff and not policy.can(user, "request.view_own"):
        if policy.can(user, "history.view_own"):
            # Former owners read their old requests inside My history.
            return redirect("my_history")
        policy.require(user, "request.view_own")
    qs = OwnerRequest.objects.for_user(user).select_related("lot", "assignee")
    show = request.GET.get("show", "open")
    if show == "open":
        qs = qs.filter(state__in=OPEN_REQUEST_STATES)
    elif show == "closed":
        qs = qs.exclude(state__in=OPEN_REQUEST_STATES)
    from core.scoping import own_records, wants_mine

    mine = wants_mine(request) or not staff
    if mine and staff:
        qs = own_records(qs, user)
    if staff and not mine and request.GET.get("assigned") == "1":
        person = Person.objects.for_user(user).filter(user=user).first()
        qs = qs.filter(assignee=person)
    kind = request.GET.get("type")
    if kind in RequestType.values:
        qs = qs.filter(request_type=kind)
    from core.lists import csv_response, filter_list, wants_csv

    qs, tools = filter_list(request, qs, date_field="created_at")
    tools["has_states"] = False  # the Open/Closed/All tabs above do this
    if wants_csv(request) and staff and not mine:
        return csv_response(
            request,
            "owner-requests",
            ["Number", "Subject", "Type", "Lot", "Assigned", "Respond by", "Answered", "Status"],
            (
                [
                    r.pk,
                    r.subject,
                    r.get_request_type_display(),
                    r.lot.number,
                    r.assignee.display_name if r.assignee else "",
                    r.respond_by or "",
                    timezone.localdate(r.first_response_at) if r.first_response_at else "",
                    r.get_state_display(),
                ]
                for r in qs
            ),
            audit_kind="requests",
        )
    form = None
    if mine and policy.can(user, "request.submit"):
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
                    form.add_error(None, str(exc))
                else:
                    messages.success(request, "Request sent. You'll get an email when we reply.")
                    photos = request.FILES.getlist("photos")
                    if photos and services.can_add_request_photos(user, req):
                        attachments.attach_many(request, req, photos, actor=user)
                    return redirect("request_detail", pk=req.pk)
            # Invalid: shown again below with what was typed and the errors.
    elif request.method == "POST":
        policy.require(user, "request.submit")
        return redirect(reverse("requests") + "?mine=1")
    return render(
        request,
        "operations/requests.html",
        {
            "page": paginate(request, qs),
            "show": show,
            "staff": staff and not mine,
            "is_staff": staff,
            "mine": mine,
            "tools": tools,
            "can_export": staff and not mine,
            "form": form,
            "types": RequestType.choices,
            "type": kind,
            "open_form": bool(form and (form.errors or request.GET.get("new"))),
            # View-only contacts: the account's requests, read-only.
            "read_only": mine and not policy.can(user, "request.submit"),
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
            "photos": _files(req),
            "can_add_photos": services.can_add_request_photos(request.user, req),
            "owner_view": not staff or req.requested_by_id == request.user.pk,
        },
    )


def _files(req):
    from documents.services import scan_allows_download

    files = list(attachments.attachments_for(req))
    for f in files:
        f.thumbnail = f.content_type.startswith("image/") and scan_allows_download(
            attachments.current_scan(f)
        )
    return files


def _scoped(request, pk) -> OwnerRequest:
    return get_object_or_404(OwnerRequest.objects.for_user(request.user), pk=pk)


@login_required
@require_POST
def request_attach(request, pk):
    """Photos on a request: the person who raised it, while it is open, or staff."""
    req = _scoped(request, pk)
    uploads = request.FILES.getlist("photos")
    if not services.can_add_request_photos(request.user, req):
        raise PermissionDenied
    if not uploads:
        messages.error(request, "Choose a photo first.")
    else:
        attachments.attach_many(request, req, uploads, actor=request.user)
    return redirect("request_detail", pk=req.pk)


@login_required
def request_file(request, pk, file_id):
    return attachments.serve(request, _scoped(request, pk), file_id)


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
