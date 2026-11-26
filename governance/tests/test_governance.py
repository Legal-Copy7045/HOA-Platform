from datetime import date, timedelta

import pytest
from django.core import mail
from django.core.exceptions import PermissionDenied
from django.utils import timezone

from community import services as community
from community.models import OwnershipType, Person, Role
from community.tests.factories import LotFactory, make_member
from core.append_only import AppendOnlyError
from core.policy import SYSTEM
from core.workflow import WorkflowError
from governance import services
from governance.models import Ballot, Meeting, Vote, VoteKind
from governance.tests.helpers import legacy_poll

pytestmark = pytest.mark.django_db


@pytest.fixture
def board():
    return make_member(board=True)


def soon(days=30):
    return timezone.now() + timedelta(days=days)


# --- Meetings ---------------------------------------------------------------------


def test_notice_emails_consenting_owners_and_lists_the_rest(board):
    consenting = make_member(lots=[LotFactory()], email="yes@example.com")
    community.update_my_profile(user=consenting, electronic_notice_consent=True)
    make_member(lots=[LotFactory()], email="no@example.com")
    meeting = services.create_meeting(
        title="Annual meeting", kind="annual", location="Clubhouse", starts_at=soon(), actor=board
    )
    mail.outbox = []
    result = services.send_notice(meeting, actor=board)
    assert [m.to[0] for m in mail.outbox] == ["yes@example.com"]
    assert result["emailed"] == 1 and len(result["paper"]) == 1


def test_notice_shortfall_against_the_rule(board):
    community.set_rule(
        key="meeting.owner_notice_days", value="10", citation="Bylaws §3.4", actor=board
    )
    meeting = services.create_meeting(
        title="Special", kind="special", location="Clubhouse", starts_at=soon(3), actor=board
    )
    assert services.notice_shortfall_days(meeting) == 7


def test_executive_sessions_are_hidden_from_owners_and_never_noticed(board):
    meeting = services.create_meeting(
        title="Exec", kind="executive", location="Clubhouse", starts_at=soon(), actor=board
    )
    owner = make_member(lots=[LotFactory()])
    assert not Meeting.objects.for_user(owner).filter(pk=meeting.pk).exists()
    with pytest.raises(services.GovernanceError):
        services.send_notice(meeting, actor=board)


# --- Resolutions -------------------------------------------------------------------


def test_resolutions_are_numbered_permanent_and_superseded(board):
    first = services.record_resolution(
        kind="policy",
        title="Trash cans",
        text="Out of sight.",
        adopted_on=date(2026, 3, 1),
        votes_for=5,
        actor=board,
    )
    assert first.number == "2026-01"
    with pytest.raises(AppendOnlyError):
        first.delete()
    second = services.record_resolution(
        kind="policy",
        title="Trash cans (amended)",
        text="Out of sight by 8pm.",
        adopted_on=date(2026, 6, 1),
        supersedes=first,
        actor=board,
    )
    assert list(services.in_force()) == [second]
    with pytest.raises(services.GovernanceError):
        services.record_resolution(
            kind="policy",
            title="x",
            text="y",
            adopted_on=date(2026, 7, 1),
            supersedes=first,
            actor=board,
        )


def test_only_the_board_records_resolutions():
    with pytest.raises(PermissionDenied):
        services.record_resolution(
            kind="policy",
            title="x",
            text="y",
            adopted_on=date(2026, 1, 1),
            actor=make_member(roles=[Role.MANAGER]),
        )


# --- Records requests -----------------------------------------------------------------


def test_records_request_workflow(board):
    community.set_rule(key="records.response_days", value="30", citation="Bylaws §8", actor=board)
    owner = make_member(lots=[LotFactory()])
    other_owner = make_member(lots=[LotFactory()])
    manager = make_member(roles=[Role.MANAGER])
    request = services.submit_records_request(description="2025 budget", actor=owner)
    assert request.due_on == timezone.localdate() + timedelta(days=30)
    with pytest.raises(PermissionDenied):
        services.RECORDS.apply(request, "fulfil", actor=other_owner, note="x")
    with pytest.raises(WorkflowError, match="note"):
        services.RECORDS.apply(request, "fulfil", actor=manager)
    services.RECORDS.apply(request, "acknowledge", actor=manager)
    services.RECORDS.apply(request, "fulfil", actor=manager, note="Emailed the PDF")
    request.refresh_from_db()
    assert request.state == "fulfilled"
    assert [e.action for e in services.RECORDS.history(request)] == ["acknowledge", "fulfil"]


def test_only_the_requester_withdraws(board):
    owner = make_member(lots=[LotFactory()])
    request = services.submit_records_request(description="minutes", actor=owner)
    other = make_member(lots=[LotFactory()])
    with pytest.raises(WorkflowError, match="Only the person"):
        services.RECORDS.apply(request, "withdraw", actor=other)
    services.RECORDS.apply(request, "withdraw", actor=owner)


