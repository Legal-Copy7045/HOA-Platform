"""Surveys: informal, non-binding multi-question surveys (M5).

The board drafts a survey (title, intro, audience, dates, anonymous or
named, questions in order), then opens it; from then on it is fixed (this
module refuses, and database triggers back it up). Each person in the
audience answers once; answers are append-only.

Anonymous surveys are stored like secret ballots (D21, D23): a
``SurveyParticipation`` row records only that the person responded, on which
day; their answers sit under a ``SurveyResponse`` with no person, no time and
a random id. The application never joins the two. The D23 limitation applies
here too: someone with direct database access and Postgres knowledge could
match rows written in the same transaction.
"""

from __future__ import annotations

import csv
import datetime
from collections import defaultdict

from django.conf import settings
from django.db import IntegrityError, transaction
from django.http import HttpResponse
from django.urls import reverse
from django.utils import timezone

from audit.services import record_event
from community.models import AccountContact, ContactKind, Ownership, Person
from community.services import _refuse_read_only
from core import policy
from core.lists import safe_cell
from core.policy import can, require
from core.scoping import system_access
from core.text import single_line
from governance.models import (
    TEXT_ANSWER_MAX,
    QuestionKind,
    Survey,
    SurveyAnswer,
    SurveyAudience,
    SurveyChoice,
    SurveyParticipation,
    SurveyQuestion,
    SurveyResponse,
    SurveyStatus,
)

MAX_QUESTIONS = 40
MAX_CHOICES = 20


class SurveyError(ValueError):
    pass


# --- Who is in the audience -------------------------------------------------------------


def in_audience(survey: Survey, user) -> bool:
    """Owners always; account contacts and tenants only on a residents survey."""
    if user is policy.SYSTEM or not getattr(user, "is_authenticated", False):
        return False
    groups = policy.roles_for(user).groups
    if survey.audience == SurveyAudience.OWNERS:
        return policy.OWNER in groups
    return bool(groups & policy.RESIDENTS)


def audience_people(survey: Survey) -> set[int]:
    """Person ids of everyone asked to answer the survey, today (view-only
    contacts may read a residents survey but are not asked to answer)."""
    with system_access("survey audience"):
        people = set(Ownership.objects.active().values_list("person_id", flat=True))
        if survey.audience == SurveyAudience.RESIDENTS:
            people |= set(
                AccountContact.objects.active()
                .filter(account__closed_on__isnull=True)
                .exclude(kind=ContactKind.VIEW_ONLY)
                .values_list("person_id", flat=True)
            )
    return people


def can_manage(user) -> bool:
    return can(user, "survey.manage")


def can_view(survey: Survey, user) -> bool:
    if can_manage(user):
        return True
    if survey.status == SurveyStatus.DRAFT or not can(user, "survey.view"):
        return False
    return in_audience(survey, user)


def visible_surveys(user):
    """Surveys this person may open: every one for the board; otherwise the
    non-draft surveys whose audience includes them."""
    rows = Survey.objects.all()
    if can_manage(user):
        return list(rows)
    if not can(user, "survey.view"):
        return []
    return [s for s in rows.exclude(status=SurveyStatus.DRAFT) if in_audience(s, user)]


def _person_id(user) -> int | None:
    return policy.roles_for(user).person_id


def has_responded(survey: Survey, user) -> datetime.date | None:
    """The day this person responded, or None."""
    person_id = _person_id(user)
    if not person_id:
        return None
    return (
        SurveyParticipation.objects.unscoped("own participation")
        .filter(survey=survey, person_id=person_id)
        .values_list("responded_on", flat=True)
        .first()
    )


def can_respond(survey: Survey, user) -> bool:
    return (
        survey.phase == "open"
        and can(user, "survey.respond")
        and in_audience(survey, user)
        and _person_id(user) is not None
        and has_responded(survey, user) is None
    )


