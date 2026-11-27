"""D68: a participation can never be correlated with its choice through
anything the database persists.

The casting transaction still writes the participation and the pooled choice
together (one vote per home stays atomic, no ballot is lost), so the pooled
row shares the participation's transaction id, WAL position and dump order.
These tests show that this link reaches only an opaque, fixed-size sealed row
that nothing in the database can open, that the mix cuts every tie between a
sealed row and a written choice (kept rows are sealed again), and that no
transaction id, id, timestamp, order, job row or audit row links a
participation to a permanent choice. They also show what integrity costs
nothing: duplicates, concurrent submissions, failures after the participation
and a lost key never drop or double a ballot.

The tests marked COMMITTED use real committed transactions, as in production
(each cast in its own request, the flush in the worker)."""

import threading
from datetime import timedelta

import pytest
from django.db import connection
from django.utils import timezone

from audit.models import AuditEvent
from community import services as community
from community.models import Person
from community.tests.factories import LotFactory, make_member
from core.scoping import system_access
from governance import elections, mixing, sealing, services, surveys
from governance.models import (
    Ballot,
    PendingSecretChoice,
    SecretChoice,
    VoteKind,
)
from governance.tests.helpers import close_early, window_kw

COMMITTED = pytest.mark.django_db(transaction=True, serialized_rollback=True)


@pytest.fixture(scope="module", autouse=True)
def _allow_flush(django_db_setup, django_db_blocker):
    """Committed tests empty the tables with TRUNCATE afterwards, which the
    append-only triggers refuse: switch them off for this module only."""
    with django_db_blocker.unblock(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT DISTINCT c.relname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE NOT t.tgisinternal AND n.nspname = current_schema()"
        )
        tables = [row[0] for row in cursor.fetchall()]
        for table in tables:
            cursor.execute(f'ALTER TABLE "{table}" DISABLE TRIGGER USER')  # nosec B608
    yield
    with django_db_blocker.unblock(), connection.cursor() as cursor:
        for table in tables:
            cursor.execute(f'ALTER TABLE "{table}" ENABLE TRIGGER USER')  # nosec B608


@pytest.fixture(autouse=True)
def _no_worker(monkeypatch):
    """Flushes happen only where a test calls them, each in its own transaction."""
    monkeypatch.setattr(mixing, "defer_flush", lambda kind, pk: None)


def _rows(sql, params):
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.fetchall()


def _owners(n):
    out = []
    for _ in range(n):
        lot = LotFactory()
        out.append((lot, make_member(lots=[lot])))
    return out


def _secret_vote(board, labels):
    community.set_rule(key="vote.quorum_percent", value="10", citation="Bylaws §2.6", actor=board)
    community.set_rule(key="vote.pass_percent", value="50", citation="Decl. §14", actor=board)
    vote = services.create_vote(
        title="Fountain?", kind=VoteKind.FORMAL, options=labels, secret=True, actor=board
    )
    vote = services.open_vote(vote, actor=board, **window_kw())
    return vote, list(vote.options.all())


def _pool_snapshot(vote_id):
    """What a database reader, a dump or the WAL holds for the pool."""
    return _rows(
        "SELECT xmin::text, ctid::text, id::text, sort_key, sealed"
        " FROM governance_pendingsecretchoice WHERE vote_id = %s ORDER BY ctid",
        [vote_id],
    )


def _participation_xmins(vote_id):
    return {
        row[0]: row[1]
        for row in _rows(
            "SELECT xmin::text, lot_id FROM governance_ballot WHERE vote_id = %s", [vote_id]
        )
    }


# --- The casting-window link reaches only an opaque row -----------------------------------


