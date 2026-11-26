"""Surveys (M5): informal multi-question surveys."""

import datetime
import uuid

import pytest
from django.db import DatabaseError, connection, transaction
from django.urls import reverse
from django.utils import timezone

from audit.models import AuditEvent
from community import services as community
from community.models import Account, Person
from community.tests.factories import (
    LotFactory,
    UserFactory,
    days_ago,
    enroll_totp,
    make_member,
)
from core.append_only import AppendOnlyError
from core.my_items import items_for
from core.policy import SYSTEM
from core.scoping import system_access
from governance import surveys
from governance.models import (
    Survey,
    SurveyAnswer,
    SurveyChoice,
    SurveyParticipation,
    SurveyQuestion,
    SurveyResponse,
)

pytestmark = pytest.mark.django_db

DB_REFUSED = (DatabaseError,)  # IntegrityError, InternalError, ProgrammingError


def _person(user):
    with system_access("test"):
        return Person.objects.get(user=user)


def _account(lot):
    with system_access("test"):
        return Account.objects.get(lot=lot, closed_on__isnull=True)


def _contact_user(account, kind, name):
    user = UserFactory()
    person = community.record_person(display_name=name, actor=SYSTEM)
    with system_access("test"):
        Person.objects.filter(pk=person.pk).update(user=user)
    enroll_totp(user)
    community.add_account_contact(account, person=person, kind=kind, actor=SYSTEM)
    return user


@pytest.fixture
def w():
    lot_a, lot_b = LotFactory(), LotFactory()
    owner_a = make_member(lots=[lot_a])
    owner_b = make_member(lots=[lot_b])
    tenant = _contact_user(_account(lot_a), "tenant", "Tess Tenant")
    return {
        "board": make_member(board=True),
        "owner_a": owner_a,
        "owner_b": owner_b,
        "tenant": tenant,
        "lot_a": lot_a,
        "lot_b": lot_b,
    }


def _survey(w, *, anonymous=False, audience="owners", share=False, opens_in=0, days=7):
    today = timezone.localdate()
    survey = surveys.create_survey(
        title="Pool hours survey",
        intro="Tell us when you swim.",
        audience=audience,
        opens_on=today + datetime.timedelta(days=opens_in),
        closes_on=today + datetime.timedelta(days=opens_in + days),
        anonymous=anonymous,
        share_results=share,
        actor=w["board"],
    )
    surveys.add_question(
        survey,
        kind="single",
        prompt="Best opening time?",
        choices=["7 am", "9 am"],
        required=True,
        actor=w["board"],
    )
    surveys.add_question(
        survey,
        kind="multiple",
        prompt="Which days?",
        choices=["Mon", "Tue", "Wed"],
        max_picks=2,
        actor=w["board"],
    )
    surveys.add_question(survey, kind="rating", prompt="Rate the pool", actor=w["board"])
    surveys.add_question(survey, kind="text", prompt="Anything else?", actor=w["board"])
    return survey


def _q(survey):
    return {q.kind: q for q in survey.questions.prefetch_related("choices")}


def _answers(survey, *, single=0, multiple=(0,), rating=4, text="Longer hours please"):
    q = _q(survey)
    choices = {k: [c.pk for c in v.choices.all()] for k, v in q.items()}
    return {
        q["single"].pk: [str(choices["single"][single])],
        q["multiple"].pk: [str(choices["multiple"][i]) for i in multiple],
        q["rating"].pk: [str(rating)],
        q["text"].pk: [text],
    }


def _answer(survey, user, **kw):
    surveys.submit_response(survey, answers=_answers(survey, **kw), actor=user)


# --- Drafting and freezing --------------------------------------------------------------


