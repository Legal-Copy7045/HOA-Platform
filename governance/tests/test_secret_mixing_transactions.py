"""D56: after mixing, no permanent choice row shares a transaction id
(``xmin``) with any participation row. Each cast and each flush is a real
committed transaction here, as in production (casts in web requests, the
flush in the worker after the cast commits)."""

from datetime import timedelta

import pytest
from django.db import connection
from django.utils import timezone

from community import services as community
from community.models import Person
from community.tests.factories import LotFactory, make_member
from core.scoping import system_access
from governance import elections, mixing, services, surveys
from governance.models import PendingSecretChoice, VoteKind

# These tests commit, so Django empties the tables afterwards with TRUNCATE.
# The append-only triggers refuse TRUNCATE; switch them off for this module
# only (the test database's owner may) and back on at the end.
COMMITTED = pytest.mark.django_db(transaction=True, serialized_rollback=True)


@pytest.fixture(scope="module", autouse=True)
def _allow_flush(django_db_setup, django_db_blocker):
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
def _worker(settings, monkeypatch):
    """The worker's flush, run right after each cast commits (in its own
    transaction, as Procrastinate would), with a small K so open-time
    batches happen too."""
    settings.SECRET_BALLOT_MIX_MIN = 2
    monkeypatch.setattr(mixing, "defer_flush", lambda kind, pk: mixing.flush(kind, pk))


def _xmins(table, column, target_id):
    with connection.cursor() as cursor:
        cursor.execute(
            f"SELECT xmin::text FROM {table} WHERE {column} = %s",  # nosec B608
            [target_id],
        )
        return [row[0] for row in cursor.fetchall()]


def _owners(n):
    out = []
    for _ in range(n):
        lot = LotFactory()
        out.append((lot, make_member(lots=[lot])))
    return out


@COMMITTED
def test_secret_vote_choices_share_no_transaction_with_ballots():
    board = make_member(board=True)
    community.set_rule(key="vote.quorum_percent", value="10", citation="Bylaws §2.6", actor=board)
    community.set_rule(key="vote.pass_percent", value="50", citation="Decl. §14", actor=board)
    vote = services.create_vote(
        title="Fountain?", kind=VoteKind.FORMAL, options=["Yes", "No"], secret=True, actor=board
    )
    vote = services.open_vote(vote, actor=board)
    yes, no = list(vote.options.all())
    voters = _owners(7)
    lot, user = voters[0]
    services.cast_ballot(vote, option_id=yes.pk, lot_id=lot.pk, actor=user)
    # The residual window (documented in D56): while pooled, the choice and
    # the ballot still share the casting transaction.
    assert _xmins("governance_pendingsecretchoice", "vote_id", vote.pk) == _xmins(
        "governance_ballot", "vote_id", vote.pk
    )
    for i, (lot, user) in enumerate(voters[1:]):
        services.cast_ballot(vote, option_id=(no if i % 2 else yes).pk, lot_id=lot.pk, actor=user)
    assert _xmins("governance_secretchoice", "vote_id", vote.pk)  # open-time batches ran
    services.close_vote(vote, actor=board)

    ballots = _xmins("governance_ballot", "vote_id", vote.pk)
    choices = _xmins("governance_secretchoice", "vote_id", vote.pk)
    assert len(set(ballots)) == 7  # each ballot in its own transaction
    assert len(choices) == 7
    assert not set(ballots) & set(choices)
    assert PendingSecretChoice.objects.count() == 0
    counts = {r["option"].label: r["count"] for r in services.results(vote)["rows"]}
    assert counts == {"Yes": 4, "No": 3}


@COMMITTED
def test_election_choices_share_no_transaction_with_participation():
    board = make_member(board=True)
    election = elections.create_election(title="2027 election", seats=1, actor=board)
    for _lot, user in _owners(2):
        with system_access("t"):
            person = Person.objects.get(user=user)
        elections.nominate(election, person=person, actor=user)
    election = elections.open_voting(election, actor=board)
    cands = list(elections.ballot_candidates(election).order_by("pk"))
    for i, (lot, user) in enumerate(_owners(5)):
        elections.cast_election_ballot(
            election, lot_id=lot.pk, candidate_ids=[cands[i % 2].pk], actor=user
        )
    elections.close_voting(election, actor=board)
    with system_access("t"):
        parts = _xmins("governance_electionparticipation", "election_id", election.pk)
    choices = _xmins("governance_electionchoice", "election_id", election.pk)
    assert len(parts) == 5 and len(choices) == 5
    assert not set(parts) & set(choices)
    votes = sorted(r["votes"] for r in elections.tally(election)["rows"])
    assert votes == [2, 3]


@COMMITTED
def test_anonymous_survey_answers_share_no_transaction_with_participation():
    board = make_member(board=True)
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
    question = surveys.add_question(
        survey, kind="single", prompt="Time?", choices=["7 am", "9 am"], actor=board
    )
    surveys.open_survey(survey, actor=board)
    picks = list(question.choices.all())
    for i, (_lot, user) in enumerate(_owners(5)):
        surveys.submit_response(survey, answers={question.pk: [str(picks[i % 2].pk)]}, actor=user)
    surveys.close_survey(survey, actor=board)
    parts = _xmins("governance_surveyparticipation", "survey_id", survey.pk)
    responses = _xmins("governance_surveyresponse", "survey_id", survey.pk)
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT a.xmin::text FROM governance_surveyanswer a JOIN governance_surveyresponse r"
            " ON r.id = a.response_id WHERE r.survey_id = %s",
            [survey.pk],
        )
        answers = [row[0] for row in cursor.fetchall()]
    assert len(parts) == 5 and len(responses) == 5 and len(answers) == 5
    assert not set(parts) & (set(responses) | set(answers))
    res = surveys.results(survey, include_text=False)
    assert [c["count"] for c in res["rows"][0]["choices"]] == [3, 2]