def waiting_for(user) -> list[Survey]:
    """Open surveys this person is asked to answer and has not (My items)."""
    if not can(user, "survey.respond"):
        return []
    person_id = _person_id(user)
    if not person_id:
        return []
    today = timezone.localdate()
    rows = list(
        Survey.objects.filter(
            status=SurveyStatus.OPEN, opens_on__lte=today, closes_on__gte=today
        ).order_by("closes_on")
    )
    if not rows:
        return []
    done = set(
        SurveyParticipation.objects.unscoped("my items")
        .filter(person_id=person_id, survey__in=rows)
        .values_list("survey_id", flat=True)
    )
    return [s for s in rows if s.pk not in done and in_audience(s, user)]


# --- Drafting ------------------------------------------------------------------------------


def _clean_settings(title, intro, audience, opens_on, closes_on):
    title = single_line(title or "")[:200]
    if not title:
        raise SurveyError("A survey needs a title.")
    if audience not in SurveyAudience.values:
        raise SurveyError("Choose who the survey is for.")
    if not opens_on or not closes_on:
        raise SurveyError("Choose the opening and closing dates.")
    if closes_on < opens_on:
        raise SurveyError("The closing date must be on or after the opening date.")
    return title, (intro or "").strip()[:5000]


def _require_draft(survey: Survey) -> Survey:
    """Lock the survey row and refuse unless it is still a draft."""
    locked = Survey.objects.select_for_update().get(pk=survey.pk)
    if locked.status != SurveyStatus.DRAFT:
        raise SurveyError("This survey is open or closed, so it can no longer change.")
    return locked


def create_survey(
    *,
    title: str,
    intro: str = "",
    audience: str = SurveyAudience.OWNERS,
    opens_on: datetime.date,
    closes_on: datetime.date,
    anonymous: bool = False,
    share_results: bool = False,
    actor,
    request=None,
) -> Survey:
    require(actor, "survey.manage")
    title, intro = _clean_settings(title, intro, audience, opens_on, closes_on)
    survey = Survey.objects.create(
        title=title,
        intro=intro,
        audience=audience,
        opens_on=opens_on,
        closes_on=closes_on,
        anonymous=bool(anonymous),
        share_results=bool(share_results),
        created_by=actor,
    )
    record_event(
        "survey.created",
        actor=actor,
        request=request,
        target=survey,
        metadata={"audience": audience, "anonymous": survey.anonymous},
    )
    return survey


def update_survey(
    survey: Survey,
    *,
    title: str,
    intro: str,
    audience: str,
    opens_on,
    closes_on,
    anonymous: bool,
    share_results: bool,
    actor,
    request=None,
) -> Survey:
    require(actor, "survey.manage")
    title, intro = _clean_settings(title, intro, audience, opens_on, closes_on)
    with transaction.atomic():
        _require_draft(survey)
        Survey.objects.filter(pk=survey.pk).update(
            title=title,
            intro=intro,
            audience=audience,
            opens_on=opens_on,
            closes_on=closes_on,
            anonymous=bool(anonymous),
            share_results=bool(share_results),
        )
    record_event(
        "survey.edited",
        actor=actor,
        request=request,
        target=survey,
        metadata={"change": "settings"},
    )
    survey.refresh_from_db()
    return survey


def _clean_question(kind, prompt, choices, max_picks):
    prompt = single_line(prompt or "")[:300]
    if kind not in QuestionKind.values:
        raise SurveyError("Choose the kind of question.")
    if not prompt:
        raise SurveyError("Write the question.")
    labels = []
    if kind in (QuestionKind.SINGLE, QuestionKind.MULTIPLE):
        labels = [single_line(c)[:200] for c in (choices or []) if c and single_line(c)]
        if len(labels) < 2 or len(set(labels)) != len(labels):
            raise SurveyError("Give at least two different choices, one per line.")
        if len(labels) > MAX_CHOICES:
            raise SurveyError(f"A question can have at most {MAX_CHOICES} choices.")
    picks = None
    if kind == QuestionKind.MULTIPLE and max_picks:
        picks = int(max_picks)
        if not 1 <= picks <= len(labels):
            raise SurveyError("The most picks must be between 1 and the number of choices.")
    return prompt, labels, picks


