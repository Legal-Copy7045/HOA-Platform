"""The one Voting page: formal votes, board elections and surveys in a single
list with filters (open or past, and type), the board's "New ..." forms, and
"Give a proxy" for coming owners' meetings.

The old list pages (``votes``, ``elections``, ``surveys``) redirect here with
their type preselected; they still take the create forms' POSTs, and a
refused form comes back on this page with what was typed. Each kind keeps its
own records and rules (formal votes are binding, surveys are not; election
ballots stay unlinkable); only the list is shared.
"""

from __future__ import annotations

import datetime

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone

from core import features, policy
from core.dates import short_date
from governance import elections as election_services
from governance import services, surveys, window
from governance.models import (
    Election,
    ElectionState,
    Meeting,
    MeetingKind,
    MeetingStatus,
    Proxy,
    Vote,
    VoteKind,
    VoteStatus,
)

TYPES = ("votes", "elections", "surveys")
_OWNER_MEETINGS = (MeetingKind.ANNUAL, MeetingKind.SPECIAL)


def _coming_owner_meetings(user):
    return Meeting.objects.for_user(user).filter(
        kind__in=_OWNER_MEETINGS, status=MeetingStatus.SCHEDULED, starts_at__gte=timezone.now()
    )


class _MeetingChoice(forms.ModelChoiceField):
    def label_from_instance(self, m):
        return f"{m.title} · {timezone.localtime(m.starts_at):%b %-d, %Y}"


_LOCAL = "%Y-%m-%dT%H:%M"


def _when_widget():
    return forms.DateTimeInput(attrs={"type": "datetime-local"}, format=_LOCAL)


class WindowForm(forms.Form):
    """The voting window (D61): both times are required; the service checks
    the order and the shortest voting period (rule vote.min_open_days)."""

    opens_at = forms.DateTimeField(
        label="Voting opens",
        widget=_when_widget(),
        help_text="Now or later. Ballots are taken from this time.",
    )
    closes_at = forms.DateTimeField(
        label="Voting closes",
        widget=_when_widget(),
        help_text="Voting closes by itself at this time.",
    )

    def __init__(self, *args, **kwargs):
        from governance import window

        kwargs.setdefault("auto_id", "window_%s")
        super().__init__(*args, **kwargs)
        days = window.min_open_days()
        self.fields["closes_at"].help_text = (
            f"At least {days} day{'s' if days != 1 else ''} after it opens. "
            "Voting closes by itself at this time."
        )

    def clean(self):
        data = super().clean()
        opens_at, closes_at = data.get("opens_at"), data.get("closes_at")
        if opens_at and closes_at and closes_at <= opens_at:
            self.add_error("closes_at", "Voting must close after it opens.")
        return data

    @classmethod
    def initial_for(cls, opens_at=None, closes_at=None):
        from governance import window

        now = timezone.localtime().replace(second=0, microsecond=0)
        opens_at = opens_at or now
        closes_at = closes_at or (
            max(opens_at, now) + datetime.timedelta(days=window.min_open_days() + 1)
        )
        return {
            "opens_at": timezone.localtime(opens_at).strftime(_LOCAL),
            "closes_at": timezone.localtime(closes_at).strftime(_LOCAL),
        }


class ExtendForm(forms.Form):
    closes_at = forms.DateTimeField(
        label="New closing time",
        widget=_when_widget(),
        help_text="Later than the current deadline. A deadline can be extended once.",
    )
    reason = forms.CharField(
        label="Why", max_length=300, help_text="Shown to owners in the update email."
    )

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("auto_id", "extend_%s")
        super().__init__(*args, **kwargs)


