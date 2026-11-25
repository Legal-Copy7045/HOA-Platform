"""Elections, proxies, meeting quorum and written consent (Milestone 4).

Elections: owners nominate owners; nominees accept; the board opens voting;
one ballot per lot (by an owner, or by the holder of that lot's proxy for the
meeting), choosing up to the number of open seats; ballots are stored like
secret votes (who voted apart from the unlinked choices). A board member who
is not a candidate certifies the count; the elected are proposed for board
seats (with the second approval where it applies). A tie for the last seat is
reported for the board to resolve as the Bylaws say.

Written consent: every board member serving when it is proposed must approve;
one decline means it is not adopted. Counsel should confirm the rule for the
association (Bylaws and the PA nonprofit corporation law).
"""

from __future__ import annotations

import datetime

from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.db.models import Count
from django.utils import timezone

from audit.services import record_event
from community.models import BoardSeat, Lot, Ownership, Person
from community.rules import rule
from core import policy
from core.policy import can, require
from core.scoping import system_access
from core.text import single_line
from governance.models import (
    Attendance,
    Candidate,
    ConsentRequest,
    ConsentSignature,
    ConsentState,
    Election,
    ElectionChoice,
    ElectionParticipation,
    ElectionResult,
    ElectionState,
    Meeting,
    Proxy,
    ResolutionKind,
)


class ElectionError(ValueError):
    pass


def _person(actor) -> Person | None:
    with system_access("person of actor"):
        return Person.objects.filter(user=actor).first()


def _is_owner(person: Person) -> bool:
    with system_access("is owner"):
        return Ownership.objects.active().filter(person=person).exists()


# --- Elections ----------------------------------------------------------------------------


def create_election(
    *,
    title: str,
    seats: int,
    meeting=None,
    term_years: int = 2,
    citation: str = "",
    actor,
    request=None,
) -> Election:
    require(actor, "vote.manage")
    title = single_line(title)[:200]
    if not title or not 1 <= int(seats) <= 15:
        raise ElectionError("An election needs a title and between 1 and 15 seats.")
    election = Election.objects.create(
        title=title,
        seats=int(seats),
        meeting=meeting,
        term_years=max(1, min(int(term_years), 6)),
        citation=single_line(citation)[:200],
        created_by=actor,
    )
    record_event(
        "election.created",
        actor=actor,
        request=request,
        target=election,
        metadata={"seats": election.seats},
    )
    return election


def nominate(election: Election, *, person: Person, statement: str = "", actor, request=None):
    """Any owner may nominate an owner (themselves included) while nominations
    are open; the board may too."""
    if not (can(actor, "vote.cast") or can(actor, "vote.manage")):
        raise PermissionDenied("nominate")
    if election.state != ElectionState.NOMINATIONS:
        raise ElectionError("Nominations are closed.")
    if not _is_owner(person):
        raise ElectionError("Only current owners can stand for the board.")
    me = _person(actor)
    try:
        candidate = Candidate.objects.create(
            election=election,
            person=person,
            statement=(statement or "").strip()[:3000],
            nominated_by=actor,
            accepted=me is not None and me.pk == person.pk,  # self-nomination accepts
        )
    except IntegrityError as exc:
        raise ElectionError("That person is already nominated.") from exc
    record_event(
        "election.nominated",
        actor=actor,
        request=request,
        target=election,
        metadata={"person_id": person.pk},
    )
    return candidate


def accept_nomination(candidate: Candidate, *, actor, statement: str | None = None, request=None):
    """The nominee accepts (or the board records a written acceptance), only
    while nominations are open: the ballot is fixed when voting opens."""
    me = _person(actor)
    if not ((me and me.pk == candidate.person_id) or can(actor, "vote.manage")):
        raise PermissionDenied("accept")
    fields = {"accepted": True}
    if statement is not None:
        fields["statement"] = statement.strip()[:3000]
    with transaction.atomic():
        locked = Election.objects.select_for_update().get(pk=candidate.election_id)
        if locked.state != ElectionState.NOMINATIONS:
            raise ElectionError("Nominations are closed: the ballot is fixed.")
        Candidate.objects.filter(pk=candidate.pk).update(**fields)
    record_event(
        "election.candidate_accepted",
        actor=actor,
        request=request,
        target=candidate.election,
        metadata={"candidate_id": candidate.pk},
    )
    candidate.refresh_from_db()
    return candidate