def add_question(
    survey: Survey,
    *,
    kind: str,
    prompt: str,
    required: bool = False,
    choices: list[str] | None = None,
    max_picks: int | None = None,
    actor,
    request=None,
) -> SurveyQuestion:
    require(actor, "survey.manage")
    prompt, labels, picks = _clean_question(kind, prompt, choices, max_picks)
    with transaction.atomic():
        locked = _require_draft(survey)
        count = SurveyQuestion.objects.filter(survey=locked).count()
        if count >= MAX_QUESTIONS:
            raise SurveyError(f"A survey can have at most {MAX_QUESTIONS} questions.")
        last = (
            SurveyQuestion.objects.filter(survey=locked)
            .order_by("-position")
            .values_list("position", flat=True)
            .first()
        )
        question = SurveyQuestion.objects.create(
            survey=locked,
            position=(last + 1) if last is not None else 0,
            kind=kind,
            prompt=prompt,
            required=bool(required),
            max_picks=picks,
        )
        for i, label in enumerate(labels):
            SurveyChoice.objects.create(question=question, label=label, position=i)
    record_event(
        "survey.edited",
        actor=actor,
        request=request,
        target=survey,
        metadata={"change": "question_added"},
    )
    return question


def edit_question(
    question: SurveyQuestion,
    *,
    kind: str,
    prompt: str,
    required: bool = False,
    choices: list[str] | None = None,
    max_picks: int | None = None,
    actor,
    request=None,
) -> SurveyQuestion:
    require(actor, "survey.manage")
    prompt, labels, picks = _clean_question(kind, prompt, choices, max_picks)
    with transaction.atomic():
        _require_draft(question.survey)
        SurveyQuestion.objects.filter(pk=question.pk).update(
            kind=kind, prompt=prompt, required=bool(required), max_picks=picks
        )
        SurveyChoice.objects.filter(question=question).delete()
        for i, label in enumerate(labels):
            SurveyChoice.objects.create(question=question, label=label, position=i)
    record_event(
        "survey.edited",
        actor=actor,
        request=request,
        target=question.survey,
        metadata={"change": "question_edited"},
    )
    question.refresh_from_db()
    return question


def move_question(question: SurveyQuestion, *, up: bool, actor, request=None) -> None:
    require(actor, "survey.manage")
    with transaction.atomic():
        locked = _require_draft(question.survey)
        order = list(SurveyQuestion.objects.filter(survey=locked).order_by("position", "id"))
        i = next(n for n, q in enumerate(order) if q.pk == question.pk)
        j = i - 1 if up else i + 1
        if not 0 <= j < len(order):
            return
        order[i], order[j] = order[j], order[i]
        for position, q in enumerate(order):
            if q.position != position:
                SurveyQuestion.objects.filter(pk=q.pk).update(position=position)
    record_event(
        "survey.edited",
        actor=actor,
        request=request,
        target=question.survey,
        metadata={"change": "question_moved"},
    )


def remove_question(question: SurveyQuestion, *, actor, request=None) -> None:
    require(actor, "survey.manage")
    survey = question.survey
    with transaction.atomic():
        _require_draft(survey)
        SurveyChoice.objects.filter(question=question).delete()
        SurveyQuestion.objects.filter(pk=question.pk).delete()
    record_event(
        "survey.edited",
        actor=actor,
        request=request,
        target=survey,
        metadata={"change": "question_removed"},
    )


# --- Opening and closing -------------------------------------------------------------------


def open_survey(survey: Survey, *, notify: bool = False, actor, request=None) -> Survey:
    """Fix the survey and open it (answers are taken from its opening date)."""
    require(actor, "survey.manage")
    with transaction.atomic():
        locked = _require_draft(survey)
        questions = list(SurveyQuestion.objects.filter(survey=locked))
        if not questions:
            raise SurveyError("Add at least one question before opening the survey.")
        if locked.closes_on < timezone.localdate():
            raise SurveyError("The closing date has passed. Choose new dates first.")
        Survey.objects.filter(pk=locked.pk).update(
            status=SurveyStatus.OPEN, opened_at=timezone.now()
        )
        survey.refresh_from_db()
        emailed = _announce(survey) if notify else 0
    record_event(
        "survey.opened",
        actor=actor,
        request=request,
        target=survey,
        metadata={"questions": len(questions), "emailed": emailed},
    )
    return survey


