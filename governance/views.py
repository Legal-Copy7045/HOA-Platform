from allauth.account.decorators import reauthentication_required
from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
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
                "Meeting scheduled."
                if form.cleaned_data["kind"] == MeetingKind.EXECUTIVE
                else "Meeting scheduled and added to the calendar.",
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


class ApproveMinutesForm(forms.Form):
    approved_on = forms.DateField(
        label="Approved on",
        widget=forms.DateInput(attrs={"type": "date"}),
        help_text="The day the board approved them, usually at its next meeting.",
    )
    note = forms.CharField(
        max_length=200,
        required=False,
        label="Note",
        help_text="Optional, e.g. which meeting approved them or with what corrections.",
    )
    # The version shown on the page: the one approved (fixres BUG-006).
    version = forms.IntegerField(widget=forms.HiddenInput)

    def __init__(self, *args, version=None, **kwargs):
        kwargs.setdefault("auto_id", "approve_%s")
        super().__init__(*args, **kwargs)
        self.fields["approved_on"].initial = timezone.localdate()
        if version is not None:
            self.fields["version"].initial = version.pk


class MinutesUploadForm(forms.Form):
    file = forms.FileField(label="Minutes file", help_text="A PDF or Word file.")
    title = forms.CharField(
        max_length=200,
        required=False,
        help_text="Leave empty to name it after the meeting and its date.",
    )

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("auto_id", "minutes_%s")
        super().__init__(*args, **kwargs)


def _linked(request, meeting) -> dict:
    """Votes, elections and resolutions that belong to this meeting."""
    from core import features
    from governance.models import Election

    user = request.user
    out = {"linked_votes": [], "linked_elections": [], "linked_resolutions": []}
    if policy.can(user, "vote.view"):
        if features.enabled("votes", request):
            rows = Vote.objects.filter(meeting=meeting)
            if not policy.can(user, "vote.manage"):
                rows = rows.exclude(status="draft")
            out["linked_votes"] = list(rows)
        if features.enabled("elections", request):
            out["linked_elections"] = list(Election.objects.filter(meeting=meeting))
    if policy.can(user, "resolution.view"):
        out["linked_resolutions"] = list(Resolution.objects.filter(meeting=meeting))
    return out


def _for_version(form, version):
    """A refused approval form, offering the version now current (the page
    names it), with what was typed kept (fixres BUG-006)."""
    if form is None or version is None or not form.is_bound:
        return form
    if str(form.data.get("version")) != str(version.pk):
        form.data = form.data.copy()
        form.data["version"] = str(version.pk)
    return form


def _render_meeting(request, meeting, *, minutes_form=None, approve_form=None, status=200):
    user = request.user
    can_manage = policy.can(user, "meeting.manage")
    approval = services.minutes_approval(meeting)
    can_approve = (
        policy.can(user, "minutes.adopt")
        and meeting.minutes_document_id
        and meeting.status == MeetingStatus.HELD
        and approval is None
    )
    minutes_version = None
    if can_approve:
        doc = (
            Document.objects.for_user(user)
            .select_related("current_version")
            .filter(pk=meeting.minutes_document_id)
            .first()
        )
        minutes_version = doc.current_version if doc else None
    can_upload = can_manage and policy.can(user, "document.manage")
    minutes_choices = (
        Document.objects.for_user(user).filter(category="minutes") if can_manage else []
    )
    deadlines, deadline_default = (
        services.notice_deadlines(meeting)
        if can_manage and policy.can(user, "compliance.manage")
        else ([], None)
    )
    return render(
        request,
        "governance/meeting_detail.html",
        {
            "m": meeting,
            "can_manage": can_manage,
            "minutes_choices": minutes_choices,
            "minutes_form": minutes_form
            or (MinutesUploadForm() if can_upload and not meeting.minutes_document_id else None),
            "minutes_approval": approval,
            "approve_form": (
                _for_version(approve_form, minutes_version)
                or ApproveMinutesForm(version=minutes_version)
            )
            if can_approve
            else None,
            "minutes_version": minutes_version,
            "deadlines": deadlines,
            "deadline_default": deadline_default,
            "can_record_resolution": policy.can(user, "resolution.record"),
            "attendance": meeting.attendance.select_related("person") if can_manage else [],
            "short_days": services.notice_shortfall_days(meeting) if can_manage else None,
            "packets": visible_packets(user, meeting),
            "can_build_packet": policy.can(user, "meeting.packet_build"),
            **_linked(request, meeting),
            **meeting_extras(user, meeting),
        },
        status=status,
    )


@login_required
def meeting_detail(request, pk):
    meeting = get_object_or_404(Meeting.objects.for_user(request.user), pk=pk)
    return _render_meeting(request, meeting)