@COMMITTED
def test_a_snapshot_during_voting_links_each_home_only_to_an_opaque_row(settings):
    settings.SECRET_BALLOT_MIX_MIN = 10  # nothing flushes while we look
    board = make_member(board=True)
    vote, (yes, no) = _secret_vote(board, ["Yes", "No"])
    voters = _owners(4)
    picks = [yes, yes, no, yes]
    for (lot, user), option in zip(voters, picks, strict=True):
        services.cast_ballot(vote, option_id=option.pk, lot_id=lot.pk, actor=user)

    pool = _pool_snapshot(vote.pk)
    by_xmin = _participation_xmins(vote.pk)
    # The link the casting transaction must keep (atomic one-vote-per-home):
    # each pooled row shares its xmin with exactly one home's ballot ...
    assert sorted(by_xmin) == sorted(row[0] for row in pool)
    # ... but what it reaches says nothing: one size for every choice, no
    # plaintext, and two homes that chose alike get unrelated bytes.
    sealed = [bytes(row[4]) for row in pool]
    assert len({len(blob) for blob in sealed}) == 1
    assert len(set(sealed)) == 4
    for blob in sealed:
        assert b"option" not in blob
        for option in (yes, no):
            assert sealing.encode({"option": option.pk}) not in blob
    # Nothing in the database opens them: without the server's key, every
    # row is refused, so the snapshot alone does not determine any choice.
    real_key = settings.SECRET_KEY
    settings.SECRET_KEY, settings.SECRET_KEY_FALLBACKS = "not-the-server-key-" * 3, []
    for blob in sealed:
        with pytest.raises(sealing.PoolKeyError):
            sealing.unseal("vote", vote.pk, blob)
    settings.SECRET_KEY = real_key
    opened = sorted(sealing.unseal("vote", vote.pk, blob)["option"] for blob in sealed)
    assert opened == sorted(o.pk for o in picks)
    # The ballot rows hold no choice either.
    assert not Ballot.objects.filter(vote=vote, option__isnull=False).exists()


@COMMITTED
def test_kept_choices_are_sealed_again_and_lose_every_tie_to_their_cast(settings):
    settings.SECRET_BALLOT_MIX_MIN = 2
    board = make_member(board=True)
    vote, (yes, no) = _secret_vote(board, ["Yes", "No"])
    for i, (lot, user) in enumerate(_owners(4)):
        services.cast_ballot(vote, option_id=(yes, no)[i % 2].pk, lot_id=lot.pk, actor=user)
    before = _pool_snapshot(vote.pk)
    ballots = _participation_xmins(vote.pk)

    assert mixing.flush("vote", vote.pk) == 2  # its own committed transaction

    after = _pool_snapshot(vote.pk)
    assert len(after) == 2
    # Integrity: written plus pooled always equals the ballots cast.
    assert SecretChoice.objects.filter(vote=vote).count() + len(after) == len(ballots) == 4
    old = {field for row in before for field in (row[0], row[2], row[3], bytes(row[4]))}
    for xmin, _ctid, row_id, sort_key, blob in after:
        assert xmin not in ballots  # no longer the casting transaction
        assert row_id not in old and sort_key not in old and bytes(blob) not in old
    written = {
        row[0]
        for row in _rows(
            "SELECT xmin::text FROM governance_secretchoice WHERE vote_id = %s", [vote.pk]
        )
    }
    assert written == {after[0][0]} == {after[1][0]}  # one flush transaction for all
    assert not written & set(ballots)


@COMMITTED
def test_no_persisted_relationship_links_a_participation_to_its_choice(settings):
    """After close: transaction ids, ids, times, order, jobs and audit rows."""
    settings.SECRET_BALLOT_MIX_MIN = 2
    board = make_member(board=True)
    labels = [f"Option {n}" for n in range(6)]
    vote, options = _secret_vote(board, labels)
    voters = _owners(6)
    for (lot, user), option in zip(voters, options, strict=True):  # home i picks option i
        services.cast_ballot(vote, option_id=option.pk, lot_id=lot.pk, actor=user)
        mixing.flush("vote", vote.pk)  # the worker, after each cast commits
    close_early(vote, board)
    assert PendingSecretChoice.objects.count() == 0

    ballots = _rows(
        "SELECT xmin::text, id, lot_id, cast_at FROM governance_ballot"
        " WHERE vote_id = %s ORDER BY id",
        [vote.pk],
    )
    choices = _rows(
        "SELECT xmin::text, ctid::text, id::text, option_id FROM governance_secretchoice"
        " WHERE vote_id = %s ORDER BY ctid",
        [vote.pk],
    )
    assert len(ballots) == len(choices) == 6
    ballot_xmins = {row[0] for row in ballots}
    # Transaction ids: no choice was written by any casting transaction, and
    # every flush wrote at least K choices together.
    assert not ballot_xmins & {row[0] for row in choices}
    batches = {}
    for row in choices:
        batches.setdefault(row[0], []).append(row)
    assert all(len(batch) >= 2 for batch in batches.values())
    # Columns: a choice row holds its vote, its option and a random UUID; no
    # time, sequence, home, person, ballot or pool id.
    columns = {
        row[0]
        for row in _rows(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_name = 'governance_secretchoice'",
            [],
        )
    }
    assert columns == {"id", "vote_id", "option_id"}
    ballot_values = {str(v) for row in ballots for v in row[1:]}
    assert not {row[2] for row in choices} & ballot_values
    # Order: neither the physical order nor the id order follows the casting
    # order (home i picked option i).
    cast_order = [o.pk for o in options]
    assert sorted(row[3] for row in choices) == sorted(cast_order)
    by_id = [row[3] for row in sorted(choices, key=lambda row: row[2])]
    assert [row[3] for row in choices] != cast_order or by_id != cast_order
    # Jobs: the flush job, if queued, names only the vote.
    from procrastinate.contrib.django.models import ProcrastinateJob

    for job in ProcrastinateJob.objects.filter(task_name__startswith="governance.tasks.flush"):
        assert set(job.args) == {"kind", "target_id"}
    # Audit: cast events name the home, never a choice or a pool or choice id,
    # and no audit row was written by a flush transaction.
    choice_ids = {row[2] for row in choices}
    for event in AuditEvent.objects.filter(action="vote.ballot_cast"):
        text = str(event.metadata)
        assert "option" not in text and not any(cid in text for cid in choice_ids)
    audit_xmins = {row[0] for row in _rows("SELECT xmin::text FROM audit_auditevent", [])}
    assert not audit_xmins & set(batches)
    counts = {r["option"].label: r["count"] for r in services.results(vote)["rows"]}
    assert counts == dict.fromkeys(labels, 1)


