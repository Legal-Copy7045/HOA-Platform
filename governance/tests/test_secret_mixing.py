"""D56: secret choices go through a mixing pool, so the permanent choice rows
cannot be linked to who voted by transaction id, order, id or time.

These tests run inside the usual per-test transaction; the transaction id
checks, which need real separate transactions, are in
test_secret_mixing_transactions.py."""

import json
import logging
import random
from datetime import timedelta

import psycopg
import pytest
from django.db import connection
from django.utils import timezone

from audit.models import AuditEvent
from community import services as community
from community.models import Role
from community.tests.factories import LotFactory, make_member
from core.scoping import system_access
from governance import elections, mixing, services, surveys
from governance.models import (
    Ballot,
    ElectionChoice,
    ElectionParticipation,
    PendingSecretChoice,
    SecretChoice,
    SurveyAnswer,
    SurveyParticipation,
    SurveyResponse,
    VoteKind,
)
from governance.tests.helpers import close_early, window_kw

pytestmark = pytest.mark.django_db

K = 5


@pytest.fixture(autouse=True)
def _mix(settings, monkeypatch):
    settings.SECRET_BALLOT_MIX_MIN = K
    # A seeded generator makes the shuffle deterministic in tests only.
    seeded = random.Random(20261126)  # noqa: S311  # nosec B311
    monkeypatch.setattr(mixing, "_rng", seeded)


@pytest.fixture
def board():
    return make_member(board=True)


def _owners(n):
    out = []
    for _ in range(n):
        lot = LotFactory()
        out.append((lot, make_member(lots=[lot])))
    return out


def _secret_formal(board, labels, days=8):
    community.set_rule(key="vote.quorum_percent", value="10", citation="Bylaws §2.6", actor=board)
    community.set_rule(key="vote.pass_percent", value="50", citation="Decl. §14", actor=board)
    vote = services.create_vote(
        title="Pond fountain?", kind=VoteKind.FORMAL, options=labels, secret=True, actor=board
    )
    vote = services.open_vote(vote, actor=board, **window_kw(days))
    return vote, list(vote.options.all())


def _cast(vote, voters, options, picks):
    for (lot, user), i in zip(voters, picks, strict=True):
        services.cast_ballot(vote, option_id=options[i].pk, lot_id=lot.pk, actor=user)


def _physical(table, column, where_column, target_id):
    with connection.cursor() as cursor:
        cursor.execute(
            f"SELECT {column} FROM {table} WHERE {where_column} = %s ORDER BY ctid",  # nosec B608
            [target_id],
        )
        return [row[0] for row in cursor.fetchall()]


# --- The pool carries no identity ----------------------------------------------------------


def test_a_pooled_choice_has_no_person_lot_ballot_or_time(board):
    fields = {f.name for f in PendingSecretChoice._meta.get_fields()}
    assert fields == {"id", "vote", "election", "survey", "payload", "sort_key"}
    assert PendingSecretChoice._meta.pk.get_internal_type() == "UUIDField"
    voters = _owners(1)
    vote, options = _secret_formal(board, ["Yes", "No"])
    _cast(vote, voters, options, [0])
    row = PendingSecretChoice.objects.get(vote=vote)
    assert row.payload == {"option": options[0].pk}
    ballot = Ballot.objects.get(vote=vote)
    assert ballot.option_id is None
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_name = 'governance_pendingsecretchoice' ORDER BY column_name"
        )
        columns = [r[0] for r in cursor.fetchall()]
    assert columns == ["election_id", "id", "payload", "sort_key", "survey_id", "vote_id"]
    assert set(row.payload) == {"option"}  # nothing but the choice