def _announce(survey: Survey) -> int:
    """Email the audience through the update channel (honours the digest)."""
    from comms.notify import send_update

    with system_access("survey announcement"):
        emails = set(
            Person.objects.filter(
                pk__in=audience_people(survey), user__isnull=False, user__is_active=True
            ).values_list("user__email", flat=True)
        )
    emails.discard("")
    if not emails:
        return 0
    link = settings.PORTAL_BASE_URL + reverse("survey_detail", args=[survey.pk])
    when = (
        f"from {survey.opens_on:%B} {survey.opens_on.day} "
        if survey.opens_on > timezone.localdate()
        else ""
    )
    send_update(
        emails,
        subject=f"[{settings.SITE_NAME}] New survey: {survey.title}"[:200],
        body=(
            f"The board would like your views. The survey is open {when}until "
            f"{survey.closes_on:%B} {survey.closes_on.day}, {survey.closes_on.year}. "
            "It is informal and not a vote.\n\n"
            f"{link}\n\n-- \n{settings.SITE_NAME}"
        ),
        link=link,
    )
    return len(emails)


def close_survey(survey: Survey, *, actor, at_deadline: bool = False, request=None) -> Survey:
    """Close the survey: by the board at any time (it is informal), or by
    SYSTEM after its last day (the periodic task, D61)."""
    if actor is not policy.SYSTEM:
        require(actor, "survey.manage")
        at_deadline = False
    with transaction.atomic():
        locked = Survey.objects.select_for_update().get(pk=survey.pk)
        if locked.status != SurveyStatus.OPEN:
            raise SurveyError("Only an open survey can be closed.")
        if locked.anonymous:
            # Every pooled response is written before the results (D56), while
            # the survey is still open: the database refuses responses to a
            # closed survey, and none can join the pool once it is.
            from governance import mixing

            mixing.flush(mixing.SURVEY, locked.pk, final=True)
        Survey.objects.filter(pk=locked.pk).update(
            status=SurveyStatus.CLOSED, closed_at=timezone.now(), closed_at_deadline=at_deadline
        )
    record_event(
        "survey.closed",
        actor=None if actor is policy.SYSTEM else actor,
        request=request,
        target=survey,
        metadata={"how": "deadline" if at_deadline else "board"},
    )
    survey.refresh_from_db()
    return survey


def set_sharing(survey: Survey, *, share: bool, actor, request=None) -> Survey:
    """Whether owners see the totals once the survey closes. A display
    choice, so it may change at any time; free-text answers are never shared."""
    require(actor, "survey.manage")
    Survey.objects.filter(pk=survey.pk).update(share_results=bool(share))
    record_event(
        "survey.sharing_changed",
        actor=actor,
        request=request,
        target=survey,
        metadata={"share_results": bool(share)},
    )
    survey.refresh_from_db()
    return survey


# --- Answering -----------------------------------------------------------------------------


def _parse(question: SurveyQuestion, values: list[str]) -> list[dict]:
    """Turn one question's submitted values into answer fields."""
    values = [v for v in (values or []) if v is not None and str(v).strip() != ""]
    if not values:
        if question.required:
            raise SurveyError(f"Please answer: {question.prompt}")
        return []
    kind = question.kind
    if kind == QuestionKind.TEXT:
        text = str(values[0]).strip()
        if len(text) > TEXT_ANSWER_MAX:
            raise SurveyError(f"Answers can be at most {TEXT_ANSWER_MAX} characters.")
        return [{"text": text}]
    if kind == QuestionKind.RATING:
        try:
            rating = int(values[0])
        except (TypeError, ValueError):
            rating = 0
        if not 1 <= rating <= 5:
            raise SurveyError(f"Choose a rating from 1 to 5: {question.prompt}")
        return [{"rating": rating}]
    valid = {str(c.pk): c for c in question.choices.all()}
    picked = list(dict.fromkeys(str(v) for v in values))
    if any(p not in valid for p in picked):
        raise SurveyError(f"Choose from the choices given: {question.prompt}")
    if kind == QuestionKind.SINGLE and len(picked) > 1:
        raise SurveyError(f"Choose one answer: {question.prompt}")
    if kind == QuestionKind.MULTIPLE and question.max_picks and len(picked) > question.max_picks:
        raise SurveyError(f"Choose at most {question.max_picks}: {question.prompt}")
    return [{"choice": valid[p]} for p in picked]


