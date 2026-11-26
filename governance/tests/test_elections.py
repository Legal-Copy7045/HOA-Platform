"""Milestone 4: elections, proxies, quorum and written consent."""

import datetime

import pytest
from django.core.exceptions import PermissionDenied
from django.urls import reverse
from django.utils import timezone

from community import services as community
from community.models import BoardSeatNomination, Person
from community.tests.factories import LotFactory, make_member
from core.scoping import system_access
from governance import elections
from governance import services as gov
from governance.models import (
    ElectionChoice,
    ElectionParticipation,
    Resolution,
)

pytestmark = pytest.mark.django_db


def _person(user):
    with system_access("t"):
        return Person.objects.get(user=user)


@pytest.fixture
def w(settings):
    settings.BOARD_SEAT_SECOND_APPROVAL = True
    board = make_member(board=True)
    board2 = make_member(board=True)
    owners = [make_member(lots=[LotFactory()]) for _ in range(4)]
    meeting = gov.create_meeting(
        title="Annual meeting",
        kind="annual",
        starts_at=timezone.now() + datetime.timedelta(days=20),
        actor=board,
    )
    election = elections.create_election(
        title="2027 election", seats=1, meeting=meeting, actor=board
    )
    return {"board": board, "board2": board2, "owners": owners, "meeting": meeting, "e": election}


def _ready(w, *candidates):
    """Nominate and accept the given owners, then open voting."""
    for user in candidates:
        elections.nominate(w["e"], person=_person(user), actor=user)  # self-nomination accepts
    return elections.open_voting(w["e"], actor=w["board"])


def test_nominees_must_accept_before_the_ballot(w):
    a, b = w["owners"][:2]
    candidate = elections.nominate(w["e"], person=_person(b), actor=a)
    assert not candidate.accepted
    elections.nominate(w["e"], person=_person(a), actor=a)
    assert [c.person_id for c in elections.ballot_candidates(w["e"])] == [_person(a).pk]
    elections.accept_nomination(candidate, actor=b)
    assert elections.ballot_candidates(w["e"]).count() == 2


def test_one_ballot_per_lot_and_choices_are_unlinked(w):
    a, b, c, d = w["owners"]
    election = _ready(w, a, b)
    cand_a = elections.ballot_candidates(election).get(person=_person(a))
    lot_c = next(iter(elections.lots_actor_may_vote_for(election, c)))["lot"]
    elections.cast_election_ballot(election, lot_id=lot_c.pk, candidate_ids=[cand_a.pk], actor=c)
    with pytest.raises(elections.ElectionError):
        elections.cast_election_ballot(
            election, lot_id=lot_c.pk, candidate_ids=[cand_a.pk], actor=c
        )
    with pytest.raises(elections.ElectionError):  # not their lot
        elections.cast_election_ballot(
            election, lot_id=lot_c.pk, candidate_ids=[cand_a.pk], actor=d
        )
    fields = {f.name for f in ElectionChoice._meta.get_fields()}
    assert fields == {"id", "election", "candidate"}
    assert ElectionParticipation.objects.filter(election=election).count() == 1


def test_too_many_choices_are_refused(w):
    a, b, c, _ = w["owners"]
    election = _ready(w, a, b)
    ids = list(elections.ballot_candidates(election).values_list("pk", flat=True))
    lot_c = elections.lots_actor_may_vote_for(election, c)[0]["lot"]
    with pytest.raises(elections.ElectionError):
        elections.cast_election_ballot(election, lot_id=lot_c.pk, candidate_ids=ids, actor=c)