def test_casting_defers_the_flush_after_commit(
    board, monkeypatch, django_capture_on_commit_callbacks
):
    queued = []
    monkeypatch.setattr(mixing, "defer_flush", lambda kind, pk: queued.append((kind, pk)))
    voters = _owners(1)
    vote, options = _secret_formal(board, ["Yes", "No"])
    with django_capture_on_commit_callbacks(execute=False) as callbacks:
        _cast(vote, voters, options, [1])
    assert queued == []  # nothing runs inside the casting transaction
    for callback in callbacks:
        callback()
    assert queued == [("vote", vote.pk)]


def test_the_flush_task_is_a_procrastinate_job(board, django_capture_on_commit_callbacks):
    from procrastinate.contrib.django.models import ProcrastinateJob

    voters = _owners(2)
    vote, options = _secret_formal(board, ["Yes", "No"])
    with django_capture_on_commit_callbacks(execute=True):
        _cast(vote, voters, options, [0, 1])
    jobs = ProcrastinateJob.objects.filter(task_name="governance.tasks.flush_secret_choices")
    # One waiting job per vote (queueing lock), carrying only the vote's id.
    assert jobs.count() == 1
    assert jobs.get().args == {"kind": "vote", "target_id": vote.pk}


# --- Flushing rules -------------------------------------------------------------------------


def test_open_vote_flushes_only_with_twice_k_waiting_and_keeps_k_behind(board):
    voters = _owners(2 * K + 1)
    vote, options = _secret_formal(board, ["Yes", "No"])
    _cast(vote, voters[: 2 * K - 1], options, [0] * (2 * K - 1))
    assert mixing.flush("vote", vote.pk) == 0  # 2K-1 waiting: nothing written
    assert SecretChoice.objects.filter(vote=vote).count() == 0
    _cast(vote, voters[2 * K - 1 : 2 * K], options, [1])
    assert mixing.flush("vote", vote.pk) == K
    assert SecretChoice.objects.filter(vote=vote).count() == K
    assert mixing.pending_count("vote", vote.pk) == K
    res = services.results(vote)
    assert res["total"] == K and res["pending"] == K and res["outcome"] is None


def test_closing_with_fewer_than_k_pooled_still_counts_them(board):
    voters = _owners(3)
    vote, options = _secret_formal(board, ["Yes", "No"])
    _cast(vote, voters, options, [0, 0, 1])
    assert mixing.flush("vote", vote.pk) == 0
    close_early(vote, board)
    assert PendingSecretChoice.objects.count() == 0
    res = services.results(vote)
    assert {r["option"].label: r["count"] for r in res["rows"]} == {"Yes": 2, "No": 1}
    assert res["pending"] == 0 and res["total"] == 3 and res["outcome"]["turnout"] > 0


def test_choices_are_written_in_shuffled_order_not_casting_order(board):
    labels = [f"Option {n}" for n in range(12)]
    voters = _owners(12)
    vote, options = _secret_formal(board, labels)
    picks = list(range(12))  # voter i picks option i: casting order is visible
    _cast(vote, voters[:10], options, picks[:10])
    assert mixing.flush("vote", vote.pk) == K  # an open-time batch
    _cast(vote, voters[10:], options, picks[10:])
    close_early(vote, board)
    by_position = _physical("governance_secretchoice", "option_id", "vote_id", vote.pk)
    by_id = [
        c.option_id for c in SecretChoice.objects.filter(vote=vote).order_by("id")
    ]  # random ids
    cast_order = [options[i].pk for i in picks]
    assert sorted(by_position) == sorted(cast_order)  # a permutation of what was cast
    assert by_position != cast_order
    assert by_id != cast_order
    counts = {r["option"].label: r["count"] for r in services.results(vote)["rows"]}
    assert counts == dict.fromkeys(labels, 1)
    assert PendingSecretChoice.objects.count() == 0


def test_permanent_choice_rows_have_no_time_or_sequence():
    for model in (SecretChoice, ElectionChoice, SurveyAnswer):
        types = {f.get_internal_type() for f in model._meta.concrete_fields}
        assert "DateTimeField" not in types and "DateField" not in types
        assert model._meta.pk.get_internal_type() == "UUIDField"
    assert SurveyResponse._meta.pk.get_internal_type() == "UUIDField"


