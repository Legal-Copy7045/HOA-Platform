"""Elections, proxies, quorum and written-consent pages."""

from allauth.account.decorators import reauthentication_required
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from community.models import BoardSeat, Lot, Ownership, Person
from core import policy
from core.scoping import system_access
from governance import elections
from governance.models import (
    Candidate,
    ConsentRequest,
    Election,
    ElectionState,
    Meeting,
    Proxy,
)


def _flash(request, fn, ok):
    try:
        result = fn()
    except elections.ElectionError as exc:
        messages.error(request, str(exc))
        return None
    messages.success(request, ok)
    return result


# --- Elections --------------------------------------------------------------------------


def _owner_choices():
    """Homes to nominate from, by street address only: owners' names stay
    private unless they opted into the directory (SEC-010); a nominee's name
    appears once nominated."""
    with system_access("nomination choices"):
        lot_ids = set(Ownership.objects.active().values_list("lot_id", flat=True))
        return list(Lot.objects.filter(pk__in=lot_ids).order_by("street_address", "number"))


@login_required
def election_detail(request, pk):
    policy.require(request.user, "vote.view")
    election = get_object_or_404(Election, pk=pk)
    return _render_election(request, election)


@login_required
def election_who_voted_csv(request, pk):
    from governance import window

    policy.require(request.user, "vote.turnout")
    election = get_object_or_404(Election, pk=pk)
    if election.state == ElectionState.NOMINATIONS:
        raise Http404
    return window.export_who_has_voted(election, actor=request.user, request=request)


def _render_election(request, election, *, window_form=None, extend_form=None, status=200):
    from governance import window
    from governance.voting_views import ExtendForm, WindowForm

    user = request.user
    can_manage = policy.can(user, "vote.manage")
    me = elections._person(user)
    candidates = Candidate.objects.filter(election=election).select_related("person")
    show_results = election.state == ElectionState.CERTIFIED or (
        can_manage and election.state == ElectionState.CLOSED
    )
    # That a ballot was cast for my lot (never what it chose: D21), and only
    # mine if cast since I own the lot (BUG-004).
    recorded, cast_before_me = elections.my_lots_participation(election, user)
    meeting_ahead = (
        election.meeting
        if election.meeting_id
        and election.state in (ElectionState.NOMINATIONS, ElectionState.VOTING)
        and election.meeting.status == "scheduled"
        and policy.can(user, "vote.cast")
        else None
    )
    context = window.page_context(election, user)
    context.update(
        {
            "window_form": window_form
            or (
                WindowForm(initial=WindowForm.initial_for())
                if can_manage and election.state == ElectionState.NOMINATIONS
                else None
            ),
            "extend_form": extend_form or ExtendForm(),
            "extend_open": extend_form is not None,
            "who_voted_url": reverse("election_who_voted_csv", args=[election.pk]),
        }
    )
    return render(
        request,
        "governance/election_detail.html",
        context
        | {
            "e": election,
            "candidates": candidates,
            "me": me,
            "can_manage": can_manage,
            "can_nominate": election.state == ElectionState.NOMINATIONS
            and (policy.can(user, "vote.cast") or can_manage),
            "owners": _owner_choices() if election.state == ElectionState.NOMINATIONS else [],
            "my_lots": elections.lots_actor_may_vote_for(election, user),
            "recorded": recorded,
            "cast_before_me": cast_before_me,
            "proxy_meeting": meeting_ahead,
            "ballot": elections.ballot_candidates(election),
            "tally": elections.tally(election) if show_results else None,
            "results": election.results.select_related("candidate__person")
            if election.state == ElectionState.CERTIFIED
            else [],
        },
        status=status,
    )


