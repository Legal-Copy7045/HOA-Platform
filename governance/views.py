from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from core import policy
from core.workflow_views import transition_view
from documents.models import Document
from governance import services
from governance.models import (
    Ballot,
    Meeting,
    MeetingKind,
    MeetingStatus,
    RecordsRequest,
    Resolution,
    ResolutionKind,
    Vote,
    VoteKind,
)


def _flash(request, fn, ok: str):
    try:
        result = fn()
    except services.GovernanceError as exc:
        messages.error(request, str(exc))
        return None
    messages.success(request, ok)
    return result


# --- Meetings -----------------------------------------------------------------------


class MeetingForm(forms.Form):
    title = forms.CharField(max_length=200)
    kind = forms.ChoiceField(choices=MeetingKind.choices)
    starts_at = forms.DateTimeField(widget=forms.DateTimeInput(attrs={"type": "datetime-local"}))
    location = forms.CharField(max_length=200, required=False)
    agenda = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 4}))


@login_required
def meetings(request):
    policy.require(request.user, "meeting.view")
    form = MeetingForm(request.POST or None)
    if request.method == "POST":
        policy.require(request.user, "meeting.manage")
        if form.is_valid():
            m = _flash(
                request,
                lambda: services.create_meeting(
                    actor=request.user, request=request, **form.cleaned_data
                ),
                "Meeting scheduled.",
            )
            if m:
                return redirect("meeting_detail", pk=m.pk)
        return redirect("meetings")
    return render(
        request,
        "governance/meetings.html",
        {
            "meetings": Meeting.objects.for_user(request.user)[:200],
            "form": form,
            "can_manage": policy.can(request.user, "meeting.manage"),
        },
    )


@login_required
def meeting_detail(request, pk):
    meeting = get_object_or_404(Meeting.objects.for_user(request.user), pk=pk)
    can_manage = policy.can(request.user, "meeting.manage")
    minutes_choices = (
        Document.objects.for_user(request.user).filter(category="minutes") if can_manage else []
    )
    return render(
        request,
        "governance/meeting_detail.html",
        {
            "m": meeting,
            "can_manage": can_manage,
            "minutes_choices": minutes_choices,
            "attendance": meeting.attendance.select_related("person") if can_manage else [],
            "short_days": services.notice_shortfall_days(meeting) if can_manage else None,
        },
    )


@login_required
@require_POST
def meeting_action(request, pk, action):
    policy.require(request.user, "meeting.manage")
    meeting = get_object_or_404(Meeting.objects.for_user(request.user), pk=pk)
    if action == "notice":
        result = _flash(
            request,
            lambda: services.send_notice(meeting, actor=request.user, request=request),
            "Notice sent.",
        )
        if result:
            if result["paper"]:
                messages.warning(
                    request,
                    f"{len(result['paper'])} owner(s) have not consented "
                    "to email notice and need a paper notice.",
                )
            if result["short_days"]:
                messages.warning(
                    request,
                    f"Notice is {result['short_days']} day(s) shorter "
                    "than the governing rule requires.",
                )
    elif action in ("held", "cancelled"):
        _flash(
            request,
            lambda: services.set_meeting_status(
                meeting, MeetingStatus(action), actor=request.user, request=request
            ),
            "Updated.",
        )
    elif action == "minutes":
        doc = get_object_or_404(
            Document.objects.for_user(request.user), pk=request.POST.get("document")
        )
        services.attach_minutes(meeting, doc, actor=request.user, request=request)
        messages.success(request, "Minutes attached.")
    return redirect("meeting_detail", pk=meeting.pk)


# --- Resolutions and policies ----------------------------------------------------------


