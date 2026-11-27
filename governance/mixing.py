"""Mixing pool for secret choices (decisions D56 and D68).

Secret votes, elections and anonymous surveys record *who* took part in one
append-only table and *what* was chosen in another. Writing both in the same
database transaction let anyone with read access to the database link them
afterwards: the rows share PostgreSQL's ``xmin`` (the inserting transaction
id) and sit next to each other on disk, in the write-ahead log and in a
dump's row order (external review, D56; the limitation D23 disclosed).

So casting writes the participation row as before and puts the choice in
``PendingSecretChoice``, a staging table with no identity. A flush, always
in a transaction of its own (a Procrastinate task deferred after the cast
commits, the hourly sweep, or the close itself), takes pooled choices out
with ``DELETE ... RETURNING``, shuffles them with ``secrets`` and inserts
them into the permanent append-only table in that order, with fresh random
ids and no timestamp. The permanent rows then share no transaction id,
sequence, time or physical position with any participation.

Pool mix, not a plain threshold: while voting is open a flush happens only
once ``2 * K`` choices wait (K = ``settings.SECRET_BALLOT_MIX_MIN``) and it
moves a random selection, keeping K random choices behind. Every flushed
choice may therefore belong to any of the voters whose choices were waiting,
and the last batch, written when voting closes, holds at least K choices
whenever K or more were cast (a plain threshold could leave a last batch of
one, revealing that voter). Closing flushes everything before results are
computed, and results for a closed vote, election or survey flush anything
left first, so no tally ever leaves a pooled choice out.

Sealed pool (D68). The pool row is written in the casting transaction on
purpose: the participation and the choice commit together or not at all, so
one vote per home stays atomic and no crash or retry loses or doubles a
ballot. That row therefore shares the casting transaction's id (``xmin``),
its place in the write-ahead log and its place in a dump with the
participation. So it holds nothing readable: the choice is sealed in the web
process (``governance.sealing``: AES-GCM, key derived from the server's
``DJANGO_SECRET_KEY``, padded to one size per vote, election or survey)
before the INSERT, and only ``flush`` opens it, in memory. Choices kept
behind are sealed again with fresh nonces and new ids, so they cannot be
followed from one batch to the next. A person with the database, a backup or
the WAL but not the server's key sees which homes voted (as the board does)
and opaque rows of one size; the plaintext choices appear only in the
flush's shuffled inserts, with no tie to any sealed row.

Residual risk (docs/decisions.md D68): someone holding the server's key *and*
the database, a disk image or the WAL from the voting window (in practice
root on the server) can open each sealed row and link it; a server operator
willing to change the code can read choices as they are cast. Nothing here is
logged or audited with the choice itself.
"""

from __future__ import annotations

import logging
import secrets
import uuid

from django.conf import settings
from django.db import connection, transaction
from django.db.models import Count
from django.utils import timezone
from psycopg import sql

from governance import sealing
from governance.models import (
    TEXT_ANSWER_MAX,
    Election,
    ElectionChoice,
    ElectionState,
    PendingSecretChoice,
    SecretChoice,
    Survey,
    SurveyAnswer,
    SurveyQuestion,
    SurveyResponse,
    Vote,
    VoteStatus,
)

logger = logging.getLogger(__name__)

VOTE, ELECTION, SURVEY = "vote", "election", "survey"
KINDS = (VOTE, ELECTION, SURVEY)
_PARENT = {VOTE: Vote, ELECTION: Election, SURVEY: Survey}
_COLUMN = {VOTE: "vote_id", ELECTION: "election_id", SURVEY: "survey_id"}

# Shuffling uses the operating system's randomness; tests swap in a seeded
# ``random.Random`` to make the order deterministic.
_rng = secrets.SystemRandom()


def mix_min() -> int:
    return max(2, int(getattr(settings, "SECRET_BALLOT_MIX_MIN", 5)))


# --- Casting -----------------------------------------------------------------------------