def test_draft_can_change_and_an_opened_survey_is_frozen(w):
    survey = _survey(w)
    q = _q(survey)
    surveys.move_question(q["text"], up=True, actor=w["board"])
    assert [x.kind for x in survey.questions.all()] == ["single", "multiple", "text", "rating"]
    surveys.edit_question(
        q["single"], kind="single", prompt="Opening time?", choices=["6", "7"], actor=w["board"]
    )
    extra = surveys.add_question(survey, kind="text", prompt="Remove me", actor=w["board"])
    surveys.remove_question(extra, actor=w["board"])
    assert survey.questions.count() == 4

    surveys.open_survey(survey, actor=w["board"])
    q = _q(survey)
    for attempt in (
        lambda: surveys.add_question(survey, kind="text", prompt="Late", actor=w["board"]),
        lambda: surveys.edit_question(q["text"], kind="text", prompt="x", actor=w["board"]),
        lambda: surveys.move_question(q["text"], up=True, actor=w["board"]),
        lambda: surveys.remove_question(q["text"], actor=w["board"]),
        lambda: surveys.update_survey(
            survey,
            title="New",
            intro="",
            audience="owners",
            opens_on=survey.opens_on,
            closes_on=survey.closes_on,
            anonymous=True,
            share_results=False,
            actor=w["board"],
        ),
        lambda: surveys.open_survey(survey, actor=w["board"]),
    ):
        with pytest.raises(surveys.SurveyError):
            attempt()
    # The database refuses too (the backstop behind the service).
    with pytest.raises(DB_REFUSED), transaction.atomic():
        SurveyQuestion.objects.filter(pk=q["text"].pk).update(prompt="changed")
    with pytest.raises(DB_REFUSED), transaction.atomic():
        SurveyQuestion.objects.create(survey=survey, kind="text", prompt="sneaked in")
    with pytest.raises(DB_REFUSED), transaction.atomic():
        SurveyChoice.objects.create(question=q["single"], label="Midnight")
    with pytest.raises(DB_REFUSED), transaction.atomic():
        SurveyChoice.objects.filter(question=q["single"]).delete()
    with pytest.raises(DB_REFUSED), transaction.atomic():
        Survey.objects.filter(pk=survey.pk).update(anonymous=True)
    with pytest.raises(DB_REFUSED), transaction.atomic():
        Survey.objects.filter(pk=survey.pk).update(status="draft")
    # Sharing results is a display choice and stays changeable.
    surveys.set_sharing(survey, share=True, actor=w["board"])
    assert Survey.objects.get(pk=survey.pk).share_results


def test_only_the_board_drafts_and_a_survey_needs_a_question(w):
    from django.core.exceptions import PermissionDenied

    today = timezone.localdate()
    with pytest.raises(PermissionDenied):
        surveys.create_survey(title="x", opens_on=today, closes_on=today, actor=w["owner_a"])
    manager = make_member(roles=["manager"])
    with pytest.raises(PermissionDenied):
        surveys.create_survey(title="x", opens_on=today, closes_on=today, actor=manager)
    empty = surveys.create_survey(title="Empty", opens_on=today, closes_on=today, actor=w["board"])
    with pytest.raises(surveys.SurveyError):
        surveys.open_survey(empty, actor=w["board"])
    with pytest.raises(surveys.SurveyError):  # choice questions need two choices
        surveys.add_question(empty, kind="single", prompt="?", choices=["Only"], actor=w["board"])
    with pytest.raises(surveys.SurveyError):
        surveys.create_survey(
            title="Backwards",
            opens_on=today,
            closes_on=today - datetime.timedelta(days=1),
            actor=w["board"],
        )
    assert AuditEvent.objects.filter(action="survey.created").count() == 1


# --- Answering ---------------------------------------------------------------------------


def test_one_response_per_person(w):
    survey = _survey(w)
    surveys.open_survey(survey, actor=w["board"])
    _answer(survey, w["owner_a"])
    with pytest.raises(surveys.SurveyError):
        _answer(survey, w["owner_a"], single=1)
    with system_access("t"):
        assert SurveyParticipation.objects.filter(survey=survey).count() == 1
        assert SurveyResponse.objects.filter(survey=survey).count() == 1
    assert AuditEvent.objects.filter(action="survey.responded").count() == 1


def test_answers_are_checked(w):
    survey = _survey(w)
    surveys.open_survey(survey, actor=w["board"])
    q = _q(survey)
    bad = [
        {**_answers(survey), q["single"].pk: []},  # required
        _answers(survey, multiple=(0, 1, 2)),  # more than max picks
        _answers(survey, rating=6),
        _answers(survey, text="x" * 1001),
        {**_answers(survey), q["single"].pk: ["999999"]},  # not one of its choices
        {**_answers(survey), q["single"].pk: [str(c.pk) for c in q["single"].choices.all()]},
    ]
    for answers in bad:
        with pytest.raises(surveys.SurveyError):
            surveys.submit_response(survey, answers=answers, actor=w["owner_a"])
    with system_access("t"):
        assert not SurveyParticipation.objects.filter(survey=survey).exists()