@login_required
@require_POST
@reauthentication_required(enabled=lambda r: r.resolver_match.kwargs.get("action") == "ballot")
def election_action(request, pk, action):
    election = get_object_or_404(Election, pk=pk)
    user = request.user
    if action == "nominate":
        me = elections._person(user)
        if request.POST.get("lot"):
            with system_access("nominee by home"):
                ownership = (
                    Ownership.objects.active()
                    .filter(lot_id=int(request.POST["lot"]) if request.POST["lot"].isdigit() else 0)
                    .select_related("person")
                    .order_by("start_date", "pk")
                    .first()
                )
            if ownership is None:
                raise Http404
            person = ownership.person
        elif me is not None:
            person = me
        else:
            raise Http404
        _flash(
            request,
            lambda: elections.nominate(
                election, person=person, statement=request.POST.get("statement", ""), actor=user
            ),
            "Nominated. The nominee must accept before appearing on the ballot.",
        )
    elif action in ("accept", "withdraw"):
        candidate = get_object_or_404(
            Candidate, pk=request.POST.get("candidate"), election=election
        )
        if action == "accept":
            _flash(
                request,
                lambda: elections.accept_nomination(
                    candidate, actor=user, statement=request.POST.get("statement"), request=request
                ),
                "Nomination accepted.",
            )
        else:
            _flash(
                request,
                lambda: elections.withdraw_candidate(candidate, actor=user, request=request),
                "Withdrawn.",
            )
    elif action == "open":
        from governance.voting_views import WindowForm

        policy.require(user, "vote.manage")
        form = WindowForm(request.POST)
        if form.is_valid():
            try:
                elections.open_voting(
                    election,
                    opens_at=form.cleaned_data["opens_at"],
                    closes_at=form.cleaned_data["closes_at"],
                    actor=user,
                    request=request,
                )
            except elections.ElectionError as exc:
                form.add_error(None, str(exc))
            else:
                messages.success(
                    request, "Voting is open. It closes by itself at its closing time."
                )
                return redirect("election_detail", pk=election.pk)
        return _render_election(request, election, window_form=form)
    elif action == "close":
        _flash(
            request,
            lambda: elections.close_voting(
                election, actor=user, reason=request.POST.get("reason", ""), request=request
            ),
            "Voting closed.",
        )
    elif action == "extend":
        from governance import window
        from governance.voting_views import ExtendForm

        policy.require(user, "vote.manage")
        form = ExtendForm(request.POST)
        if form.is_valid():
            try:
                window.extend(
                    election,
                    closes_at=form.cleaned_data["closes_at"],
                    reason=form.cleaned_data["reason"],
                    actor=user,
                    request=request,
                )
            except window.WindowError as exc:
                form.add_error(None, str(exc))
            else:
                messages.success(request, "Deadline extended. Owners are told by email.")
                return redirect("election_detail", pk=election.pk)
        return _render_election(request, election, extend_form=form)
    elif action == "certify":
        _flash(
            request,
            lambda: elections.certify(election, actor=user, request=request),
            "Results certified. Board seats for the elected are proposed.",
        )
    elif action == "ballot":
        _flash(
            request,
            lambda: elections.cast_election_ballot(
                election,
                lot_id=int(request.POST.get("lot") or 0),
                candidate_ids=request.POST.getlist("candidates"),
                actor=user,
                request=request,
            ),
            "Ballot cast. Thank you.",
        )
    else:
        raise Http404
    return redirect("election_detail", pk=election.pk)


# --- Proxies and quorum (on the meeting page) --------------------------------------------


def meeting_extras(user, meeting) -> dict:
    roles = policy.roles_for(user)
    mine = []
    if policy.can(user, "vote.cast"):
        mine = list(
            Proxy.objects.for_user(user)
            .filter(meeting=meeting, revoked_at__isnull=True)
            .select_related("lot", "holder")
        )
    with system_access("board names"):
        board = list(
            Person.objects.filter(
                pk__in=BoardSeat.objects.active().values_list("person_id", flat=True)
            ).order_by("display_name")
        )
    with system_access("my lots"):
        my_lots = list(Lot.objects.filter(pk__in=roles.owned_lot_ids).order_by("number"))
    return {
        "proxies_mine": [p for p in mine if p.lot_id in roles.owned_lot_ids],
        "proxies_held": [p for p in mine if p.holder_id == roles.person_id],
        "proxy_board": board,
        "proxy_lots": my_lots,
        "proxy_homes": [lot for lot in _owner_choices() if lot.pk not in roles.owned_lot_ids]
        if my_lots
        else [],
        "quorum": elections.quorum_status(meeting) if policy.can(user, "meeting.manage") else None,
    }