def test_results_of_a_vote_past_its_closing_time_count_the_pool(board, monkeypatch):
    voters = _owners(2)
    vote, options = _secret_formal(board, ["Yes", "No"])
    _cast(vote, voters, options, [0, 1])
    # Past the deadline, before the close task has run (D61 forbids
    # moving the deadline earlier, so time moves instead).
    later = vote.closes_at + timedelta(minutes=1)
    monkeypatch.setattr(timezone, "now", lambda: later)
    res = services.results(vote)  # nobody can vote any more: write the pool first
    assert res["pending"] == 0 and res["total"] == 2


def test_sweep_writes_what_can_no_longer_grow_and_leaves_open_pools(board, monkeypatch):
    voters = _owners(4)
    open_vote, open_options = _secret_formal(board, ["Yes", "No"], days=20)
    _cast(open_vote, voters[:2], open_options, [0, 1])
    ended, ended_options = _secret_formal(board, ["Yes", "No"])
    _cast(ended, voters[2:], ended_options, [0, 0])
    later = ended.closes_at + timedelta(hours=1)
    monkeypatch.setattr(timezone, "now", lambda: later)
    assert mixing.sweep() == 2
    assert mixing.pending_count("vote", open_vote.pk) == 2
    assert mixing.pending_count("vote", ended.pk) == 0
    assert SecretChoice.objects.filter(vote=ended).count() == 2


def test_choices_cast_before_this_change_still_count(board):
    """Old rows are never rewritten (append-only); they are counted with the new."""
    voters = _owners(1)
    vote, options = _secret_formal(board, ["Yes", "No"])
    SecretChoice.objects.create(vote=vote, option=options[1])  # the pre-D56 layout
    _cast(vote, voters, options, [0])
    close_early(vote, board)
    counts = {r["option"].label: r["count"] for r in services.results(vote)["rows"]}
    assert counts == {"Yes": 1, "No": 1}


# --- Unchanged rules ------------------------------------------------------------------------


def test_double_voting_is_still_refused_and_adds_nothing_to_the_pool(board):
    voters = _owners(1)
    vote, options = _secret_formal(board, ["Yes", "No"])
    _cast(vote, voters, options, [0])
    lot, user = voters[0]
    with pytest.raises(services.GovernanceError):
        services.cast_ballot(vote, option_id=options[1].pk, lot_id=lot.pk, actor=user)
    assert PendingSecretChoice.objects.filter(vote=vote).count() == 1
    assert Ballot.objects.filter(vote=vote).count() == 1


def test_eligibility_is_unchanged_and_refusals_pool_nothing(board):
    from django.core.exceptions import PermissionDenied

    voters = _owners(1)
    vote, options = _secret_formal(board, ["Yes", "No"])
    lot, _owner = voters[0]
    other = make_member(lots=[LotFactory()])
    with pytest.raises(services.GovernanceError):  # not their home
        services.cast_ballot(vote, option_id=options[0].pk, lot_id=lot.pk, actor=other)
    with pytest.raises(PermissionDenied):
        services.cast_ballot(
            vote, option_id=options[0].pk, lot_id=lot.pk, actor=make_member(roles=[Role.MANAGER])
        )
    close_early(vote, board)
    with pytest.raises(services.GovernanceError):  # closed
        services.cast_ballot(vote, option_id=options[0].pk, lot_id=lot.pk, actor=_owner)
    assert PendingSecretChoice.objects.count() == 0


def test_vote_page_shows_pooled_ballots_as_a_count_only(board, client):
    voters = _owners(2)
    vote, options = _secret_formal(board, ["Yes", "No"])
    _cast(vote, voters, options, [0, 1])
    client.force_login(board)
    from django.urls import reverse

    page = client.get(reverse("vote_detail", args=[vote.pk])).content.decode()
    assert "2 secret ballots are waiting to be mixed" in page
    assert "Yes" not in page.split("Turnout so far", 1)[1].split("</section>", 1)[0]