# --- Votes ---------------------------------------------------------------------------


def _vote(board, kind=VoteKind.INFORMAL, **kw):
    if kind == VoteKind.INFORMAL:
        vote = legacy_poll(board, **kw)
    else:
        vote = services.create_vote(
            title="Pond fountain?", kind=kind, options=["Yes", "No"], actor=board, **kw
        )
    return vote, list(vote.options.all())


def test_informal_poll_one_ballot_per_owner_and_ballots_never_change(board):
    owner = make_member(lots=[LotFactory()])
    vote, (yes, _) = _vote(board)
    services.open_vote(vote, actor=board)
    ballot = services.cast_ballot(vote, option_id=yes.pk, actor=owner)
    with pytest.raises(services.GovernanceError):
        services.cast_ballot(vote, option_id=yes.pk, actor=owner)
    with pytest.raises(AppendOnlyError):
        ballot.delete()


def test_ballot_with_malformed_option_or_lot_gets_a_message_not_500(client, board):
    """Review BUG-007."""
    from django.urls import reverse

    owner = make_member(lots=[LotFactory()])
    vote, (yes, _) = _vote(board)
    services.open_vote(vote, actor=board)
    client.force_login(owner)
    url = reverse("vote_action", args=[vote.pk, "cast"])
    for bad in ("abc", "²", "9" * 30):
        response = client.post(url, {"option": bad}, follow=True)
        assert response.status_code == 200
        assert b"Choose one of the options" in response.content
    assert not Ballot.objects.filter(vote=vote).exists()
    # A garbled lot field is refused or ignored, never a 500 (an informal
    # poll has no lot, so the ballot is recorded).
    response = client.post(url, {"option": str(yes.pk), "lot": "x"}, follow=True)
    assert response.status_code == 200
    assert Ballot.objects.filter(vote=vote).count() == 1


def test_formal_vote_needs_rules_and_counts_one_ballot_per_lot(board):
    vote, (yes, no) = _vote(board, VoteKind.FORMAL)
    with pytest.raises(services.GovernanceError, match="quorum"):
        services.open_vote(vote, actor=board)
    community.set_rule(key="vote.quorum_percent", value="50", citation="Bylaws §2.6", actor=board)
    community.set_rule(key="vote.pass_percent", value="67", citation="Declaration §14", actor=board)
    shared_lot = LotFactory()
    a = make_member()
    b = make_member()
    for user in (a, b):
        community.record_ownership(
            person=Person.objects.unscoped("t").get(user=user),
            lot=shared_lot,
            start_date=date(2020, 1, 1),
            ownership_type=OwnershipType.JOINT,
            actor=SYSTEM,
        )
    vote = services.open_vote(vote, actor=board)
    assert vote.quorum_percent == 50 and "Bylaws §2.6" in vote.rules_citation
    services.cast_ballot(vote, option_id=yes.pk, lot_id=shared_lot.pk, actor=a)
    with pytest.raises(services.GovernanceError):  # the co-owner: lot already voted
        services.cast_ballot(vote, option_id=no.pk, lot_id=shared_lot.pk, actor=b)
    result = services.results(vote)
    assert result["total"] == 1 and result["outcome"]["turnout"] > 0


def test_manager_and_auditor_cannot_vote(board):
    vote, (yes, _) = _vote(board)
    services.open_vote(vote, actor=board)
    for roles in ([Role.MANAGER], [Role.AUDITOR]):
        with pytest.raises(PermissionDenied):
            services.cast_ballot(vote, option_id=yes.pk, actor=make_member(roles=roles))


def test_secret_ballots_hide_voters_from_the_board(board):
    vote, _ = _vote(board, secret=True)
    assert not services.can_see_voters(vote, board)
    open_vote, _ = _vote(board)
    assert services.can_see_voters(open_vote, board)


def test_closed_vote_takes_no_ballots(board):
    owner = make_member(lots=[LotFactory()])
    vote, (yes, _) = _vote(board)
    services.open_vote(vote, actor=board)
    services.close_vote(vote, actor=board)
    with pytest.raises(services.GovernanceError):
        services.cast_ballot(vote, option_id=yes.pk, actor=owner)
    assert Ballot.objects.count() == 0


# --- Security review 2026-11 ------------------------------------------------------


def test_ballot_from_a_page_loaded_before_close_is_refused(board):
    """D3: the vote's status was checked on a stale object, with no lock."""
    owner = make_member(lots=[LotFactory()])
    vote, (yes, _) = _vote(board)
    vote = services.open_vote(vote, actor=board)
    stale = Vote.objects.get(pk=vote.pk)  # loaded while open
    services.close_vote(vote, actor=board)
    with pytest.raises(services.GovernanceError):
        services.cast_ballot(stale, option_id=yes.pk, actor=owner)