def test_answers_only_while_open(w):
    later = _survey(w, opens_in=3)
    surveys.open_survey(later, actor=w["board"])
    with pytest.raises(surveys.SurveyError):
        _answer(later, w["owner_a"])
    draft = _survey(w)
    with pytest.raises(surveys.SurveyError):
        _answer(draft, w["owner_a"])
    closed = _survey(w)
    surveys.open_survey(closed, actor=w["board"])
    surveys.close_survey(closed, actor=w["board"])
    with pytest.raises(surveys.SurveyError):
        _answer(closed, w["owner_a"])
    with pytest.raises(DB_REFUSED), transaction.atomic():  # reopening is refused
        Survey.objects.filter(pk=closed.pk).update(status="open")


def test_audience_owners_only_excludes_tenants(w, client):
    survey = _survey(w, audience="owners")
    surveys.open_survey(survey, actor=w["board"])
    with pytest.raises(surveys.SurveyError):
        _answer(survey, w["tenant"])
    client.force_login(w["tenant"])
    assert client.get(reverse("survey_detail", args=[survey.pk])).status_code == 404
    assert b"Pool hours survey" not in client.get(reverse("surveys"), follow=True).content
    assert not surveys.waiting_for(w["tenant"])

    residents = _survey(w, audience="residents")
    surveys.open_survey(residents, actor=w["board"])
    assert client.get(reverse("survey_detail", args=[residents.pk])).status_code == 200
    _answer(residents, w["tenant"])
    assert surveys.has_responded(residents, w["tenant"]) == timezone.localdate()


def test_view_only_contact_reads_but_cannot_answer(w, client):
    """R2-005: view-only contacts "see, cannot act"."""
    from django.core.exceptions import PermissionDenied

    viewer = _contact_user(_account(w["lot_a"]), "view_only", "Vera Viewer")
    authorized = _contact_user(_account(w["lot_a"]), "authorized", "Al Authorized")
    survey = _survey(w, audience="residents", share=True)
    surveys.open_survey(survey, actor=w["board"])
    url = reverse("survey_detail", args=[survey.pk])

    assert not surveys.can_respond(survey, viewer)
    assert not surveys.waiting_for(viewer)
    with pytest.raises(PermissionDenied):
        _answer(survey, viewer)
    assert _person(viewer).pk not in surveys.audience_people(survey)
    client.force_login(viewer)
    page = client.get(url)
    assert page.status_code == 200
    assert b"Send answers" not in page.content and b"not include answering" in page.content
    data = {f"q{k}": v for k, v in _answers(survey).items()}
    assert client.post(url, data).status_code == 403
    assert surveys.has_responded(survey, viewer) is None

    # The authorized contact on the same account still answers.
    assert surveys.can_respond(survey, authorized)
    _answer(survey, authorized)

    # Shared results stay visible to the view-only contact once closed.
    surveys.close_survey(survey, actor=w["board"])
    assert client.get(reverse("survey_results", args=[survey.pk])).status_code == 200


def test_former_owner_cannot_answer(w, client):
    survey = _survey(w)
    surveys.open_survey(survey, actor=w["board"])
    buyer = make_member()
    community.transfer_lot(
        lot=w["lot_b"], new_owners=[_person(buyer)], transfer_date=days_ago(0), actor=SYSTEM
    )
    from django.core.exceptions import PermissionDenied

    with pytest.raises(PermissionDenied):
        _answer(survey, w["owner_b"])
    client.force_login(w["owner_b"])
    assert client.get(reverse("survey_detail", args=[survey.pk])).status_code == 403
    response = client.post(reverse("survey_detail", args=[survey.pk]), {})
    assert response.status_code == 403
    assert not surveys.waiting_for(w["owner_b"])
    _answer(survey, buyer)  # the new owner can


# --- Anonymity ---------------------------------------------------------------------------


