"""Surveys pages (M5): the list, the board's draft builder, answering, the
results and the CSV export. The rules live in governance.surveys."""

from __future__ import annotations

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from core import policy
from governance import surveys
from governance.models import (
    TEXT_ANSWER_MAX,
    QuestionKind,
    Survey,
    SurveyAudience,
    SurveyQuestion,
)

_DATE = forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")


class SurveyForm(forms.Form):
    title = forms.CharField(max_length=200)
    intro = forms.CharField(
        label="Introduction",
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="Shown above the questions. Say what the survey is for.",
    )
    audience = forms.ChoiceField(
        label="Who is asked", choices=SurveyAudience.choices, initial=SurveyAudience.OWNERS
    )
    opens_on = forms.DateField(label="Opens on", widget=_DATE)
    closes_on = forms.DateField(label="Closes on", widget=_DATE, help_text="Last day for answers.")
    anonymous = forms.BooleanField(
        label="Anonymous",
        required=False,
        help_text="Answers are stored with no link to who gave them. The portal still records "
        "who has responded, to ask each person only once.",
    )
    share_results = forms.BooleanField(
        label="Share the results with owners after it closes",
        required=False,
        help_text="Totals only. Written answers are never shared.",
    )

    def clean(self):
        data = super().clean()
        if data.get("opens_on") and data.get("closes_on") and data["closes_on"] < data["opens_on"]:
            self.add_error("closes_on", "The closing date must be on or after the opening date.")
        return data


class QuestionForm(forms.Form):
    prompt = forms.CharField(label="Question", max_length=300)
    kind = forms.ChoiceField(label="Kind of answer", choices=QuestionKind.choices)
    choices = forms.CharField(
        label="Choices",
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="For single and multiple choice: one choice per line.",
    )
    max_picks = forms.IntegerField(
        label="Most picks allowed",
        required=False,
        min_value=1,
        max_value=surveys.MAX_CHOICES,
        help_text="Multiple choice only. Leave empty for no limit.",
    )
    required = forms.BooleanField(label="An answer is required", required=False)

    def clean(self):
        data = super().clean()
        kind = data.get("kind")
        lines = [c.strip() for c in (data.get("choices") or "").splitlines() if c.strip()]
        if kind in (QuestionKind.SINGLE, QuestionKind.MULTIPLE) and len(lines) < 2:
            self.add_error("choices", "Give at least two choices, one per line.")
        data["choice_list"] = lines
        return data

    def service_kwargs(self) -> dict:
        d = self.cleaned_data
        return {
            "kind": d["kind"],
            "prompt": d["prompt"],
            "required": d["required"],
            "choices": d["choice_list"],
            "max_picks": d.get("max_picks") if d["kind"] == QuestionKind.MULTIPLE else None,
        }


def _question_initial(q: SurveyQuestion) -> dict:
    return {
        "prompt": q.prompt,
        "kind": q.kind,
        "choices": "\n".join(c.label for c in q.choices.all()),
        "max_picks": q.max_picks,
        "required": q.required,
    }


RATING_CHOICES = [(str(n), str(n)) for n in range(1, 6)]