class VoteForm(forms.Form):
    """Formal (binding) votes only. Informal polls are surveys now."""

    title = forms.CharField(max_length=200)
    description = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 3}))
    options = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="One option per line, e.g. Yes / No / Abstain.",
    )
    secret = forms.BooleanField(
        label="Secret ballot", required=False, help_text="Counts only: nobody sees who chose what."
    )
    opens_at = forms.DateTimeField(
        label="Voting opens",
        widget=_when_widget(),
        help_text="Now or later. You open the vote from its page; ballots count from then.",
    )
    closes_at = forms.DateTimeField(
        label="Voting closes",
        widget=_when_widget(),
        help_text="Voting closes by itself at this time.",
    )
    meeting = _MeetingChoice(
        label="At meeting",
        queryset=Meeting.objects.none(),
        required=False,
        empty_label="(none)",
        help_text="The coming owners' meeting this vote belongs to, if any.",
    )

    def __init__(self, *args, user, **kwargs):
        from governance import window

        kwargs.setdefault("auto_id", "vote_%s")
        if not args and "initial" not in kwargs:
            kwargs["initial"] = WindowForm.initial_for()
        super().__init__(*args, **kwargs)
        self.fields["meeting"].queryset = _coming_owner_meetings(user)
        days = window.min_open_days()
        self.fields["closes_at"].help_text = (
            f"At least {days} day{'s' if days != 1 else ''} after it opens. "
            "Voting closes by itself at this time."
        )

    def clean(self):
        data = super().clean()
        opens_at, closes_at = data.get("opens_at"), data.get("closes_at")
        if opens_at and closes_at and closes_at <= opens_at:
            self.add_error("closes_at", "Voting must close after it opens.")
        return data


class ElectionForm(forms.Form):
    title = forms.CharField(max_length=200, help_text="For example: 2027 board election.")
    seats = forms.IntegerField(label="Seats open", min_value=1, max_value=15, initial=2)
    term_years = forms.IntegerField(label="Term (years)", min_value=1, max_value=6, initial=2)
    meeting = _MeetingChoice(
        label="At meeting", queryset=Meeting.objects.none(), required=False, empty_label="(none)"
    )
    citation = forms.CharField(
        label="Authority",
        max_length=200,
        required=False,
        help_text="The section of the Bylaws or Declaration, for example Bylaws §3.2.",
    )

    def __init__(self, *args, user, **kwargs):
        kwargs.setdefault("auto_id", "election_%s")
        super().__init__(*args, **kwargs)
        self.fields["meeting"].queryset = Meeting.objects.for_user(user).filter(
            status=MeetingStatus.SCHEDULED
        )


def _survey_form(data=None):
    from governance.survey_views import SurveyForm

    if data is not None:
        return SurveyForm(data, auto_id="survey_%s")
    today = timezone.localdate()
    return SurveyForm(
        initial={"opens_on": today, "closes_on": today + datetime.timedelta(days=14)},
        auto_id="survey_%s",
    )


def _shown(request) -> dict[str, bool]:
    user = request.user
    can_votes = policy.can(user, "vote.view")
    can_surveys = policy.can(user, "survey.view")  # tenants too (surveys for residents)
    return {
        "votes": can_votes and features.enabled("votes", request),
        "elections": can_votes and features.enabled("elections", request),
        "surveys": can_surveys and features.enabled("surveys", request),
    }


def _vote_row(v: Vote, user) -> dict:
    voted = services.my_ballots(v, user)
    meta = ["Informal poll (from before polls became surveys)"] if v.kind != VoteKind.FORMAL else []
    if not meta:
        meta.append("Formal vote")
    if v.closes_at and v.status != VoteStatus.CLOSED:
        if v.opens_at and v.status == VoteStatus.OPEN and v.opens_at > timezone.now():
            meta.append(f"opens {window.when(v.opens_at)}")
        meta.append(f"vote by {window.when(v.closes_at)}")
    if voted:
        meta.append(
            "Your ballot was recorded"
            if v.secret
            else f"You voted on {timezone.localtime(voted[0].cast_at):%b %-d, %Y}"
        )
    return {
        "type": "votes",
        "icon": "vote",
        "title": v.title,
        "url": reverse("vote_detail", args=[v.pk]),
        "meta": " · ".join(meta),
        "state": v.status,
        "label": v.get_status_display(),
        "open": v.kind == VoteKind.FORMAL and v.status in (VoteStatus.DRAFT, VoteStatus.OPEN),
        "when": v.created_at,
    }