@COMMITTED
def test_election_and_survey_pools_are_sealed_too(settings):
    settings.SECRET_BALLOT_MIX_MIN = 10
    board = make_member(board=True)
    election = elections.create_election(title="2027 election", seats=3, actor=board)
    for _lot, user in _owners(3):
        with system_access("t"):
            person = Person.objects.get(user=user)
        elections.nominate(election, person=person, actor=user)
    election = elections.open_voting(election, actor=board, **window_kw())
    cands = list(elections.ballot_candidates(election).order_by("pk"))
    voters = _owners(2)
    elections.cast_election_ballot(  # one pick and three picks: same size
        election, lot_id=voters[0][0].pk, candidate_ids=[cands[0].pk], actor=voters[0][1]
    )
    elections.cast_election_ballot(
        election, lot_id=voters[1][0].pk, candidate_ids=[c.pk for c in cands], actor=voters[1][1]
    )
    today = timezone.localdate()
    survey = surveys.create_survey(
        title="Pool hours",
        intro="",
        audience="owners",
        opens_on=today,
        closes_on=today + timedelta(days=7),
        anonymous=True,
        share_results=False,
        actor=board,
    )
    question = surveys.add_question(survey, kind="text", prompt="Anything else?", actor=board)
    surveys.open_survey(survey, actor=board)
    surveys.submit_response(survey, answers={question.pk: ["ok"]}, actor=voters[0][1])
    marker = "Quokka-" + "x" * 900 + "\U0001f986"  # long, and outside the BMP
    surveys.submit_response(survey, answers={question.pk: [marker]}, actor=voters[1][1])

    for column, target_id in (("election_id", election.pk), ("survey_id", survey.pk)):
        blobs = [
            bytes(row[0])
            for row in _rows(
                f"SELECT sealed FROM governance_pendingsecretchoice WHERE {column} = %s",  # nosec B608
                [target_id],
            )
        ]
        assert len(blobs) == 2 and len({len(b) for b in blobs}) == 1
        assert not any(b"Quokka" in b or b"candidates" in b for b in blobs)
    close_early(election, board)
    surveys.close_survey(survey, actor=board)
    assert PendingSecretChoice.objects.count() == 0
    votes = {r["candidate"].pk: r["votes"] for r in elections.tally(election)["rows"]}
    assert votes == {cands[0].pk: 2, cands[1].pk: 1, cands[2].pk: 1}
    texts = surveys.results(survey, include_text=True)["rows"][0]["texts"]
    assert sorted(t["text"] for t in texts) == sorted(["ok", marker])


# --- Integrity is unchanged ---------------------------------------------------------------