def withdraw_candidate(candidate: Candidate, *, actor, request=None):
    """Only while nominations are open: once voting starts the ballot is fixed,
    so no one (not even the board) can drop a candidate and the votes cast for
    them (security review SEC-001)."""
    me = _person(actor)
    if not ((me and me.pk == candidate.person_id) or can(actor, "vote.manage")):
        raise PermissionDenied("withdraw")
    with transaction.atomic():
        locked = Election.objects.select_for_update().get(pk=candidate.election_id)
        if locked.state != ElectionState.NOMINATIONS:
            raise ElectionError(
                "Voting has opened, so the ballot can no longer change. A candidate who "
                "will not serve can decline the seat after the count."
            )
        Candidate.objects.filter(pk=candidate.pk).update(withdrawn=True)
    record_event(
        "election.candidate_withdrawn",
        actor=actor,
        request=request,
        target=candidate.election,
        metadata={"candidate_id": candidate.pk},
    )


def ballot_candidates(election: Election):
    return Candidate.objects.filter(
        election=election, accepted=True, withdrawn=False
    ).select_related("person")


def open_voting(election: Election, *, actor, request=None) -> Election:
    require(actor, "vote.manage")
    if election.state != ElectionState.NOMINATIONS:
        raise ElectionError("Voting can only be opened once, after nominations.")
    if not ballot_candidates(election).exists():
        raise ElectionError("No candidate has accepted a nomination yet.")
    with system_access("eligible lots"):
        eligible = Lot.objects.count()
    Election.objects.filter(pk=election.pk).update(
        state=ElectionState.VOTING, eligible_lots=eligible
    )
    record_event("election.voting_opened", actor=actor, request=request, target=election)
    election.refresh_from_db()
    return election


def close_voting(election: Election, *, actor, request=None) -> Election:
    require(actor, "vote.manage")
    with transaction.atomic():
        locked = Election.objects.select_for_update().get(pk=election.pk)
        if locked.state != ElectionState.VOTING:
            raise ElectionError("Voting is not open.")
        Election.objects.filter(pk=election.pk).update(state=ElectionState.CLOSED)
    record_event("election.voting_closed", actor=actor, request=request, target=election)
    election.refresh_from_db()
    return election


def lots_actor_may_vote_for(election: Election, actor) -> list[dict]:
    """Lots this person can still cast an election ballot for: their own, and
    lots whose live proxy for the election's meeting names them."""
    if election.state != ElectionState.VOTING:
        return []
    roles = policy.roles_for(actor)
    out = {}
    if can(actor, "vote.cast"):
        for lot_id in roles.owned_lot_ids:
            out[lot_id] = False
    if election.meeting_id and roles.person_id:
        with system_access("proxy lots"):
            for lot_id in live_proxies(
                Proxy.objects.filter(meeting_id=election.meeting_id, holder_id=roles.person_id)
            ).values_list("lot_id", flat=True):
                out.setdefault(lot_id, True)
    with system_access("voted lots"):
        voted = set(
            ElectionParticipation.objects.filter(election=election).values_list("lot_id", flat=True)
        )
        lots = {lot.pk: lot for lot in Lot.objects.filter(pk__in=list(out))}
    return [
        {"lot": lots[lot_id], "by_proxy": by_proxy}
        for lot_id, by_proxy in sorted(out.items())
        if lot_id not in voted and lot_id in lots
    ]


def cast_election_ballot(
    election: Election, *, lot_id: int, candidate_ids: list[int], actor, request=None
) -> None:
    try:
        candidate_ids = list(dict.fromkeys(int(c) for c in candidate_ids))
        lot_id = int(lot_id)
    except (TypeError, ValueError) as exc:
        raise ElectionError("Choose from the candidates on the ballot.") from exc
    with transaction.atomic():
        locked = Election.objects.select_for_update().get(pk=election.pk)
        if locked.state != ElectionState.VOTING:
            raise ElectionError("Voting is not open.")
        allowed = {row["lot"].pk: row["by_proxy"] for row in lots_actor_may_vote_for(locked, actor)}
        if lot_id not in allowed:
            raise ElectionError("You cannot cast a ballot for that lot (or it has voted).")
        if not candidate_ids or len(candidate_ids) > locked.seats:
            raise ElectionError(f"Choose between 1 and {locked.seats} candidates.")
        valid = set(ballot_candidates(locked).values_list("pk", flat=True))
        if not set(candidate_ids) <= valid:
            raise ElectionError("Choose from the candidates on the ballot.")
        try:
            with transaction.atomic():
                ElectionParticipation.objects.create(
                    election=locked, lot_id=lot_id, cast_by=actor, by_proxy=allowed[lot_id]
                )
        except IntegrityError as exc:
            raise ElectionError("A ballot was already cast for this lot.") from exc
        for cid in candidate_ids:  # unlinked from the participation row
            ElectionChoice.objects.create(election=locked, candidate_id=cid)
    record_event(
        "election.ballot_cast",
        actor=actor,
        request=request,
        target=election,
        metadata={"lot_id": lot_id, "by_proxy": allowed[lot_id]},
    )