def _election_row(e: Election, user) -> dict:
    can_vote = bool(election_services.lots_actor_may_vote_for(e, user))
    # Only ballots cast since this person owns the lot (R2-010).
    voted = bool(election_services.my_lots_participation(e, user)[0])
    meta = [f"Board election · {e.seats} seat{'s' if e.seats != 1 else ''}"]
    if e.state == ElectionState.VOTING and e.voting_closes_at:
        if e.voting_opens_at and e.voting_opens_at > timezone.now():
            meta.append(f"opens {window.when(e.voting_opens_at)}")
        meta.append(f"vote by {window.when(e.voting_closes_at)}")
    if can_vote:
        meta.append("Your ballot is open")
    elif voted:
        meta.append("Your ballot was recorded")
    return {
        "type": "elections",
        "icon": "users",
        "title": e.title,
        "url": reverse("election_detail", args=[e.pk]),
        "meta": " · ".join(meta),
        "state": e.state,
        "label": e.get_state_display(),
        "open": e.state in (ElectionState.NOMINATIONS, ElectionState.VOTING),
        "when": e.created_at,
    }


def _survey_row(s, user) -> dict:
    responded = surveys.has_responded(s, user)
    meta = ["Survey"]
    if s.phase == "scheduled":
        meta.append(f"opens {_short(s.opens_on)}")
    meta.append("closed" if s.phase == "closed" else f"closes {_short(s.closes_on)}")
    if s.anonymous:
        meta.append("anonymous")
    if responded:
        meta.append(f"You answered on {responded:%b %-d, %Y}")
    elif s.phase == "open" and surveys.can_respond(s, user):
        meta.append("Waiting for your answer")
    return {
        "type": "surveys",
        "icon": "list",
        "title": s.title,
        "url": reverse("survey_detail", args=[s.pk]),
        "meta": " · ".join(meta),
        "state": s.phase,
        "label": s.phase_label,
        "open": s.phase in ("open", "scheduled", "draft"),
        "when": s.created_at,
    }


def _short(value) -> str:
    return short_date(value)


def _rows(request, shown: dict[str, bool]) -> list[dict]:
    user = request.user
    rows: list[dict] = []
    if shown["votes"]:
        votes = Vote.objects.all()
        if not policy.can(user, "vote.manage"):
            votes = votes.exclude(status=VoteStatus.DRAFT)
        rows += [_vote_row(v, user) for v in votes[:200]]
    if shown["elections"]:
        rows += [_election_row(e, user) for e in Election.objects.all()[:100]]
    if shown["surveys"]:
        rows += [_survey_row(s, user) for s in surveys.visible_surveys(user)[:200]]
    rows.sort(key=lambda r: r["when"], reverse=True)
    return rows


def _proxy_meetings(user) -> list[dict]:
    """Coming owners' meetings (and meetings with an election) where this
    owner can give a proxy, and whether they already have."""
    if not policy.can(user, "vote.cast"):
        return []
    from core.dashboard import proxy_meetings

    meetings = {m.pk: m for m in _coming_owner_meetings(user)[:5]}
    for m in proxy_meetings(user):
        meetings.setdefault(m.pk, m)
    if not meetings:
        return []
    owned = policy.roles_for(user).owned_lot_ids
    given = set(
        Proxy.objects.for_user(user)
        .filter(meeting_id__in=meetings, revoked_at__isnull=True, lot_id__in=owned)
        .values_list("meeting_id", flat=True)
    )
    return [
        {"meeting": m, "given": m.pk in given}
        for m in sorted(meetings.values(), key=lambda m: m.starts_at)
    ]