@COMMITTED
def test_concurrent_submissions_for_one_home_record_one_ballot_and_one_choice(settings):
    settings.SECRET_BALLOT_MIX_MIN = 10
    board = make_member(board=True)
    vote, (yes, no) = _secret_vote(board, ["Yes", "No"])
    ((lot, user),) = _owners(1)
    barrier = threading.Barrier(2)
    outcomes = []

    def cast(option):
        try:
            barrier.wait()
            services.cast_ballot(vote, option_id=option.pk, lot_id=lot.pk, actor=user)
            outcomes.append("cast")
        except services.GovernanceError:
            outcomes.append("refused")
        finally:
            connection.close()

    threads = [threading.Thread(target=cast, args=(o,)) for o in (yes, no)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(outcomes) == ["cast", "refused"]
    assert Ballot.objects.filter(vote=vote).count() == 1
    assert PendingSecretChoice.objects.filter(vote=vote).count() == 1


@pytest.mark.django_db
def test_a_failure_after_the_participation_loses_nothing_and_allows_a_retry(monkeypatch):
    board = make_member(board=True)
    vote, (yes, _no) = _secret_vote(board, ["Yes", "No"])
    ((lot, user),) = _owners(1)
    real_seal = sealing.seal

    def broken(*args, **kwargs):
        raise RuntimeError("simulated crash after the ballot row was written")

    monkeypatch.setattr(sealing, "seal", broken)
    with pytest.raises(RuntimeError):
        services.cast_ballot(vote, option_id=yes.pk, lot_id=lot.pk, actor=user)
    # The participation rolled back with the choice: the home did not "vote".
    assert not Ballot.objects.filter(vote=vote).exists()
    assert not PendingSecretChoice.objects.filter(vote=vote).exists()
    monkeypatch.setattr(sealing, "seal", real_seal)
    services.cast_ballot(vote, option_id=yes.pk, lot_id=lot.pk, actor=user)
    assert Ballot.objects.filter(vote=vote).count() == 1
    assert PendingSecretChoice.objects.filter(vote=vote).count() == 1


@pytest.mark.django_db
def test_without_the_key_nothing_is_counted_and_nothing_is_lost(settings):
    board = make_member(board=True)
    vote, (yes, no) = _secret_vote(board, ["Yes", "No"])
    for (lot, user), option in zip(_owners(3), (yes, no, yes), strict=True):
        services.cast_ballot(vote, option_id=option.pk, lot_id=lot.pk, actor=user)
    old_key = settings.SECRET_KEY
    settings.SECRET_KEY, settings.SECRET_KEY_FALLBACKS = "rotated-without-fallback-" * 3, []
    with pytest.raises(sealing.PoolKeyError):
        close_early(vote, board)  # refuses to close on a partial count
    vote.refresh_from_db()
    assert vote.status == "open"
    assert PendingSecretChoice.objects.filter(vote=vote).count() == 3
    assert SecretChoice.objects.filter(vote=vote).count() == 0
    settings.SECRET_KEY_FALLBACKS = [old_key]  # the documented rotation
    close_early(vote, board)
    counts = {r["option"].label: r["count"] for r in services.results(vote)["rows"]}
    assert counts == {"Yes": 2, "No": 1}


@pytest.mark.django_db
def test_a_sealed_choice_opens_only_for_its_own_vote():
    blob = sealing.seal("vote", 7, {"option": 3}, sealing.vote_bound())
    assert sealing.unseal("vote", 7, blob) == {"option": 3}
    for kind, target in (("vote", 8), ("election", 7), ("survey", 7)):
        with pytest.raises(sealing.PoolKeyError):
            sealing.unseal(kind, target, blob)
    tampered = blob[:-1] + bytes([blob[-1] ^ 1])
    with pytest.raises(sealing.PoolKeyError):
        sealing.unseal("vote", 7, tampered)


def test_padding_hides_what_was_chosen_and_never_truncates():
    vote = {sealing.seal("vote", 1, {"option": o}, sealing.vote_bound()) for o in (1, 10**18)}
    assert len({len(b) for b in vote}) == 1
    size = sealing.election_bound(5)
    picks = [[1], [10**18] * 5]
    assert len({len(sealing.seal("election", 1, {"candidates": p}, size)) for p in picks}) == 1
    size = sealing.survey_bound([("text", 0), ("multiple", 4), ("rating", 0)], 1000)
    long_text = "\U0001f986" * 1000
    answers = [
        [{"question": 1, "choice": None, "rating": None, "text": ""}],
        [{"question": 1, "choice": None, "rating": None, "text": long_text}]
        + [{"question": 2, "choice": 10**18, "rating": None, "text": ""}] * 4
        + [{"question": 3, "choice": None, "rating": 5, "text": ""}],
    ]
    blobs = [sealing.seal("survey", 1, {"answers": a}, size) for a in answers]
    assert len({len(b) for b in blobs}) == 1
    with pytest.raises(ValueError):
        sealing.seal("vote", 1, {"option": 1, "extra": "x" * 100}, sealing.vote_bound())