@login_required
@require_POST
def proxy_grant(request, pk):
    meeting = get_object_or_404(Meeting.objects.for_user(request.user), pk=pk)
    lot = get_object_or_404(
        Lot.objects.unscoped("own lot checked by service"), pk=request.POST.get("lot") or 0
    )
    holder = None
    if request.POST.get("holder"):
        holder = get_object_or_404(
            Person.objects.unscoped("board member by id"), pk=request.POST["holder"]
        )
        with system_access("holder must be on the board"):
            if not BoardSeat.objects.active().filter(person=holder).exists():
                raise PermissionDenied
    elif request.POST.get("holder_home"):
        # "The owner at <address>": homes are listed by street address, no
        # owner names (SEC-010).
        home = request.POST["holder_home"]
        with system_access("proxy holder by home"):
            ownership = (
                Ownership.objects.active()
                .filter(lot_id=int(home) if home.isdigit() else 0)
                .order_by("start_date", "pk")
                .first()
            )
        holder = ownership.person if ownership else None
    if holder is None:
        messages.error(request, "Choose a board member or a home for your proxy.")
    else:
        _flash(
            request,
            lambda: elections.grant_proxy(
                meeting, lot=lot, holder=holder, actor=request.user, request=request
            ),
            "Proxy given. You can revoke it until it is used.",
        )
    return redirect("meeting_detail", pk=meeting.pk)


@login_required
@require_POST
def proxy_revoke(request, pk):
    proxy = get_object_or_404(Proxy.objects.for_user(request.user), pk=pk)
    _flash(
        request,
        lambda: elections.revoke_proxy(proxy, actor=request.user, request=request),
        "Proxy revoked.",
    )
    return redirect("meeting_detail", pk=proxy.meeting_id)


# --- Written consent ---------------------------------------------------------------------


@login_required
def consents(request):
    """Old list URL: written consents live on Resolutions and policies now.
    POST still proposes one (the form there posts here)."""
    policy.require(request.user, "resolution.record")
    if request.method != "POST":
        return redirect(f"{reverse('resolutions')}#consents")
    from governance.views import ConsentForm, render_resolutions

    form = ConsentForm(request.POST)
    if form.is_valid():
        try:
            consent = elections.propose_consent(
                actor=request.user, request=request, **form.cleaned_data
            )
        except elections.ElectionError as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(request, "Sent to every board member for signature.")
            return redirect("consent_detail", pk=consent.pk)
    return render_resolutions(request, consent_form=form, open_panel="consent")


@login_required
def consent_detail(request, pk):
    policy.require(request.user, "resolution.record")
    consent = get_object_or_404(ConsentRequest, pk=pk)
    me = elections._person(request.user)
    if request.method == "POST":
        action = request.POST.get("action")
        if action in ("approve", "decline"):
            _flash(
                request,
                lambda: elections.sign_consent(
                    consent, approve=action == "approve", actor=request.user, request=request
                ),
                "Signed.",
            )
        elif action == "withdraw":
            elections.withdraw_consent(consent, actor=request.user, request=request)
        return redirect("consent_detail", pk=consent.pk)
    signed = {s.person_id: s for s in consent.signatures.all()}
    with system_access("consent signers"):
        signers = [(p, signed.get(p.pk)) for p in consent.signers.order_by("display_name")]
    return render(
        request,
        "governance/consent_detail.html",
        {
            "c": consent,
            "signers": signers,
            "can_sign": consent.state == "open"
            and me is not None
            and any(p.pk == me.pk and s is None for p, s in signers),
        },
    )