@login_required
@require_POST
def meeting_action(request, pk, action):
    policy.require(request.user, "meeting.manage")
    meeting = get_object_or_404(Meeting.objects.for_user(request.user), pk=pk)
    if action == "notice":
        deadline_id = _form_id(request.POST.get("deadline"))
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
            if deadline_id:
                _flash(
                    request,
                    lambda: services.complete_notice_deadline(
                        meeting, deadline_id, actor=request.user, request=request
                    ),
                    "The deadline is marked done on Board tasks.",
                )
    elif action in ("held", "cancelled"):
        _flash(
            request,
            lambda: services.set_meeting_status(
                meeting, MeetingStatus(action), actor=request.user, request=request
            ),
            "Meeting cancelled and taken off the calendar."
            if action == "cancelled" and meeting.calendar_event_id
            else "Updated.",
        )
    elif action == "minutes" and request.POST.get("how") == "upload":
        form = MinutesUploadForm(request.POST, request.FILES)
        if form.is_valid() and _saved(
            request,
            form,
            lambda: services.upload_minutes(
                meeting,
                file=form.cleaned_data["file"],
                title=form.cleaned_data["title"],
                actor=request.user,
                request=request,
            ),
            "Minutes uploaded and attached.",
        ):
            return redirect("meeting_detail", pk=meeting.pk)
        return _render_meeting(request, meeting, minutes_form=form)
    elif action == "approve_minutes":
        policy.require(request.user, "minutes.adopt")
        form = ApproveMinutesForm(request.POST)
        if form.is_valid() and _saved(
            request,
            form,
            lambda: services.approve_minutes(
                meeting,
                approved_on=form.cleaned_data["approved_on"],
                note=form.cleaned_data["note"],
                version_id=form.cleaned_data["version"],
                actor=request.user,
                request=request,
            ),
            "Minutes marked approved. Owner packets now include them as approved minutes.",
        ):
            return redirect("meeting_detail", pk=meeting.pk)
        return _render_meeting(request, meeting, approve_form=form)
    elif action == "minutes":
        doc = get_object_or_404(
            Document.objects.for_user(request.user), pk=_form_id(request.POST.get("document"))
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
    meeting = forms.ModelChoiceField(
        label="Adopted at meeting",
        queryset=Meeting.objects.none(),
        required=False,
        empty_label="(none)",
    )
    votes_for = forms.IntegerField(min_value=0, initial=0)
    votes_against = forms.IntegerField(min_value=0, initial=0)
    abstentions = forms.IntegerField(min_value=0, initial=0)
    supersedes = forms.ModelChoiceField(queryset=Resolution.objects.none(), required=False)
    citation = forms.CharField(max_length=200, required=False)

    def __init__(self, *args, user, **kwargs):
        kwargs.setdefault("auto_id", "res_%s")
        super().__init__(*args, **kwargs)
        self.fields["supersedes"].queryset = services.in_force()
        self.fields["meeting"].queryset = Meeting.objects.for_user(user).exclude(
            status=MeetingStatus.CANCELLED
        )
        self.fields["meeting"].label_from_instance = lambda m: (
            f"{m.title} · {timezone.localtime(m.starts_at):%b %-d, %Y}"
        )


class ConsentForm(forms.Form):
    title = forms.CharField(max_length=200)
    text = forms.CharField(label="Resolution text", widget=forms.Textarea(attrs={"rows": 6}))
    citation = forms.CharField(
        label="Authority",
        max_length=200,
        required=False,
        help_text="The section that allows action without a meeting, for example Bylaws §4.6.",
    )

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("auto_id", "consent_%s")
        super().__init__(*args, **kwargs)


def _consents_on(request) -> bool:
    from core import features

    return features.enabled("consents", request) and policy.can(request.user, "resolution.record")


def render_resolutions(request, *, form=None, consent_form=None, open_panel=""):
    """Resolutions and policies, with (for the board) the written consents
    waiting for signatures and the form to adopt one by written consent.
    Consent signatures stay their own records: an adopted consent is recorded
    as a resolution by the consent service, never typed in here."""
    from governance.models import ConsentRequest, ConsentState

    user = request.user
    show_all = request.GET.get("all") == "1"
    rows = Resolution.objects.all() if show_all else services.in_force()
    can_record = policy.can(user, "resolution.record")
    consents_on = _consents_on(request)
    initial = {}
    meeting_id = _form_id(request.GET.get("meeting"))
    if can_record and meeting_id:
        meeting = Meeting.objects.for_user(user).filter(pk=meeting_id).first()
        if meeting is not None:
            initial = {"meeting": meeting.pk, "adopted_on": timezone.localdate(meeting.starts_at)}
            open_panel = open_panel or "record"
    if form is None:
        form = ResolutionForm(initial=initial, user=user)
    waiting, past_consents = [], []
    if consents_on:
        consents = list(ConsentRequest.objects.all()[:100])
        waiting = [c for c in consents if c.state == ConsentState.OPEN]
        past_consents = [c for c in consents if c.state != ConsentState.OPEN]
    return render(
        request,
        "governance/resolutions.html",
        {
            "rows": rows.select_related("supersedes", "meeting"),
            "show_all": show_all,
            "form": form,
            "can_record": can_record,
            "consents_on": consents_on,
            "waiting": waiting,
            "past_consents": past_consents,
            "consent_form": consent_form or (ConsentForm() if consents_on else None),
            "open_panel": open_panel or request.GET.get("new", ""),
        },
    )


@login_required
def resolutions(request):
    policy.require(request.user, "resolution.view")
    if request.method != "POST":
        return render_resolutions(request)
    policy.require(request.user, "resolution.record")
    form = ResolutionForm(request.POST, user=request.user)
    if form.is_valid() and _saved(
        request,
        form,
        lambda: services.record_resolution(
            actor=request.user, request=request, **form.cleaned_data
        ),
        "Recorded.",
    ):
        meeting = form.cleaned_data.get("meeting")
        if meeting is not None:
            return redirect("meeting_detail", pk=meeting.pk)
        return redirect("resolutions")
    return render_resolutions(request, form=form, open_panel="record")


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
            # Documents' "Ask to inspect it" opens the form (WT-017).
            "open_form": request.GET.get("new") == "1",
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


# --- Votes ---------------------------------------------------------------------------------


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