def test_anonymous_answers_have_no_link_to_the_person(w):
    survey = _survey(w, anonymous=True)
    surveys.open_survey(survey, actor=w["board"])
    _answer(survey, w["owner_a"])
    surveys.close_survey(survey, actor=w["board"])  # writes the mixing pool (D56)
    person = _person(w["owner_a"])
    with system_access("t"):
        part = SurveyParticipation.objects.get(survey=survey)
        response = SurveyResponse.objects.get(survey=survey)
    assert part.person_id == person.pk
    assert part.responded_on == timezone.localdate()  # day precision only
    assert response.person_id is None and response.submitted_at is None
    assert isinstance(response.pk, uuid.UUID)
    # No person, lot, time or sequential id on the answer rows.
    fields = {f.name for f in SurveyAnswer._meta.get_fields()}
    assert fields == {"id", "response", "question", "choice", "rating", "text"}
    assert SurveyAnswer._meta.pk.get_internal_type() == "UUIDField"
    assert {f.name for f in SurveyResponse._meta.get_fields()} == {
        "id",
        "survey",
        "person",
        "submitted_at",
    }
    assert {f.get_internal_type() for f in SurveyParticipation._meta.get_fields()} >= {"DateField"}
    assert surveys.my_answers(survey, w["owner_a"]) is None
    # The database refuses a named response on an anonymous survey.
    with pytest.raises(DB_REFUSED), transaction.atomic():
        SurveyResponse.objects.create(survey=survey, person=person)
    with pytest.raises(DB_REFUSED), transaction.atomic():
        SurveyResponse.objects.create(survey=survey, submitted_at=timezone.now())


def test_named_survey_links_answers_to_the_person(w):
    survey = _survey(w)
    surveys.open_survey(survey, actor=w["board"])
    _answer(survey, w["owner_a"])
    with system_access("t"):
        response = SurveyResponse.objects.get(survey=survey)
    assert response.person_id == _person(w["owner_a"]).pk
    assert response.submitted_at is not None
    mine = surveys.my_answers(survey, w["owner_a"])
    assert mine[_q(survey)["text"].pk][0].text == "Longer hours please"
    with pytest.raises(DB_REFUSED), transaction.atomic():
        SurveyResponse.objects.create(survey=survey)  # a named survey names the person


def test_submitted_answers_are_append_only(w):
    survey = _survey(w, anonymous=True)
    surveys.open_survey(survey, actor=w["board"])
    _answer(survey, w["owner_a"])
    surveys.close_survey(survey, actor=w["board"])  # writes the mixing pool (D56)
    answer = SurveyAnswer.objects.filter(response__survey=survey).first()
    answer.text = "changed"
    with pytest.raises(AppendOnlyError):
        answer.save()
    with pytest.raises(AppendOnlyError):
        answer.delete()
    with pytest.raises(AppendOnlyError):
        SurveyAnswer.objects.filter(pk=answer.pk).update(text="x")
    with system_access("t"), pytest.raises(AppendOnlyError):
        SurveyParticipation.objects.filter(survey=survey).delete()
    for sql in (
        "DELETE FROM governance_surveyanswer",
        "DELETE FROM governance_surveyresponse",
        "DELETE FROM governance_surveyparticipation",
        "UPDATE governance_surveyanswer SET text = 'x'",
        "UPDATE governance_surveyparticipation SET responded_on = responded_on - 1",
    ):
        with pytest.raises(DB_REFUSED), transaction.atomic(), connection.cursor() as cur:
            cur.execute(sql)


def test_runtime_role_cannot_change_responses():
    from core.db_roles import append_only_tables

    tables = set(append_only_tables())
    assert {
        "governance_surveyanswer",
        "governance_surveyresponse",
        "governance_surveyparticipation",
    } <= tables


# --- Results and export -----------------------------------------------------------------


def test_results_counts_and_averages(w):
    survey = _survey(w)
    surveys.open_survey(survey, actor=w["board"])
    _answer(survey, w["owner_a"], single=0, multiple=(0, 1), rating=4)
    _answer(survey, w["owner_b"], single=1, multiple=(1,), rating=5, text="")
    res = surveys.results(survey, include_text=True)
    rows = {r["question"].kind: r for r in res["rows"]}
    assert res["responses"] == 2
    assert [c["count"] for c in rows["single"]["choices"]] == [1, 1]
    assert [c["percent"] for c in rows["single"]["choices"]] == [50.0, 50.0]
    assert [c["count"] for c in rows["multiple"]["choices"]] == [1, 2, 0]
    assert rows["multiple"]["choices"][1]["percent"] == 100.0
    assert rows["rating"]["average"] == 4.5
    assert rows["text"]["texts"] == [
        {"text": "Longer hours please", "author": _person(w["owner_a"]).display_name}
    ]
    assert "texts" not in {
        k for r in surveys.results(survey, include_text=False)["rows"] for k in r
    }