# --- Elections and anonymous surveys ------------------------------------------------------


def _election(board, voters, picks_by_voter, seats=2):
    candidates_people = _owners(3)
    election = elections.create_election(title="2027 election", seats=seats, actor=board)
    with system_access("t"):
        from community.models import Person

        for _lot, user in candidates_people:
            elections.nominate(election, person=Person.objects.get(user=user), actor=user)
    election = elections.open_voting(election, actor=board, **window_kw())
    cands = list(elections.ballot_candidates(election).order_by("pk"))
    for (lot, user), picks in zip(voters, picks_by_voter, strict=True):
        elections.cast_election_ballot(
            election, lot_id=lot.pk, candidate_ids=[cands[i].pk for i in picks], actor=user
        )
    return election, cands


def test_election_ballots_are_pooled_and_counted_at_close(board):
    voters = _owners(3)
    election, cands = _election(board, voters, [[0, 1], [0], [2]])
    assert ElectionParticipation.objects.filter(election=election).count() == 3
    assert ElectionChoice.objects.filter(election=election).count() == 0
    assert PendingSecretChoice.objects.filter(election=election).count() == 3
    lot, user = voters[0]
    with pytest.raises(elections.ElectionError):  # double voting
        elections.cast_election_ballot(
            election, lot_id=lot.pk, candidate_ids=[cands[2].pk], actor=user
        )
    close_early(election, board)
    assert PendingSecretChoice.objects.count() == 0
    votes = {r["candidate"].pk: r["votes"] for r in elections.tally(election)["rows"]}
    assert votes == {cands[0].pk: 2, cands[1].pk: 1, cands[2].pk: 1}


def test_election_choices_are_shuffled(board):
    voters = _owners(9)
    picks = [[i % 3] for i in range(9)]
    election, cands = _election(board, voters, picks, seats=1)
    close_early(election, board)
    written = _physical("governance_electionchoice", "candidate_id", "election_id", election.pk)
    cast_order = [cands[p[0]].pk for p in picks]
    assert sorted(written) == sorted(cast_order) and written != cast_order


@pytest.fixture
def sw(board):
    voters = _owners(6)
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
    surveys.add_question(
        survey, kind="single", prompt="Time?", choices=["7 am", "9 am"], actor=board
    )
    surveys.add_question(survey, kind="text", prompt="Anything else?", actor=board)
    surveys.open_survey(survey, actor=board)
    return survey, voters


def _respond(survey, user, choice_index, text):
    q = {q.kind: q for q in survey.questions.prefetch_related("choices")}
    choice = list(q["single"].choices.all())[choice_index]
    surveys.submit_response(
        survey, answers={q["single"].pk: [str(choice.pk)], q["text"].pk: [text]}, actor=user
    )


def test_anonymous_survey_responses_are_pooled_and_written_at_close(board, sw):
    survey, voters = sw
    for n, (_lot, user) in enumerate(voters[:3]):
        _respond(survey, user, n % 2, f"note {n}")
    with system_access("t"):
        assert SurveyParticipation.objects.filter(survey=survey).count() == 3
        assert SurveyResponse.objects.filter(survey=survey).count() == 0
    with pytest.raises(surveys.SurveyError):  # once per person
        _respond(survey, voters[0][1], 0, "again")
    pooled = PendingSecretChoice.objects.filter(survey=survey)
    assert pooled.count() == 3
    assert set(pooled.first().payload) == {"answers"}
    open_results = surveys.results(survey, include_text=True)
    assert open_results["responses"] == 3 and open_results["pending"] == 3
    surveys.close_survey(survey, actor=board)
    assert PendingSecretChoice.objects.count() == 0
    res = surveys.results(survey, include_text=True)
    rows = {r["question"].kind: r for r in res["rows"]}
    assert [c["count"] for c in rows["single"]["choices"]] == [2, 1]
    assert sorted(t["text"] for t in rows["text"]["texts"]) == ["note 0", "note 1", "note 2"]
    assert all(t["author"] is None for t in rows["text"]["texts"])
    with system_access("t"):
        assert not SurveyResponse.objects.filter(survey=survey, person__isnull=False).exists()