def size_for(kind: str, target) -> int:
    """The one padded plaintext size for every sealed choice of ``target``
    (options, seats and survey questions are fixed once voting opens)."""
    if kind == VOTE:
        return sealing.vote_bound()
    if kind == ELECTION:
        return sealing.election_bound(target.seats)
    questions = list(
        SurveyQuestion.objects.filter(survey_id=target.pk)
        .annotate(n=Count("choices"))
        .values_list("kind", "n")
    )
    return sealing.survey_bound(questions, TEXT_ANSWER_MAX)


def _pool(kind: str, target, payload: dict) -> None:
    """Seal the choice before it reaches the database, then stage it in the
    caller's (casting) transaction, next to the participation."""
    PendingSecretChoice.objects.create(
        **{_COLUMN[kind]: target.pk},
        sealed=sealing.seal(kind, target.pk, payload, size_for(kind, target)),
    )
    _schedule(kind, target.pk)


def pool_vote_choice(vote: Vote, option_id: int) -> None:
    _pool(VOTE, vote, {"option": int(option_id)})


def pool_election_ballot(election: Election, candidate_ids: list[int]) -> None:
    _pool(ELECTION, election, {"candidates": [int(c) for c in candidate_ids]})


def pool_survey_response(survey: Survey, answers: list[dict]) -> None:
    """``answers``: dicts with ``question`` and one of ``choice``/``rating``/``text``."""
    clean = [
        {
            "question": int(a["question"]),
            "choice": int(a["choice"]) if a.get("choice") is not None else None,
            "rating": int(a["rating"]) if a.get("rating") is not None else None,
            "text": str(a.get("text") or ""),
        }
        for a in answers
    ]
    _pool(SURVEY, survey, {"answers": clean})


def _schedule(kind: str, target_id: int) -> None:
    """Flush in the worker after the cast commits: never in its transaction."""
    transaction.on_commit(lambda: defer_flush(kind, target_id))


def defer_flush(kind: str, target_id: int) -> None:
    from procrastinate import exceptions as procrastinate_exceptions

    from governance.tasks import flush_secret_choices

    try:
        with transaction.atomic():  # a refused duplicate must not abort a caller's transaction
            flush_secret_choices.configure(queueing_lock=f"mix-{kind}-{target_id}").defer(
                kind=kind, target_id=target_id
            )
    except procrastinate_exceptions.AlreadyEnqueued:
        pass  # a flush for this target is already waiting
    except Exception:  # the hourly sweep and the close catch up
        logger.warning("governance.mix_flush_not_queued", extra={"kind": kind})


# --- Reading -----------------------------------------------------------------------------


def pending_count(kind: str, target_id: int) -> int:
    return PendingSecretChoice.objects.filter(**{_COLUMN[kind]: target_id}).count()


def is_final(kind: str, target) -> bool:
    """No choice can join the pool any more, so everything may be written."""
    if kind == VOTE:
        if target.status == VoteStatus.CLOSED:
            return True
        return bool(target.closes_at and target.closes_at <= timezone.now())
    if kind == ELECTION:
        return target.state in (ElectionState.CLOSED, ElectionState.CERTIFIED)
    return target.phase == "closed"


def settle_if_final(kind: str, target) -> int:
    """Before showing final results: write whatever is still pooled."""
    if is_final(kind, target) and pending_count(kind, target.pk):
        return flush(kind, target.pk, final=True)
    return 0


# --- Flushing ----------------------------------------------------------------------------