class AnswerForm(forms.Form):
    """One field per question, named q<id>."""

    def __init__(self, questions, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.questions = questions
        for q in questions:
            name = f"q{q.pk}"
            options = [(str(c.pk), c.label) for c in q.choices.all()]
            if q.kind == QuestionKind.SINGLE:
                field = forms.ChoiceField(choices=options, widget=forms.RadioSelect)
            elif q.kind == QuestionKind.MULTIPLE:
                field = forms.MultipleChoiceField(
                    choices=options, widget=forms.CheckboxSelectMultiple
                )
            elif q.kind == QuestionKind.RATING:
                field = forms.ChoiceField(choices=RATING_CHOICES, widget=forms.RadioSelect)
            else:
                field = forms.CharField(
                    max_length=TEXT_ANSWER_MAX,
                    widget=forms.Textarea(attrs={"rows": 3, "maxlength": TEXT_ANSWER_MAX}),
                )
            field.label = q.prompt
            field.required = q.required
            self.fields[name] = field

    def clean(self):
        data = super().clean()
        for q in self.questions:
            picked = data.get(f"q{q.pk}")
            if q.kind == QuestionKind.MULTIPLE and q.max_picks and len(picked or []) > q.max_picks:
                self.add_error(f"q{q.pk}", f"Choose at most {q.max_picks}.")
        return data

    def answers(self) -> dict[int, list[str]]:
        out = {}
        for q in self.questions:
            value = self.cleaned_data.get(f"q{q.pk}")
            if isinstance(value, list):
                out[q.pk] = value
            elif value not in (None, ""):
                out[q.pk] = [value]
        return out

    def rows(self):
        """(question, bound field) pairs for the template."""
        return [(q, self[f"q{q.pk}"]) for q in self.questions]


def _visible(request, pk) -> Survey:
    survey = get_object_or_404(Survey, pk=pk)
    if not surveys.can_view(survey, request.user):
        raise Http404  # drafts and other audiences' surveys are invisible
    return survey


def _render_detail(
    request,
    survey,
    *,
    answer_form=None,
    settings_form=None,
    question_form=None,
    edit_forms=None,
    status=200,
):
    user = request.user
    can_manage = surveys.can_manage(user)
    questions = list(survey.questions.prefetch_related("choices"))
    can_respond = surveys.can_respond(survey, user)
    if can_respond and answer_form is None:
        answer_form = AnswerForm(questions)
    context = {
        "survey": survey,
        "questions": questions,
        "can_manage": can_manage,
        "can_respond": can_respond,
        "answer_form": answer_form if can_respond else None,
        "responded_on": surveys.has_responded(survey, user),
        "my_answers": surveys.my_answers(survey, user),
        "results_visible": surveys.results_visible(survey, user),
    }
    if survey.status != "draft":
        from governance import window

        context.update(window.page_context(survey, user))
    if can_manage and survey.status == "draft":
        context["settings_form"] = settings_form or SurveyForm(
            initial={
                f: getattr(survey, f)
                for f in (
                    "title",
                    "intro",
                    "audience",
                    "opens_on",
                    "closes_on",
                    "anonymous",
                    "share_results",
                )
            },
            prefix="s",
        )
        context["question_form"] = question_form or QuestionForm(prefix="new")
        edit_forms = edit_forms or {}
        context["question_rows"] = [
            (
                q,
                edit_forms.get(q.pk)
                or QuestionForm(initial=_question_initial(q), prefix=f"e{q.pk}"),
            )
            for q in questions
        ]
    return render(request, "governance/survey_detail.html", context, status=status)


@login_required
def survey_detail(request, pk):
    policy.require(request.user, "survey.view")
    survey = _visible(request, pk)
    if request.method != "POST":
        return _render_detail(request, survey)
    # Answering the survey.
    policy.require(request.user, "survey.respond")
    if not surveys.can_respond(survey, request.user):
        messages.error(request, "This survey is not open for your answers.")
        return redirect("survey_detail", pk=survey.pk)
    questions = list(survey.questions.prefetch_related("choices"))
    form = AnswerForm(questions, request.POST)
    if form.is_valid():
        try:
            surveys.submit_response(
                survey, answers=form.answers(), actor=request.user, request=request
            )
        except surveys.SurveyError as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(request, "Thank you. Your answers were recorded.")
            return redirect("survey_detail", pk=survey.pk)
    return _render_detail(request, survey, answer_form=form)


@login_required
@require_POST
def survey_action(request, pk, action):
    policy.require(request.user, "survey.manage")
    survey = get_object_or_404(Survey, pk=pk)
    user = request.user
    try:
        if action == "settings":
            form = SurveyForm(request.POST, prefix="s")
            if not form.is_valid():
                return _render_detail(request, survey, settings_form=form)
            try:
                surveys.update_survey(survey, actor=user, request=request, **form.cleaned_data)
            except surveys.SurveyError as exc:
                form.add_error(None, str(exc))
                return _render_detail(request, survey, settings_form=form)
            messages.success(request, "Survey details saved.")
        elif action == "question":
            form = QuestionForm(request.POST, prefix="new")
            if not form.is_valid():
                return _render_detail(request, survey, question_form=form)
            try:
                surveys.add_question(survey, actor=user, request=request, **form.service_kwargs())
            except surveys.SurveyError as exc:
                form.add_error(None, str(exc))
                return _render_detail(request, survey, question_form=form)
            messages.success(request, "Question added.")
        elif action == "open":
            surveys.open_survey(
                survey, notify=request.POST.get("notify") == "1", actor=user, request=request
            )
            messages.success(request, "Survey opened. Its questions can no longer change.")
        elif action == "close":
            surveys.close_survey(survey, actor=user, request=request)
            messages.success(request, "Survey closed. No more answers can be given.")
        elif action in ("share", "unshare"):
            surveys.set_sharing(survey, share=action == "share", actor=user, request=request)
            messages.success(
                request,
                "Owners will see the totals once it closes."
                if action == "share"
                else "Results are no longer shared with owners.",
            )
        else:
            raise Http404
    except surveys.SurveyError as exc:
        messages.error(request, str(exc))
    return redirect("survey_detail", pk=survey.pk)


@login_required
@require_POST
def survey_question_action(request, pk, action):
    policy.require(request.user, "survey.manage")
    question = get_object_or_404(SurveyQuestion.objects.select_related("survey"), pk=pk)
    survey = question.survey
    user = request.user
    try:
        if action == "edit":
            form = QuestionForm(request.POST, prefix=f"e{question.pk}")
            if not form.is_valid():
                return _render_detail(request, survey, edit_forms={question.pk: form})
            try:
                surveys.edit_question(
                    question, actor=user, request=request, **form.service_kwargs()
                )
            except surveys.SurveyError as exc:
                form.add_error(None, str(exc))
                return _render_detail(request, survey, edit_forms={question.pk: form})
            messages.success(request, "Question saved.")
        elif action in ("up", "down"):
            surveys.move_question(question, up=action == "up", actor=user, request=request)
        elif action == "remove":
            surveys.remove_question(question, actor=user, request=request)
            messages.success(request, "Question removed.")
        else:
            raise Http404
    except surveys.SurveyError as exc:
        messages.error(request, str(exc))
    return redirect("survey_detail", pk=survey.pk)


@login_required
def survey_results(request, pk):
    policy.require(request.user, "survey.view")
    survey = _visible(request, pk)
    if not surveys.results_visible(survey, request.user):
        raise Http404
    can_manage = surveys.can_manage(request.user)
    return render(
        request,
        "governance/survey_results.html",
        {
            "survey": survey,
            "results": surveys.results(survey, include_text=can_manage),
            "can_manage": can_manage,
        },
    )


@login_required
def survey_export(request, pk):
    policy.require(request.user, "survey.manage")
    survey = get_object_or_404(Survey, pk=pk)
    try:
        return surveys.export_csv(survey, actor=request.user, request=request)
    except surveys.SurveyError as exc:
        messages.error(request, str(exc))
        return redirect("survey_detail", pk=survey.pk)