def test_survey_past_its_last_day_counts_the_pool(board, sw, monkeypatch):
    survey, voters = sw
    _respond(survey, voters[0][1], 0, "late")
    after = survey.closes_on + timedelta(days=1)
    monkeypatch.setattr(timezone, "localdate", lambda *a, **k: after)
    assert survey.phase == "closed"  # past its last day, status still open
    res = surveys.results(survey, include_text=True)
    assert res["pending"] == 0
    assert PendingSecretChoice.objects.count() == 0


# --- Nothing logs or audits the choice -----------------------------------------------------


def test_no_log_or_audit_line_carries_the_choice(board, sw, caplog):
    survey, voters = sw
    marker = "Zebra-Option-7731"
    caplog.set_level(logging.DEBUG)
    vote, options = _secret_formal(board, ["Plain", marker])
    _cast(vote, voters[:3], options, [1, 1, 0])
    _respond(survey, voters[3][1], 1, "Quokka-Text-5512")
    close_early(vote, board)
    surveys.close_survey(survey, actor=board)
    assert SecretChoice.objects.filter(vote=vote, option=options[1]).count() == 2
    text = "\n".join(
        f"{r.getMessage()} {json.dumps(r.__dict__, default=str)}" for r in caplog.records
    )
    assert marker not in text and "Quokka-Text-5512" not in text
    for event in AuditEvent.objects.filter(action__in=["vote.ballot_cast", "survey.responded"]):
        meta = json.dumps(event.metadata)
        assert "option" not in meta and "choice" not in meta and "Quokka" not in meta
    audit_text = json.dumps([e.metadata for e in AuditEvent.objects.all()], default=str)
    assert marker not in audit_text and "Quokka-Text-5512" not in audit_text


# --- Runtime role grants -------------------------------------------------------------------


def _privilege(role, table, privilege):
    with connection.cursor() as cursor:
        cursor.execute("SELECT has_table_privilege(%s, %s, %s)", [role, table, privilege])
        return cursor.fetchone()[0]


def test_runtime_role_may_stage_and_flush_but_never_rewrite_choices(settings):
    role = settings.DB_APP_ROLE
    assert role
    pool = "governance_pendingsecretchoice"
    for privilege in ("SELECT", "INSERT", "DELETE"):
        assert _privilege(role, pool, privilege), privilege
    assert not _privilege(role, pool, "UPDATE")
    assert not _privilege(role, pool, "TRUNCATE")
    for table in (
        "governance_secretchoice",
        "governance_electionchoice",
        "governance_surveyresponse",
        "governance_surveyanswer",
        "governance_ballot",
        "governance_electionparticipation",
        "governance_surveyparticipation",
    ):
        assert _privilege(role, table, "INSERT"), table
        for privilege in ("UPDATE", "DELETE", "TRUNCATE"):
            assert not _privilege(role, table, privilege), (table, privilege)


def test_the_flush_statement_works_as_the_runtime_role(app_role_connection):
    cur = app_role_connection.cursor()
    cur.execute("DELETE FROM governance_pendingsecretchoice WHERE vote_id = -1 RETURNING payload")
    assert cur.fetchall() == []
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        cur.execute("UPDATE governance_pendingsecretchoice SET sort_key = 0 WHERE vote_id = -1")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        cur.execute("DELETE FROM governance_secretchoice WHERE vote_id = -1")