def test_proxy_holder_votes_for_the_lot_and_counts_for_quorum(w):
    a, _b, c, d = w["owners"]
    election = _ready(w, a)
    lot_d = elections.lots_actor_may_vote_for(election, d)[0]["lot"]
    elections.grant_proxy(w["meeting"], lot=lot_d, holder=_person(c), actor=d)
    choices = elections.lots_actor_may_vote_for(election, c)
    assert {row["lot"].pk: row["by_proxy"] for row in choices}[lot_d.pk] is True
    cand = elections.ballot_candidates(election).get()
    elections.cast_election_ballot(election, lot_id=lot_d.pk, candidate_ids=[cand.pk], actor=c)
    assert ElectionParticipation.objects.get(lot=lot_d).by_proxy is True
    gov.record_attendance(w["meeting"], _person(c), actor=w["board"])
    q = elections.quorum_status(w["meeting"])
    assert q["in_person"] == 1 and q["by_proxy"] == 1 and q["represented"] == 2


def test_only_the_lot_owner_grants_a_proxy(w):
    a, b, c, d = w["owners"]
    lot_d = elections.lots_actor_may_vote_for(_ready(w, a), d)[0]["lot"]
    with pytest.raises(PermissionDenied):
        elections.grant_proxy(w["meeting"], lot=lot_d, holder=_person(c), actor=b)


def test_certify_by_a_non_candidate_proposes_the_seat(w):
    a, b, c, d = w["owners"]
    election = _ready(w, a, b)
    cand_a = elections.ballot_candidates(election).get(person=_person(a))
    for voter in (c, d):
        lot = elections.lots_actor_may_vote_for(election, voter)[0]["lot"]
        elections.cast_election_ballot(
            election, lot_id=lot.pk, candidate_ids=[cand_a.pk], actor=voter
        )
    elections.close_voting(election, actor=w["board"])
    result = elections.certify(election, actor=w["board"])
    assert [r["elected"] for r in result["rows"]] == [True, False]
    with system_access("t"):
        assert BoardSeatNomination.objects.filter(person=_person(a), outcome="").exists()
    assert election.results.count() == 2


def test_a_candidate_cannot_certify(w):
    candidate_board = w["board2"]
    other = w["owners"][0]
    with system_access("t"):
        community.record_ownership(
            person=_person(candidate_board),
            lot=LotFactory(),
            start_date=datetime.date(2020, 1, 1),
            actor=w["board"],
        )
    elections.nominate(w["e"], person=_person(candidate_board), actor=candidate_board)
    elections.nominate(w["e"], person=_person(other), actor=other)
    elections.open_voting(w["e"], actor=w["board"])
    elections.close_voting(w["e"], actor=w["board"])
    with pytest.raises(elections.ElectionError):
        elections.certify(w["e"], actor=candidate_board)


def test_tie_for_the_last_seat_elects_nobody_for_it(w):
    a, b, c, d = w["owners"]
    election = _ready(w, a, b)
    cands = {c_.person_id: c_ for c_ in elections.ballot_candidates(election)}
    for voter, target in ((c, a), (d, b)):
        lot = elections.lots_actor_may_vote_for(election, voter)[0]["lot"]
        elections.cast_election_ballot(
            election, lot_id=lot.pk, candidate_ids=[cands[_person(target).pk].pk], actor=voter
        )
    result = elections.tally(election)
    assert result["tie_for_last"] and not any(r["elected"] for r in result["rows"])


def test_election_pages(client, w):
    a = w["owners"][0]
    client.force_login(a)
    client.post(
        reverse("election_action", args=[w["e"].pk, "nominate"]),
        {"statement": "I'll help"},
    )
    page = client.get(reverse("election_detail", args=[w["e"].pk])).content
    assert b"I&#x27;ll help" in page or b"I'll help" in page


# --- Written consent ------------------------------------------------------------------------


def test_written_consent_needs_every_board_member(w):
    consent = elections.propose_consent(title="Buy salt", text="Buy 2 tons.", actor=w["board"])
    with system_access("t"):
        assert consent.signers.count() == 2
    elections.sign_consent(consent, approve=True, actor=w["board"])
    consent.refresh_from_db()
    assert consent.state == "open"
    elections.sign_consent(consent, approve=True, actor=w["board2"])
    consent.refresh_from_db()
    assert consent.state == "adopted"
    resolution = Resolution.objects.get(pk=consent.resolution_id)
    assert "written consent" in resolution.citation