def flush(kind: str, target_id: int, *, final: bool = False) -> int:
    """Move pooled choices for one vote, election or survey to the permanent
    table, shuffled. While open, only when ``2 * K`` wait, keeping K behind;
    with ``final`` (closing, or no more choices can arrive) all of them.

    Runs in its own transaction when called outside one; the close path calls
    it inside the closing transaction, which writes no participation rows."""
    if kind not in KINDS:
        raise ValueError(kind)
    k = mix_min()
    with transaction.atomic():
        # Lock the vote/election/survey: casting and closing lock it too, so
        # the pool cannot change under us and no two flushes overlap.
        parent = _PARENT[kind].objects.select_for_update().filter(pk=target_id).first()
        if parent is None:
            return 0
        final = final or is_final(kind, parent)
        waiting = pending_count(kind, target_id)
        if not waiting or (not final and waiting < 2 * k):
            return 0
        rows = _take(kind, target_id)
        if not final:
            # Keep K random choices behind for the next batch (pool mix),
            # sealed again with fresh nonces and new ids: nothing ties a kept
            # row to the sealed row it came from.
            _rng.shuffle(rows)
            keep, rows = rows[:k], rows[k:]
            size = size_for(kind, parent)
            PendingSecretChoice.objects.bulk_create(
                [
                    PendingSecretChoice(
                        id=uuid.uuid4(),
                        **{_COLUMN[kind]: target_id},
                        sealed=sealing.seal(kind, target_id, payload, size),
                    )
                    for payload in keep
                ]
            )
        written = _write(kind, parent, rows)
    logger.info("governance.secret_choices_mixed", extra={"kind": kind, "count": written})
    return written


def _take(kind: str, target_id: int) -> list[dict]:
    """Delete, open and return every pooled choice for the target, in random
    order. A row no configured key opens stops the flush
    (``sealing.PoolKeyError``): the transaction rolls back, the row stays
    pooled, and no result is computed without it."""
    statement = sql.SQL(
        "DELETE FROM governance_pendingsecretchoice WHERE {} = %s RETURNING sealed, sort_key"
    ).format(sql.Identifier(_COLUMN[kind]))
    with connection.cursor() as cursor:
        cursor.execute(statement.as_string(cursor.connection), [target_id])
        fetched = cursor.fetchall()
    fetched.sort(key=lambda row: row[1])  # random keys: physical order is gone
    try:
        rows = [sealing.unseal(kind, target_id, blob) for blob, _ in fetched]
    except sealing.PoolKeyError:
        logger.error("governance.mix_unseal_failed", extra={"kind": kind})
        raise
    _rng.shuffle(rows)
    return rows


def _write(kind: str, parent, rows: list[dict]) -> int:
    if kind == VOTE:
        SecretChoice.objects.bulk_create(
            [SecretChoice(id=uuid.uuid4(), vote=parent, option_id=r["option"]) for r in rows]
        )
        return len(rows)
    if kind == ELECTION:
        picks = [cid for r in rows for cid in r["candidates"]]
        _rng.shuffle(picks)
        ElectionChoice.objects.bulk_create(
            [ElectionChoice(id=uuid.uuid4(), election=parent, candidate_id=c) for c in picks]
        )
        return len(rows)
    responses = [SurveyResponse(id=uuid.uuid4(), survey=parent) for _ in rows]
    SurveyResponse.objects.bulk_create(responses)
    answers = [
        SurveyAnswer(
            id=uuid.uuid4(),
            response=response,
            question_id=a["question"],
            choice_id=a["choice"],
            rating=a["rating"],
            text=a["text"],
        )
        for response, r in zip(responses, rows, strict=True)
        for a in r["answers"]
    ]
    _rng.shuffle(answers)
    SurveyAnswer.objects.bulk_create(answers)
    return len(rows)


def sweep() -> int:
    """Hourly: flush every target with pooled choices (all of them once no
    more can arrive). Catches flushes the after-commit task missed."""
    written = 0
    for kind in KINDS:
        column = _COLUMN[kind]
        ids = set(
            PendingSecretChoice.objects.filter(**{f"{column}__isnull": False}).values_list(
                column, flat=True
            )
        )
        for target_id in sorted(ids):
            try:
                written += flush(kind, target_id)
            except Exception:  # one stuck target must not hold up the others
                logger.exception("governance.mix_flush_failed", extra={"kind": kind})
    return written