def test_results_visibility_rules(w, client):
    survey = _survey(w, anonymous=True)
    surveys.open_survey(survey, actor=w["board"])
    _answer(survey, w["owner_a"], text="Secret pond complaint")
    url = reverse("survey_results", args=[survey.pk])
    client.force_login(w["owner_b"])
    assert client.get(url).status_code == 404  # still open
    client.force_login(w["board"])
    page = client.get(url)
    # Still in the mixing pool while open (D56): counted, not yet shown.
    assert page.status_code == 200 and b"Secret pond complaint" not in page.content
    assert b"1 anonymous response is waiting to be mixed" in page.content
    surveys.close_survey(survey, actor=w["board"])
    page = client.get(url)
    assert page.status_code == 200 and b"Secret pond complaint" in page.content
    assert b"waiting to be mixed" not in page.content
    assert _person(w["owner_a"]).display_name.encode() not in page.content  # anonymous
    client.force_login(w["owner_b"])
    assert client.get(url).status_code == 404  # closed, but not shared
    surveys.set_sharing(survey, share=True, actor=w["board"])
    page = client.get(url)
    assert page.status_code == 200
    assert b"7 am" in page.content
    assert b"Secret pond complaint" not in page.content  # free text never shared
    client.force_login(w["tenant"])
    assert client.get(url).status_code == 404  # not in the owners-only audience


def test_export_is_audited_and_anonymous_safe(w, client):
    anon = _survey(w, anonymous=True)
    surveys.open_survey(anon, actor=w["board"])
    _answer(anon, w["owner_a"], text="=HYPERLINK(1)")
    surveys.close_survey(anon, actor=w["board"])  # writes the mixing pool (D56)
    named = _survey(w)
    surveys.open_survey(named, actor=w["board"])
    _answer(named, w["owner_a"])
    name = _person(w["owner_a"]).display_name.encode()

    client.force_login(w["owner_a"])
    assert client.get(reverse("survey_export", args=[anon.pk])).status_code == 403
    client.force_login(w["board"])
    csv_anon = client.get(reverse("survey_export", args=[anon.pk]))
    assert csv_anon.status_code == 200
    body = b"".join(csv_anon.streaming_content) if csv_anon.streaming else csv_anon.content
    assert name not in body
    assert b"Respondent" not in body and b"Submitted" not in body
    assert b"'=HYPERLINK(1)" in body  # formula-safe
    body_named = client.get(reverse("survey_export", args=[named.pk])).content
    assert name in body_named
    events = AuditEvent.objects.filter(action="survey.exported").order_by("occurred_at")
    assert [e.metadata["anonymous"] for e in events] == [True, False]
    assert events[0].target_id == str(anon.pk)


# --- My items, email, pages --------------------------------------------------------------


def test_my_items_asks_the_audience_until_they_answer(w):
    survey = _survey(w)

    def titles(user):
        return [i.title for i in items_for(user) if i.title.startswith("Survey:")]

    assert titles(w["owner_a"]) == []  # draft
    surveys.open_survey(survey, actor=w["board"])
    assert titles(w["owner_a"]) == ["Survey: Pool hours survey"]
    assert titles(w["tenant"]) == []  # owners only
    assert titles(w["board"]) == []  # not an owner
    _answer(survey, w["owner_a"])
    assert titles(w["owner_a"]) == []
    item = next(i for i in items_for(w["owner_b"]) if i.title.startswith("Survey:"))
    assert "waiting for your answer" in item.detail and item.nav == "voting"
    surveys.close_survey(survey, actor=w["board"])
    assert titles(w["owner_b"]) == []


def test_opening_can_email_the_audience(w, mailoutbox, django_capture_on_commit_callbacks):
    survey = _survey(w, audience="residents")
    with django_capture_on_commit_callbacks(execute=True):
        surveys.open_survey(survey, notify=True, actor=w["board"])
    to = {m.to[0] for m in mailoutbox}
    assert to == {w["owner_a"].email, w["owner_b"].email, w["tenant"].email}
    assert all(reverse("survey_detail", args=[survey.pk]) in m.body for m in mailoutbox)
    event = AuditEvent.objects.get(action="survey.opened")
    assert event.metadata["emailed"] == 3
    quiet = _survey(w)
    with django_capture_on_commit_callbacks(execute=True):
        surveys.open_survey(quiet, actor=w["board"])
    assert len(mailoutbox) == 3