def test_one_decline_fails_it_and_outsiders_cannot_sign(w):
    consent = elections.propose_consent(title="Paint", text="Paint the gate.", actor=w["board"])
    with pytest.raises(PermissionDenied):
        elections.sign_consent(consent, approve=True, actor=w["owners"][0])
    elections.sign_consent(consent, approve=False, actor=w["board2"])
    consent.refresh_from_db()
    assert consent.state == "failed"
    with pytest.raises(elections.ElectionError):
        elections.sign_consent(consent, approve=True, actor=w["board"])


# --- Regression review fixes (SEC-001, SEC-003, BUG-004, BUG-010..012, SEC-010) ------------


def test_ballot_is_fixed_once_voting_opens(w):
    from django.db import IntegrityError, InternalError, ProgrammingError, transaction

    a, b, c, _ = w["owners"]
    late = elections.nominate(w["e"], person=_person(c), actor=b)  # not accepted yet
    election = _ready(w, a, b)
    cand_a = elections.ballot_candidates(election).get(person=_person(a))
    with pytest.raises(elections.ElectionError):
        elections.withdraw_candidate(cand_a, actor=w["board"])
    with pytest.raises(elections.ElectionError):
        elections.withdraw_candidate(cand_a, actor=a)
    with pytest.raises(elections.ElectionError):
        elections.accept_nomination(late, actor=c)
    from governance.models import Candidate

    with pytest.raises((InternalError, IntegrityError, ProgrammingError)), transaction.atomic():
        Candidate.objects.filter(pk=cand_a.pk).update(withdrawn=True)


def test_withdrawal_during_nominations_is_audited(w):
    from audit.models import AuditEvent

    a = w["owners"][0]
    cand = elections.nominate(w["e"], person=_person(a), actor=w["board"])
    elections.accept_nomination(cand, actor=a)
    elections.withdraw_candidate(cand, actor=a)
    assert AuditEvent.objects.filter(action="election.candidate_accepted").exists()
    assert AuditEvent.objects.filter(action="election.candidate_withdrawn").exists()


def test_selling_the_lot_ends_the_proxy(w):
    from community.models import Ownership

    a, _b, c, d = w["owners"]
    election = _ready(w, a)
    lot_d = elections.lots_actor_may_vote_for(election, d)[0]["lot"]
    elections.grant_proxy(w["meeting"], lot=lot_d, holder=_person(c), actor=d)
    with system_access("t"):
        ownership = Ownership.objects.active().get(lot=lot_d)
    community.end_ownership(ownership, end_date=timezone.localdate(), actor=w["board"])
    assert lot_d.pk not in [r["lot"].pk for r in elections.lots_actor_may_vote_for(election, c)]
    gov.record_attendance(w["meeting"], _person(c), actor=w["board"])
    assert elections.quorum_status(w["meeting"])["by_proxy"] == 0


def test_a_proxy_holder_who_sells_can_no_longer_vote_with_it(client, w):
    """Review BUG-002: the holder half of the proxy rule."""
    from community.models import Ownership

    a, _b, c, d = w["owners"]
    election = _ready(w, a)
    lot_d = elections.lots_actor_may_vote_for(election, d)[0]["lot"]
    elections.grant_proxy(w["meeting"], lot=lot_d, holder=_person(c), actor=d)
    gov.record_attendance(w["meeting"], _person(c), actor=w["board"])
    assert elections.quorum_status(w["meeting"])["by_proxy"] == 1
    with system_access("t"):
        ownership = Ownership.objects.active().get(person=_person(c))
    community.end_ownership(ownership, end_date=timezone.localdate(), actor=w["board"])
    # c is now a read-only former owner: the proxy is neither usable nor counted.
    assert elections.lots_actor_may_vote_for(election, c) == []
    assert elections.quorum_status(w["meeting"])["by_proxy"] == 0
    candidate = elections.ballot_candidates(election).first()
    with pytest.raises((elections.ElectionError, PermissionDenied)):
        elections.cast_election_ballot(
            election, lot_id=lot_d.pk, candidate_ids=[candidate.pk], actor=c
        )
    with system_access("t"):
        assert not ElectionParticipation.objects.filter(election=election).exists()