def tally(election: Election) -> dict:
    counts = dict(
        ElectionChoice.objects.filter(election=election)
        .values_list("candidate")
        .annotate(n=Count("pk"))
        .values_list("candidate", "n")
    )
    rows = sorted(
        ({"candidate": c, "votes": counts.get(c.pk, 0)} for c in ballot_candidates(election)),
        key=lambda r: (-r["votes"], r["candidate"].person.display_name),
    )
    seats = election.seats
    cutoff = rows[seats - 1]["votes"] if len(rows) >= seats else None
    beyond = rows[seats]["votes"] if len(rows) > seats else None
    tie_for_last = cutoff is not None and beyond is not None and cutoff == beyond and cutoff > 0
    for i, r in enumerate(rows):
        r["elected"] = i < seats and r["votes"] > 0 and not (tie_for_last and r["votes"] == cutoff)
        r["tied"] = tie_for_last and r["votes"] == cutoff
    with system_access("turnout"):
        ballots = ElectionParticipation.objects.filter(election=election).count()
    return {"rows": rows, "ballots": ballots, "tie_for_last": tie_for_last}


def _add_years(day: datetime.date, years: int) -> datetime.date:
    try:
        return day.replace(year=day.year + years)
    except ValueError:  # 29 February into a non-leap year
        return day.replace(year=day.year + years, day=28)


def certify(election: Election, *, actor, request=None) -> dict:
    """A board member who is not a candidate certifies the count; results are
    recorded permanently and the elected are proposed for board seats."""
    require(actor, "vote.manage")
    me = _person(actor)
    if me is not None and Candidate.objects.filter(election=election, person=me).exists():
        raise ElectionError("A candidate cannot certify their own election.")
    with transaction.atomic():
        locked = Election.objects.select_for_update().get(pk=election.pk)
        if locked.state != ElectionState.CLOSED:
            raise ElectionError("Close voting before certifying.")
        result = tally(locked)
        for r in result["rows"]:
            ElectionResult.objects.create(
                election=locked, candidate=r["candidate"], votes=r["votes"], elected=r["elected"]
            )
        Election.objects.filter(pk=locked.pk).update(
            state=ElectionState.CERTIFIED, certified_by=actor, certified_at=timezone.now()
        )
    record_event(
        "election.certified",
        actor=actor,
        request=request,
        target=election,
        metadata={"ballots": result["ballots"], "tie": result["tie_for_last"]},
    )
    from community.services import CommunityError, propose_board_seat

    today = timezone.localdate()
    for r in result["rows"]:
        if r["elected"]:
            try:
                propose_board_seat(
                    person=r["candidate"].person,
                    seat_type="owner_elected",
                    term_start=today,
                    term_end=_add_years(today, election.term_years) - datetime.timedelta(days=1),
                    citation=f"Elected: {election.title}"
                    + (f" ({election.citation})" if election.citation else ""),
                    actor=actor,
                    request=request,
                )
            except CommunityError:
                continue  # e.g. already holds a seat; the board handles it
    return result


# --- Proxies ------------------------------------------------------------------------------


def grant_proxy(meeting: Meeting, *, lot: Lot, holder: Person, actor, request=None) -> Proxy:
    require(actor, "vote.cast")
    roles = policy.roles_for(actor)
    if lot.pk not in roles.owned_lot_ids:
        raise PermissionDenied("proxy")
    if meeting.starts_at < timezone.now() - datetime.timedelta(hours=12):
        raise ElectionError("That meeting has already taken place.")
    grantor = _person(actor)
    if grantor is None or holder.pk == grantor.pk:
        raise ElectionError("Choose someone else to hold your proxy.")
    with transaction.atomic(), system_access("grant proxy"):
        Proxy.objects.filter(meeting=meeting, lot=lot, revoked_at__isnull=True).update(
            revoked_at=timezone.now()
        )
        proxy = Proxy.objects.create(meeting=meeting, lot=lot, grantor=grantor, holder=holder)
    record_event(
        "meeting.proxy_granted",
        actor=actor,
        request=request,
        target=meeting,
        metadata={"lot_id": lot.pk},
    )
    return proxy


def live_proxies(queryset, on=None):
    """Proxies not revoked whose grantor still owns the lot: a sale ends the
    seller's proxy (security review SEC-003)."""
    from django.db.models import Exists, OuterRef

    owns = Ownership.objects.active(on).filter(
        person_id=OuterRef("grantor_id"), lot_id=OuterRef("lot_id")
    )
    return queryset.filter(revoked_at__isnull=True).filter(Exists(owns))