def _formal_with_ballots(board, yes_count, no_count, quorum="10", passing="50"):
    community.set_rule(key="vote.quorum_percent", value=quorum, citation="Bylaws §2.6", actor=board)
    community.set_rule(key="vote.pass_percent", value=passing, citation="Decl. §14", actor=board)
    voters = []
    for _ in range(yes_count + no_count):
        lot = LotFactory()
        voters.append((lot, make_member(lots=[lot])))
    vote, (yes, no) = _vote(board, VoteKind.FORMAL)
    vote = services.open_vote(vote, actor=board)
    for i, (lot, member) in enumerate(voters):
        option = yes if i < yes_count else no
        services.cast_ballot(vote, option_id=option.pk, lot_id=lot.pk, actor=member)
    return vote


def test_a_tie_passes_nothing(board):
    """D4: a 1-1 vote at a 50% threshold was reported as passing."""
    vote = _formal_with_ballots(board, 1, 1)
    outcome = services.results(vote)["outcome"]
    assert outcome["leading"] == "tie"
    assert outcome["meets_threshold"] is False


def test_threshold_uses_exact_counts_not_rounded_percentages(board):
    # 2 of 3 is 66.67%: rounds to 66.7, which must not satisfy a 67% threshold...
    vote = _formal_with_ballots(board, 2, 1, passing="67")
    assert services.results(vote)["outcome"]["meets_threshold"] is False


def test_open_vote_rules_are_fixed_in_the_database(board):
    """D6."""
    from django.db import IntegrityError, transaction

    vote = _formal_with_ballots(board, 1, 0)
    with pytest.raises(IntegrityError), transaction.atomic():
        Vote.objects.filter(pk=vote.pk).update(pass_percent=1)


# --- Decision D21: secret ballots are unlinkable in the database ----------------------


def test_secret_ballot_choice_is_not_stored_with_the_voter(board):
    from django.db import IntegrityError, connection, transaction

    from governance.models import SecretChoice

    owners = [make_member(lots=[LotFactory()]) for _ in range(3)]
    vote, (yes, no) = _vote(board, secret=True)
    services.open_vote(vote, actor=board)
    services.cast_ballot(vote, option_id=yes.pk, actor=owners[0])
    services.cast_ballot(vote, option_id=yes.pk, actor=owners[1])
    services.cast_ballot(vote, option_id=no.pk, actor=owners[2])
    # Who voted is known (one ballot each), what they chose is not.
    assert Ballot.objects.filter(vote=vote).count() == 3
    assert not Ballot.objects.filter(vote=vote, option__isnull=False).exists()
    # The choices carry no voter, lot, time or sequential id.
    fields = {f.name for f in SecretChoice._meta.get_fields()}
    assert fields == {"id", "vote", "option"}
    counts = {r["option"].label: r["count"] for r in services.results(vote)["rows"]}
    assert counts == {"Yes": 2, "No": 1}
    # The database itself refuses a secret ballot that records the choice.
    with pytest.raises(IntegrityError), transaction.atomic(), connection.cursor() as cur:
        cur.execute(
            "INSERT INTO governance_ballot (vote_id, option_id, person_id, cast_by_id, cast_at)"
            " SELECT %s, %s, person_id, cast_by_id, now() FROM governance_ballot LIMIT 1",
            [vote.pk, yes.pk],
        )


def test_open_ballots_still_record_the_choice(board):
    owner = make_member(lots=[LotFactory()])
    vote, (yes, _) = _vote(board)
    services.open_vote(vote, actor=board)
    ballot = services.cast_ballot(vote, option_id=yes.pk, actor=owner)
    assert ballot.option_id == yes.pk


def test_a_buyer_does_not_see_how_the_seller_voted(board):
    """Review R2-001."""
    from community.models import Ownership

    vote, (yes, _no) = _vote(board, VoteKind.FORMAL)
    community.set_rule(key="vote.quorum_percent", value="10", citation="Bylaws", actor=board)
    community.set_rule(key="vote.pass_percent", value="50", citation="Bylaws", actor=board)
    lot = LotFactory()
    seller = make_member()
    community.record_ownership(
        person=Person.objects.unscoped("t").get(user=seller),
        lot=lot,
        start_date=date(2020, 1, 1),
        actor=SYSTEM,
    )
    vote = services.open_vote(vote, actor=board)
    # A ballot the seller cast two days ago (written directly to set its date).
    Ballot.objects.create(
        vote=vote,
        option=yes,
        person=Person.objects.unscoped("t").get(user=seller),
        lot=lot,
        cast_by=seller,
        cast_at=timezone.now() - timedelta(days=2),
    )
    assert services.my_ballots(vote, seller)
    ownership = Ownership.objects.unscoped("t").get(lot=lot)
    community.end_ownership(ownership, end_date=timezone.localdate(), actor=SYSTEM)
    buyer = make_member()
    community.record_ownership(
        person=Person.objects.unscoped("t").get(user=buyer),
        lot=lot,
        start_date=timezone.localdate(),
        actor=SYSTEM,
    )
    assert services.my_ballots(vote, buyer) == []