def render_voting(request, *, open_panel: str = "", forms_: dict | None = None, status=200):
    user = request.user
    shown = _shown(request)
    if not policy.can(user, "vote.view") and not policy.can(user, "survey.view"):
        policy.require(user, "vote.view")
    if not any(shown.values()):
        raise Http404
    kind = request.GET.get("type", "")
    if kind not in TYPES or not shown[kind]:
        kind = ""
    past = request.GET.get("status") == "past"
    all_rows = _rows(request, {k: v and (not kind or k == kind) for k, v in shown.items()})
    rows = [r for r in all_rows if r["open"] != past]
    rows.sort(key=lambda r: (r["state"] not in ("open", "voting", "nominations"),))
    forms_ = forms_ or {}
    can_vote_manage = policy.can(user, "vote.manage")
    new = {
        "vote": shown["votes"] and can_vote_manage,
        "election": shown["elections"] and can_vote_manage,
        "survey": shown["surveys"] and surveys.can_manage(user),
    }
    panel = open_panel or request.GET.get("new", "")
    context = {
        "rows": rows,
        # Someone who can only answer surveys (a tenant) sees "Surveys", as
        # their menu does (WT-018).
        "heading": "Surveys" if [k for k, v in shown.items() if v] == ["surveys"] else "Voting",
        "open_count": sum(1 for r in all_rows if r["open"]),
        "past": past,
        "kind": kind,
        "shown": shown,
        "type_links": [
            (t, {"votes": "Votes", "elections": "Elections", "surveys": "Surveys"}[t])
            for t in TYPES
            if shown[t]
        ],
        "new": new,
        "open_panel": panel if new.get(panel) else "",
        "vote_form": forms_.get("vote") or (VoteForm(user=user) if new["vote"] else None),
        "election_form": forms_.get("election")
        or (ElectionForm(user=user) if new["election"] else None),
        "survey_form": forms_.get("survey") or (_survey_form() if new["survey"] else None),
        "proxy_meetings": _proxy_meetings(user),
    }
    return render(request, "governance/voting.html", context, status=status)


def _to_voting(kind: str):
    return redirect(f"{reverse('voting')}?type={kind}")


@login_required
def voting(request):
    return render_voting(request)


@login_required
def votes(request):
    """Old list URL: GET goes to Voting (votes); POST creates a formal vote."""
    policy.require(request.user, "vote.view")
    if request.method != "POST":
        return _to_voting("votes")
    policy.require(request.user, "vote.manage")
    form = VoteForm(request.POST, user=request.user)
    if form.is_valid():
        data = dict(form.cleaned_data)
        data["options"] = data["options"].splitlines()
        try:
            vote = services.create_vote(
                kind=VoteKind.FORMAL, actor=request.user, request=request, **data
            )
        except services.GovernanceError as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(request, "Vote created as a draft.")
            return redirect("vote_detail", pk=vote.pk)
    return render_voting(request, open_panel="vote", forms_={"vote": form})


@login_required
def elections(request):
    """Old list URL: GET goes to Voting (elections); POST creates one."""
    policy.require(request.user, "vote.view")
    if request.method != "POST":
        return _to_voting("elections")
    policy.require(request.user, "vote.manage")
    form = ElectionForm(request.POST, user=request.user)
    if form.is_valid():
        try:
            election = election_services.create_election(
                actor=request.user, request=request, **form.cleaned_data
            )
        except election_services.ElectionError as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(request, "Election created: nominations are open.")
            return redirect("election_detail", pk=election.pk)
    return render_voting(request, open_panel="election", forms_={"election": form})


@login_required
def surveys_list(request):
    """Old list URL: GET goes to Voting (surveys); POST creates a draft."""
    policy.require(request.user, "survey.view")
    if request.method != "POST":
        return _to_voting("surveys")
    policy.require(request.user, "survey.manage")
    form = _survey_form(request.POST)
    if form.is_valid():
        try:
            survey = surveys.create_survey(actor=request.user, request=request, **form.cleaned_data)
        except surveys.SurveyError as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(request, "Survey created as a draft. Now add its questions.")
            return redirect("survey_detail", pk=survey.pk)
    return render_voting(request, open_panel="survey", forms_={"survey": form})