class ResolutionForm(forms.Form):
    kind = forms.ChoiceField(choices=ResolutionKind.choices)
    title = forms.CharField(max_length=200)
    text = forms.CharField(widget=forms.Textarea(attrs={"rows": 5}))
    adopted_on = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    votes_for = forms.IntegerField(min_value=0, initial=0)
    votes_against = forms.IntegerField(min_value=0, initial=0)
    abstentions = forms.IntegerField(min_value=0, initial=0)
    supersedes = forms.ModelChoiceField(queryset=Resolution.objects.none(), required=False)
    citation = forms.CharField(max_length=200, required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["supersedes"].queryset = services.in_force()


@login_required
def resolutions(request):
    policy.require(request.user, "resolution.view")
    form = ResolutionForm(request.POST or None)
    if request.method == "POST":
        policy.require(request.user, "resolution.record")
        if form.is_valid():
            _flash(
                request,
                lambda: services.record_resolution(
                    actor=request.user, request=request, **form.cleaned_data
                ),
                "Recorded.",
            )
        else:
            messages.error(request, "Check the form.")
        return redirect("resolutions")
    show_all = request.GET.get("all") == "1"
    rows = Resolution.objects.all() if show_all else services.in_force()
    return render(
        request,
        "governance/resolutions.html",
        {
            "rows": rows.select_related("supersedes"),
            "show_all": show_all,
            "form": form,
            "can_record": policy.can(request.user, "resolution.record"),
        },
    )


# --- Records requests -------------------------------------------------------------------


@login_required
def records_requests(request):
    can_manage = policy.can(request.user, "records_request.manage")
    can_submit = policy.can(request.user, "records_request.submit")
    if not (can_manage or can_submit):
        policy.require(request.user, "records_request.manage")
    if request.method == "POST":
        policy.require(request.user, "records_request.submit")
        obj = _flash(
            request,
            lambda: services.submit_records_request(
                description=request.POST.get("description", ""), actor=request.user, request=request
            ),
            "Request submitted.",
        )
        return (
            redirect("records_request_detail", pk=obj.pk) if obj else redirect("records_requests")
        )
    rows = RecordsRequest.objects.for_user(request.user).select_related("requester")
    return render(
        request,
        "governance/records_requests.html",
        {"rows": rows, "can_submit": can_submit, "can_manage": can_manage},
    )


@login_required
def records_request_detail(request, pk):
    obj = get_object_or_404(RecordsRequest.objects.for_user(request.user), pk=pk)
    return render(
        request,
        "governance/records_request_detail.html",
        {
            "obj": obj,
            "actions": services.RECORDS.available(obj, request.user),
            "history": services.RECORDS.history(obj),
            "action_url_name": "records_request_action",
        },
    )


records_request_action = transition_view(
    model=RecordsRequest, workflow=services.RECORDS, detail_url_name="records_request_detail"
)


# --- Votes ---------------------------------------------------------------------------------


class VoteForm(forms.Form):
    title = forms.CharField(max_length=200)
    kind = forms.ChoiceField(choices=VoteKind.choices)
    description = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 3}))
    options = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="One option per line, e.g. Yes / No / Abstain.",
    )
    secret = forms.BooleanField(required=False)
    closes_at = forms.DateTimeField(
        required=False, widget=forms.DateTimeInput(attrs={"type": "datetime-local"})
    )


@login_required
def votes(request):
    policy.require(request.user, "vote.view")
    form = VoteForm(request.POST or None)
    if request.method == "POST":
        policy.require(request.user, "vote.manage")
        if form.is_valid():
            data = dict(form.cleaned_data)
            data["options"] = data["options"].splitlines()
            vote = _flash(
                request,
                lambda: services.create_vote(actor=request.user, request=request, **data),
                "Vote created as a draft.",
            )
            if vote:
                return redirect("vote_detail", pk=vote.pk)
        return redirect("votes")
    rows = Vote.objects.all()
    if not policy.can(request.user, "vote.manage"):
        rows = rows.exclude(status="draft")
    return render(
        request,
        "governance/votes.html",
        {"rows": rows, "form": form, "can_manage": policy.can(request.user, "vote.manage")},
    )


@login_required
def vote_detail(request, pk):
    policy.require(request.user, "vote.view")
    vote = get_object_or_404(Vote, pk=pk)
    can_manage = policy.can(request.user, "vote.manage")
    if vote.status == "draft" and not can_manage:
        raise Http404  # drafts are invisible, not merely refused
    show_results = vote.status == "closed" or can_manage
    voters = []
    if services.can_see_voters(vote, request.user):
        voters = Ballot.objects.filter(vote=vote).select_related("person", "lot", "option")
    return render(
        request,
        "governance/vote_detail.html",
        {
            "vote": vote,
            "choices": services.ballot_choices_for(vote, request.user),
            "results": services.results(vote) if show_results else None,
            "can_manage": can_manage,
            "voters": voters,
        },
    )


@login_required
@require_POST
def vote_action(request, pk, action):
    vote = get_object_or_404(Vote, pk=pk)
    if action == "cast":
        lot = request.POST.get("lot")
        _flash(
            request,
            lambda: services.cast_ballot(
                vote,
                option_id=int(request.POST.get("option") or 0),
                lot_id=int(lot) if lot else None,
                actor=request.user,
                request=request,
            ),
            "Ballot recorded. Ballots cannot be changed.",
        )
    elif action == "open":
        _flash(
            request,
            lambda: services.open_vote(vote, actor=request.user, request=request),
            "Vote opened.",
        )
    elif action == "close":
        policy.require(request.user, "vote.manage")
        _flash(
            request,
            lambda: services.close_vote(vote, actor=request.user, request=request),
            "Vote closed.",
        )
    return redirect("vote_detail", pk=vote.pk)
