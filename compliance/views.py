from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from audit.services import record_event
from community.models import Lot
from compliance import services
from compliance.models import Violation, ViolationCategory
from core import conflicts, policy
from core.workflow import WorkflowError
from documents import attachments


class ReportForm(forms.Form):
    lot = forms.ModelChoiceField(queryset=Lot.objects.none(), label="Home")
    category = forms.ChoiceField(choices=ViolationCategory.choices)
    description = forms.CharField(widget=forms.Textarea(attrs={"rows": 4}))
    observed_on = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["lot"].queryset = Lot.objects.for_user(user).order_by(
            "street_address", "number"
        )


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
                form.add_error(None, str(exc))
            else:
                messages.success(request, "Violation recorded. Send the notice when ready.")
                return redirect("violation_detail", pk=v.pk)
        # Invalid: the page is shown again with what was typed and the errors.
    from core.scoping import own_records, wants_mine

    staff = policy.can(request.user, "violation.view_all")
    mine = wants_mine(request) or not staff
    from core.lists import csv_response, filter_list, paginate, wants_csv

    rows = Violation.objects.for_user(request.user).select_related("lot")
    if mine and staff:
        rows = own_records(rows, request.user)
    rows, tools = filter_list(
        request,
        rows,
        date_field="observed_on",
        open_states=("open", "noticed", "responded", "hearing"),
    )
    if wants_csv(request) and staff and not mine:
        return csv_response(
            request,
            "violations",
            ["Address", "Number", "Category", "Status", "Observed", "Cure by", "Follow up"],
            (
                [
                    v.lot.label,
                    f"V-{v.pk}",
                    v.get_category_display(),
                    v.get_state_display(),
                    v.observed_on,
                    v.cure_by or "",
                    v.follow_up_on or "",
                ]
                for v in rows
            ),
            audit_kind="violations",
        )
    page = paginate(request, rows)
    rows = page.object_list
    return render(
        request,
        "compliance/list.html",
        {
            "rows": rows,
            "form": form,
            "can_manage": can_manage and not mine,
            "mine": mine,
            "staff": staff,
            "page": page,
            "tools": tools,
            "can_export": staff and not mine,
        },
    )


@login_required
def violation_detail(request, pk):
    v = _get(request, pk)
    actions = [t for t in services.VIOLATION.available(v, request.user) if t.action != "respond"]
    # Own matters (D65): a notice about one's own home reads as the owner's.
    own = conflicts.page_context(request.user, v, services.VIOLATION)
    return render(
        request,
        "compliance/detail.html",
        {
            **own,
            "obj": v,
            "actions": actions,
            "history": services.VIOLATION.history(v),
            "action_url_name": "violation_action",
            # Owners see only their own written responses, never an earlier
            # owner's (review A4); the board sees them all.
            "responses": v.responses.all()
            if policy.can(request.user, "lot.view_all")
            else v.responses.filter(person_id=policy.roles_for(request.user).person_id),
            "photos": _photos(v),
            "can_respond": any(
                t.action == "respond" for t in services.VIOLATION.available(v, request.user)
            ),
            "can_manage": policy.can(request.user, "violation.manage") and not own["own_matter"],
            "can_add_fix_photo": services.owner_may_add_photo(request.user, v),
            "owner_view": own["own_matter"]
            or v.lot_id in policy.roles_for(request.user).account_lot_ids
            or not policy.can(request.user, "violation.view_all"),
        },
    )


def _photos(v):
    from documents.services import scan_allows_download

    files = list(attachments.attachments_for(v))
    for f in files:
        f.thumbnail = f.content_type.startswith("image/") and scan_allows_download(
            attachments.current_scan(f)
        )
    return files


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


OWNER_FIX_LABEL = "Owner photo of the fix"
OWNER_FIX_PHOTOS_MAX = 10


def _owner_photo_room(v) -> int:
    """Photos the owner may still add (a first, unlocked look for the message;
    attach() enforces the allowance under the lock)."""
    used = attachments.attachments_for(v).filter(label=OWNER_FIX_LABEL).count()
    return max(OWNER_FIX_PHOTOS_MAX - used, 0)


@login_required
@require_POST
def violation_photo(request, pk):
    """Board or manager: evidence photos. An owner of the lot: photos of the
    fix, while the notice is open."""
    v = _get(request, pk)
    owner = services.owner_may_add_photo(request.user, v)
    # Own matters (D65): on one's own home's notice only the owner's path.
    own = conflicts.own_matter(request.user, v)
    if not owner:
        policy.require(request.user, "violation.manage")
        conflicts.refuse(request.user, v, area="violation", action="photo", request=request)
    uploads = request.FILES.getlist("file")
    if not uploads:
        messages.error(request, "Choose a photo first.")
    else:
        by_owner = owner and (own or not policy.can(request.user, "violation.manage"))
        label = OWNER_FIX_LABEL if by_owner else request.POST.get("label", "")
        room = None
        if by_owner:
            # Owners have their own allowance, so their photos can never use
            # up the board's room for evidence (review R2-003).
            room = _owner_photo_room(v)
        if room == 0:
            messages.error(
                request,
                f"You've added {OWNER_FIX_PHOTOS_MAX} photos already. Send the board a message"
                " if there's more to show.",
            )
        else:
            # The allowance is enforced again under the upload lock, so
            # parallel uploads cannot pass it (review R2-006).
            attachments.attach_many(
                request,
                v,
                uploads[:room] if room else uploads,
                actor=request.user,
                label=label,
                label_max=OWNER_FIX_PHOTOS_MAX if by_owner else None,
            )
    return redirect("violation_detail", pk=v.pk)


@login_required
def violation_file(request, pk, file_id):
    return attachments.serve(request, _get(request, pk), file_id)


@login_required
def violation_letter(request, pk):
    v = _get(request, pk)
    record_event("violation.letter_downloaded", request=request, actor=request.user, target=v)
    return attachments.pdf_response(services.notice_letter(v), f"violation-notice-{v.pk}.pdf")