def test_a_buyer_is_not_told_the_sellers_election_ballot_was_theirs(client, w):
    """Reviews BUG-004, R2-010: only ballots cast in the viewer's ownership."""
    from community.models import Ownership

    a, _b, c, _d = w["owners"]
    election = _ready(w, a)
    lot_c = elections.lots_actor_may_vote_for(election, c)[0]["lot"]
    # The seller's ballot two days ago (written directly to set its date).
    ElectionParticipation.objects.create(
        election=election,
        lot=lot_c,
        cast_by=c,
        cast_at=timezone.now() - datetime.timedelta(days=2),
    )
    client.force_login(c)
    page = client.get(reverse("election_detail", args=[election.pk])).content.decode()
    assert f"{lot_c.label}: your ballot was recorded" in page
    assert "Your ballot was recorded" in client.get(reverse("voting")).content.decode()

    with system_access("t"):
        ownership = Ownership.objects.active().get(lot=lot_c)
    community.end_ownership(ownership, end_date=timezone.localdate(), actor=w["board"])
    buyer = make_member()
    community.record_ownership(
        person=_person(buyer), lot=lot_c, start_date=timezone.localdate(), actor=w["board"]
    )
    assert elections.my_lots_participation(election, buyer)[0] == []
    client.force_login(buyer)
    page = client.get(reverse("election_detail", args=[election.pk])).content.decode()
    assert "your ballot was recorded" not in page
    assert "a ballot was already cast for this home before you owned it" in page
    assert "Your ballot was recorded" not in client.get(reverse("voting")).content.decode()


def test_bad_form_values_are_refused_not_500(w):
    a, _b, c, _ = w["owners"]
    election = _ready(w, a)
    with pytest.raises(elections.ElectionError):
        elections.cast_election_ballot(election, lot_id="x", candidate_ids=["y"], actor=c)


def test_leap_day_term_end():
    assert elections._add_years(datetime.date(2028, 2, 29), 2) == datetime.date(2030, 2, 28)


def test_any_board_member_can_withdraw_a_stuck_consent(w):
    consent = elections.propose_consent(title="Fence", text="Mend it.", actor=w["board"])
    elections.withdraw_consent(consent, actor=w["board2"])
    consent.refresh_from_db()
    assert consent.state == "withdrawn"


def test_nomination_list_shows_addresses_not_names(client, w):
    from community.models import Lot
    from core import policy

    a, b = w["owners"][:2]
    client.force_login(a)
    page = client.get(reverse("election_detail", args=[w["e"].pk])).content.decode()
    assert _person(b).display_name not in page
    lot_b = Lot.objects.unscoped("t").get(pk=next(iter(policy.roles_for(b).owned_lot_ids)))
    assert lot_b.street_address in page
    client.post(reverse("election_action", args=[w["e"].pk, "nominate"]), {"lot": lot_b.pk})
    page = client.get(reverse("election_detail", args=[w["e"].pk])).content.decode()
    assert _person(b).display_name in page


def test_a_consent_waits_in_my_items_until_each_board_member_answers(w):
    from core.my_items import items_for

    def waiting(user):
        return [i for i in items_for(user) if i.area == "Written consent"]

    consent = elections.propose_consent(title="Buy salt", text="Buy 2 tons.", actor=w["board"])
    assert [i.title for i in waiting(w["board"])] == ["Buy salt"]
    assert [i.title for i in waiting(w["board2"])] == ["Buy salt"]
    assert waiting(w["owners"][0]) == []
    elections.sign_consent(consent, approve=True, actor=w["board"])
    assert waiting(w["board"]) == [] and len(waiting(w["board2"])) == 1
    elections.sign_consent(consent, approve=True, actor=w["board2"])
    assert waiting(w["board2"]) == []  # adopted: nothing left to answer