def submit_response(survey: Survey, *, answers: dict, actor, request=None) -> None:
    """Record one person's answers. ``answers`` maps question id to a list
    of submitted values. Once submitted nothing can change."""
    require(actor, "survey.respond")
    _refuse_read_only(actor)  # a former owner's login changes nothing
    person_id = _person_id(actor)
    if person_id is None:
        raise SurveyError("Your login is not linked to a person in the community register.")
    with transaction.atomic():
        # Lock the survey: a page loaded before it closed must not add answers.
        locked = Survey.objects.select_for_update().get(pk=survey.pk)
        if locked.phase != "open":
            raise SurveyError("This survey is not open for answers.")
        if not in_audience(locked, actor):
            raise SurveyError("This survey is not for you.")
        questions = list(SurveyQuestion.objects.filter(survey=locked).prefetch_related("choices"))
        parsed = {q.pk: _parse(q, list(answers.get(q.pk, []))) for q in questions}
        if not any(parsed.values()):
            raise SurveyError("Answer at least one question.")
        try:
            with transaction.atomic():
                SurveyParticipation.objects.create(
                    survey=locked, person_id=person_id, responded_on=timezone.localdate()
                )
        except IntegrityError as exc:
            raise SurveyError("You have already answered this survey.") from exc
        if locked.anonymous:
            # Into the mixing pool, unlinked from the participation row;
            # written as a response later, shuffled, in another transaction
            # (D56).
            from governance import mixing

            mixing.pool_survey_response(
                locked,
                [
                    {
                        "question": question.pk,
                        "choice": fields["choice"].pk if fields.get("choice") else None,
                        "rating": fields.get("rating"),
                        "text": fields.get("text", ""),
                    }
                    for question in questions
                    for fields in parsed[question.pk]
                ],
            )
        else:
            response = SurveyResponse.objects.create(
                survey=locked, person_id=person_id, submitted_at=timezone.now()
            )
            for question in questions:
                for fields in parsed[question.pk]:
                    SurveyAnswer.objects.create(response=response, question=question, **fields)
    record_event(
        "survey.responded",
        actor=actor,
        request=request,
        target=survey,
        metadata={"anonymous": survey.anonymous},
    )


def my_answers(survey: Survey, user) -> dict[int, list[SurveyAnswer]] | None:
    """A named survey's own answers, by question. None on an anonymous one
    (they cannot be found, by design)."""
    person_id = _person_id(user)
    if survey.anonymous or not person_id:
        return None
    response = (
        SurveyResponse.objects.unscoped("own answers")
        .filter(survey=survey, person_id=person_id)
        .first()
    )
    if response is None:
        return None
    out: dict[int, list[SurveyAnswer]] = defaultdict(list)
    for a in SurveyAnswer.objects.filter(response=response).select_related("choice"):
        out[a.question_id].append(a)
    return dict(out)


# --- Results -------------------------------------------------------------------------------


def results_visible(survey: Survey, user) -> bool:
    """Only once the survey has closed (no running tallies while it is
    open, D61): the board always; others if the board chose to share them."""
    if survey.phase != "closed":
        return False
    if can_manage(user):
        return True
    return can_view(survey, user) and survey.share_results