def test_owner_answers_through_the_page(w, client):
    survey = _survey(w)
    surveys.open_survey(survey, actor=w["board"])
    client.force_login(w["owner_a"])
    url = reverse("survey_detail", args=[survey.pk])
    page = client.get(url)
    assert page.status_code == 200 and b"Send answers" in page.content
    q = _q(survey)
    # Missing the required question: the form comes back with the answers kept.
    page = client.post(url, {f"q{q['text'].pk}": "Keep this text"})
    assert page.status_code == 200
    assert b"Keep this text" in page.content and b'role="alert"' in page.content
    data = {f"q{k}": v for k, v in _answers(survey).items()}
    response = client.post(url, data)
    assert response.status_code == 302
    page = client.get(url)
    assert b"You answered on" in page.content and b"Send answers" not in page.content


def test_board_builds_a_draft_through_the_pages(w, client):
    client.force_login(w["board"])
    today = timezone.localdate()
    response = client.post(
        reverse("surveys"),
        {
            "title": "Landscaping survey",
            "audience": "owners",
            "opens_on": today.isoformat(),
            "closes_on": (today + datetime.timedelta(days=5)).isoformat(),
        },
    )
    survey = Survey.objects.get(title="Landscaping survey")
    assert response.status_code == 302
    action = reverse("survey_action", args=[survey.pk, "question"])
    bad = client.post(action, {"new-prompt": "Colors?", "new-kind": "single", "new-choices": "x"})
    assert bad.status_code == 200 and b"at least two choices" in bad.content
    client.post(action, {"new-prompt": "Colors?", "new-kind": "single", "new-choices": "Red\nBlue"})
    client.post(action, {"new-prompt": "Ideas?", "new-kind": "text"})
    first = survey.questions.first()
    client.post(reverse("survey_question_action", args=[first.pk, "down"]))
    assert [x.prompt for x in survey.questions.all()] == ["Ideas?", "Colors?"]
    assert client.get(reverse("survey_detail", args=[survey.pk])).status_code == 200
    client.post(reverse("survey_action", args=[survey.pk, "open"]))
    survey.refresh_from_db()
    assert survey.status == "open"
    client.post(reverse("survey_question_action", args=[first.pk, "remove"]))
    assert survey.questions.count() == 2  # frozen
    assert client.get(reverse("survey_results", args=[survey.pk])).status_code == 200


def test_owner_cannot_see_drafts_or_board_steps(w, client):
    survey = _survey(w)
    client.force_login(w["owner_a"])
    assert client.get(reverse("survey_detail", args=[survey.pk])).status_code == 404
    assert client.get(reverse("survey_results", args=[survey.pk])).status_code == 404
    assert b"Pool hours survey" not in client.get(reverse("surveys"), follow=True).content
    for name, kwargs in (
        ("survey_action", {"pk": survey.pk, "action": "open"}),
        ("survey_question_action", {"pk": survey.questions.first().pk, "action": "remove"}),
    ):
        assert client.post(reverse(name, kwargs=kwargs)).status_code == 403
    survey.refresh_from_db()
    assert survey.status == "draft" and survey.questions.count() == 4


def test_tenant_reaches_surveys_from_the_voting_hub(w, client):
    survey = _survey(w, audience="residents")
    surveys.open_survey(survey, actor=w["board"])
    client.force_login(w["tenant"])
    page = client.get(reverse("voting"))
    assert page.status_code == 200
    assert b"Pool hours survey" in page.content
    assert b"No votes yet" not in page.content and b"<h2>Board elections" not in page.content


def test_surveys_feature_switch(w, client):
    from core.models import FeatureSetting

    survey = _survey(w)
    surveys.open_survey(survey, actor=w["board"])
    FeatureSetting.objects.create(key="surveys", enabled=False)
    client.force_login(w["owner_a"])
    assert client.get(reverse("surveys")).status_code == 404
    assert client.get(reverse("survey_detail", args=[survey.pk])).status_code == 404
    assert not [i for i in items_for(w["owner_a"]) if i.title.startswith("Survey:")]