def revoke_proxy(proxy: Proxy, *, actor, request=None) -> None:
    roles = policy.roles_for(actor)
    if proxy.lot_id not in roles.owned_lot_ids and not can(actor, "meeting.manage"):
        raise PermissionDenied("proxy")
    with system_access("revoke proxy"):
        Proxy.objects.filter(pk=proxy.pk, revoked_at__isnull=True).update(revoked_at=timezone.now())
    record_event(
        "meeting.proxy_revoked",
        actor=actor,
        request=request,
        target=proxy.meeting,
        metadata={"lot_id": proxy.lot_id},
    )


def quorum_status(meeting: Meeting) -> dict:
    """Lots represented (an owner present, or a proxy whose holder is present)
    against the quorum rule (Bylaws, else the Act's 20%)."""
    with system_access("quorum"):
        eligible = Lot.objects.count()
        present = set(
            Attendance.objects.filter(meeting=meeting, present=True).values_list(
                "person_id", flat=True
            )
        )
        on = timezone.localtime(meeting.starts_at).date()
        lots = set(
            Ownership.objects.active(on)
            .filter(person_id__in=present)
            .values_list("lot_id", flat=True)
        )
        by_proxy = (
            set(
                live_proxies(
                    Proxy.objects.filter(meeting=meeting, holder_id__in=present), on
                ).values_list("lot_id", flat=True)
            )
            - lots
        )
    percent = rule("vote.quorum_percent") or 20
    needed = -(-eligible * int(percent) // 100)  # ceiling
    represented = len(lots) + len(by_proxy)
    return {
        "eligible": eligible,
        "in_person": len(lots),
        "by_proxy": len(by_proxy),
        "represented": represented,
        "percent": percent,
        "needed": needed,
        "met": eligible > 0 and represented >= needed,
    }


# --- Written consent ------------------------------------------------------------------------


def propose_consent(*, title: str, text: str, citation: str = "", actor, request=None):
    require(actor, "resolution.record")
    title = single_line(title)[:200]
    if not title or not (text or "").strip():
        raise ElectionError("A resolution needs a title and its text.")
    with transaction.atomic(), system_access("consent signers"):
        consent = ConsentRequest.objects.create(
            title=title, text=text.strip(), citation=single_line(citation)[:200], created_by=actor
        )
        consent.signers.set(
            Person.objects.filter(
                pk__in=BoardSeat.objects.active().values_list("person_id", flat=True)
            )
        )
        signer_count = consent.signers.count()
    record_event(
        "consent.proposed",
        actor=actor,
        request=request,
        target=consent,
        metadata={"signers": signer_count},
    )
    return consent


def sign_consent(consent: ConsentRequest, *, approve: bool, actor, request=None):
    require(actor, "resolution.record")
    me = _person(actor)
    with transaction.atomic(), system_access("consent signers"):
        locked = ConsentRequest.objects.select_for_update().get(pk=consent.pk)
        if locked.state != ConsentState.OPEN:
            raise ElectionError("This consent is no longer open.")
        if me is None or not locked.signers.filter(pk=me.pk).exists():
            raise ElectionError("Only the board members asked can sign.")
        try:
            with transaction.atomic():
                ConsentSignature.objects.create(
                    consent=locked, person=me, signed_by=actor, approve=approve
                )
        except IntegrityError as exc:
            raise ElectionError("You have already signed.") from exc
        if not approve:
            ConsentRequest.objects.filter(pk=locked.pk).update(state=ConsentState.FAILED)
        elif locked.signatures.filter(approve=True).count() == locked.signers.count():
            from governance.services import record_resolution

            resolution = record_resolution(
                kind=ResolutionKind.RESOLUTION,
                title=locked.title,
                text=locked.text,
                adopted_on=timezone.localdate(),
                votes_for=locked.signers.count(),
                citation=(
                    "Unanimous written consent of the board"
                    + (f" ({locked.citation})" if locked.citation else "")
                ),
                actor=actor,
                request=request,
            )
            ConsentRequest.objects.filter(pk=locked.pk).update(
                state=ConsentState.ADOPTED, resolution=resolution
            )
    record_event(
        "consent.signed",
        actor=actor,
        request=request,
        target=consent,
        metadata={"approve": approve},
    )
    consent.refresh_from_db()
    return consent


def withdraw_consent(consent: ConsentRequest, *, actor, request=None) -> None:
    """Any board member may withdraw an open consent (e.g. a signer left the
    board, so it can never become unanimous)."""
    require(actor, "resolution.record")
    updated = ConsentRequest.objects.filter(pk=consent.pk, state=ConsentState.OPEN).update(
        state=ConsentState.WITHDRAWN
    )
    if updated:
        record_event("consent.withdrawn", actor=actor, request=request, target=consent)