def results(survey: Survey, *, include_text: bool) -> dict:
    """Totals per question. Free-text answers only with ``include_text``
    (the board); on an anonymous survey they come with no author."""
    pending = 0
    if survey.anonymous:
        from governance import mixing

        mixing.settle_if_final(mixing.SURVEY, survey)  # closed: write the pool first (D56)
        pending = mixing.pending_count(mixing.SURVEY, survey.pk)
    questions = list(SurveyQuestion.objects.filter(survey=survey).prefetch_related("choices"))
    with system_access("survey results"):
        participants = SurveyParticipation.objects.filter(survey=survey).count()
    answers = defaultdict(list)
    for a in SurveyAnswer.objects.filter(response__survey=survey).order_by("id"):
        answers[a.question_id].append(a)
    authors = {}
    if include_text and not survey.anonymous:
        with system_access("named survey authors"):
            authors = dict(
                SurveyResponse.objects.filter(survey=survey).values_list(
                    "id", "person__display_name"
                )
            )
    rows = []
    for q in questions:
        qa = answers.get(q.pk, [])
        answered = len({a.response_id for a in qa})
        row = {"question": q, "answered": answered}
        if q.kind in (QuestionKind.SINGLE, QuestionKind.MULTIPLE):
            counts = defaultdict(int)
            for a in qa:
                counts[a.choice_id] += 1
            row["choices"] = [
                {
                    "label": c.label,
                    "count": counts.get(c.pk, 0),
                    "percent": round(100 * counts.get(c.pk, 0) / answered, 1) if answered else 0,
                }
                for c in q.choices.all()
            ]
        elif q.kind == QuestionKind.RATING:
            ratings = [a.rating for a in qa if a.rating]
            row["average"] = round(sum(ratings) / len(ratings), 1) if ratings else None
            row["choices"] = [
                {
                    "label": str(n),
                    "count": ratings.count(n),
                    "percent": round(100 * ratings.count(n) / len(ratings), 1) if ratings else 0,
                }
                for n in range(1, 6)
            ]
        elif include_text:
            row["texts"] = [
                {"text": a.text, "author": authors.get(a.response_id)} for a in qa if a.text
            ]
        rows.append(row)
    return {
        "rows": rows,
        "responses": participants,
        "audience": len(audience_people(survey)),
        "pending": pending,
    }


def export_csv(survey: Survey, *, actor, request=None) -> HttpResponse:
    """Every response as a row (formula-safe), audited. An anonymous survey
    exports no names or times, in random (id) order. Responses still in the
    mixing pool while the survey is open are not in it yet (D56)."""
    require(actor, "survey.manage")
    if survey.phase != "closed":
        # The answers would be a running tally (D61).
        raise SurveyError("The answers can be downloaded once the survey has closed.")
    if survey.anonymous:
        from governance import mixing

        mixing.settle_if_final(mixing.SURVEY, survey)
    questions = list(SurveyQuestion.objects.filter(survey=survey).prefetch_related("choices"))
    by_response: dict = defaultdict(lambda: defaultdict(list))
    for a in SurveyAnswer.objects.filter(response__survey=survey).select_related("choice"):
        if a.choice_id:
            value = a.choice.label
        elif a.rating:
            value = str(a.rating)
        else:
            value = a.text
        by_response[a.response_id][a.question_id].append(value)
    with system_access("survey export"):
        responses = list(
            SurveyResponse.objects.filter(survey=survey)
            .select_related("person")
            .order_by("id" if survey.anonymous else "submitted_at")
        )
    header = ["Response"] if survey.anonymous else ["Respondent", "Submitted"]
    header += [q.prompt for q in questions]
    rows = []
    for n, r in enumerate(responses, start=1):
        lead = (
            [str(n)]
            if survey.anonymous
            else [
                r.person.display_name if r.person else "",
                timezone.localdate(r.submitted_at).isoformat() if r.submitted_at else "",
            ]
        )
        rows.append(lead + ["; ".join(by_response[r.pk].get(q.pk, [])) for q in questions])
    record_event(
        "survey.exported",
        actor=actor,
        request=request,
        target=survey,
        metadata={"rows": len(rows), "anonymous": survey.anonymous},
    )
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    stamp = timezone.localdate().isoformat()
    response["Content-Disposition"] = f'attachment; filename="survey-{survey.pk}-{stamp}.csv"'
    writer = csv.writer(response)
    writer.writerow([safe_cell(h) for h in header])
    for row in rows:
        writer.writerow([safe_cell(c) for c in row])
    return response
