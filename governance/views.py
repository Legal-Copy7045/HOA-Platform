from allauth.account.decorators import reauthentication_required
from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from core import policy
from core.dashboard import proxy_meetings
from core.workflow_views import transition_view
from documents.models import Document
from governance import services
from governance.election_views import meeting_extras
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
from governance.packets import visible_packets


def _form_id(value) -> int:
    """A record id from a form field; anything else (letters, a huge number)
    is 0, which the service refuses with a message, never a 500 (BUG-007)."""
    value = (value or "").strip()
    return int(value) if value.isascii() and value.isdigit() and len(value) <= 9 else 0


def _flash(request, fn, ok: str):
    try:
        result = fn()
    except services.GovernanceError as exc:
        messages.error(request, str(exc))
        return None
    messages.success(request, ok)
    return result


def _saved(request, form, fn, ok: str):
    """Like _flash, but a refusal is shown on the form, which the page shows
    again with everything that was typed (nothing is thrown away)."""
    try:
        result = fn()
    except services.GovernanceError as exc:
        form.add_error(None, str(exc))
        return None
    messages.success(request, ok)
    return result or True


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
            m = _saved(
                request,
                form,
                lambda: services.create_meeting(
                    actor=request.user, request=request, **form.cleaned_data
                ),
                "Meeting scheduled.",
            )
            if m:
                return redirect("meeting_detail", pk=m.pk)
        # Invalid: show the form again with what was typed and the errors.
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
            "packets": visible_packets(request.user, meeting),
            "can_build_packet": policy.can(request.user, "meeting.packet_build"),
            **meeting_extras(request.user, meeting),
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
        _flash(
            request,
            lambda: services.attach_minutes(meeting, doc, actor=request.user, request=request),
            "Minutes attached.",
        )
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
        if form.is_valid() and _saved(
            request,
            form,
            lambda: services.record_resolution(
                actor=request.user, request=request, **form.cleaned_data
            ),
            "Recorded.",
        ):
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
    from core.scoping import own_records, wants_mine

    rows = RecordsRequest.objects.for_user(request.user).select_related("requester")
    mine = wants_mine(request) or not can_manage
    if mine and can_manage:
        rows = own_records(rows, request.user)
    return render(
        request,
        "governance/records_requests.html",
        {
            "rows": rows,
            "can_submit": can_submit,
            "can_manage": can_manage and not mine,
            "mine": mine,
        },
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


# --- Voting (votes and elections on one page) -------------------------------------------


@login_required
def voting(request):
    """Owners' one "Voting" page: open and past votes and polls, and board
    elections. Each half follows its own feature switch."""
    from core import features
    from governance import elections as election_services
    from governance.models import Election

    policy.require(request.user, "vote.view")
    show_votes = features.enabled("votes", request)
    show_elections = features.enabled("elections", request)
    if not (show_votes or show_elections):
        raise Http404
    user = request.user
    can_manage = policy.can(user, "vote.manage")
    vote_rows, election_rows = [], []
    if show_votes:
        rows = Vote.objects.all()
        if not can_manage:
            rows = rows.exclude(status="draft")
        for v in sorted(rows[:50], key=lambda v: v.status != "open"):
            vote_rows.append({"vote": v, "voted": services.my_ballots(v, user)})
    if show_elections:
        for e in Election.objects.all()[:20]:
            election_rows.append(
                {
                    "election": e,
                    "can_vote": bool(election_services.lots_actor_may_vote_for(e, user)),
                    # Only ballots cast since this person owns the lot (R2-010).
                    "voted": bool(election_services.my_lots_participation(e, user)[0]),
                }
            )
    return render(
        request,
        "governance/voting.html",
        {
            "vote_rows": vote_rows,
            "election_rows": election_rows,
            "show_votes": show_votes,
            "show_elections": show_elections,
        },
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
            vote = _saved(
                request,
                form,
                lambda: services.create_vote(actor=request.user, request=request, **data),
                "Vote created as a draft.",
            )
            if vote:
                return redirect("vote_detail", pk=vote.pk)
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
            "my_ballots": services.my_ballots(vote, request.user),
            "proxy_meetings": proxy_meetings(request.user),
            "results": services.results(vote) if show_results else None,
            "can_manage": can_manage,
            "voters": voters,
        },
    )


def _casting_formal(request, *args, **kwargs) -> bool:
    """Casting a formal vote needs a recent password or passkey (D24)."""
    if kwargs.get("action") != "cast":
        return False
    vote = Vote.objects.filter(pk=kwargs.get("pk")).only("kind").first()
    return vote is not None and vote.kind == VoteKind.FORMAL


@login_required
def ballot_link(request, token):
    from governance import ballot_links

    target = ballot_links.resolve(token, request.user)
    if target is None:
        return render(request, "governance/ballot_link_invalid.html", status=404)
    return redirect(target)


@login_required
@require_POST
@reauthentication_required(enabled=lambda r: _casting_formal(r, **r.resolver_match.kwargs))
def vote_action(request, pk, action):
    vote = get_object_or_404(Vote, pk=pk)
    if action == "cast":
        lot = request.POST.get("lot")
        _flash(
            request,
            lambda: services.cast_ballot(
                vote,
                option_id=_form_id(request.POST.get("option")),
                lot_id=_form_id(lot) if lot else None,
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
