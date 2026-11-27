"""Voting windows, automatic closing, extensions, reminders and live turnout
(D61): every formal vote and election is timeboxed; the board and the
manager see "N of M homes have voted" and who has voted (never choices)
while voting is open; nobody sees running tallies; owners see turnout only
when a rule allows."""

from __future__ import annotations

import datetime
import re

import pytest
from django.core import mail
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone

from audit.actions import ACTIONS as AUDIT_ACTIONS
from audit.models import AuditEvent
from community import services as community
from community.models import Role
from community.tests.factories import LotFactory, make_member
from core import my_items
from core.policy import SYSTEM
from core.scoping import system_access
from governance import elections, mixing, services, surveys, window
from governance.models import (
    ElectionChoice,
    SecretChoice,
    Survey,
    Vote,
    VoteKind,
    VoteStatus,
)
from governance.tests.helpers import allow_early_close, legacy_poll

pytestmark = pytest.mark.django_db

DAY = datetime.timedelta(days=1)
HOUR = datetime.timedelta(hours=1)


@pytest.fixture
def w():
    board = make_member(board=True, lots=[LotFactory(street_address="1 Board Lane")])
    owners = [
        make_member(lots=[LotFactory(street_address=f"{n} Oak Way")], email=f"o{n}@example.com")
        for n in (10, 20, 30)
    ]
    manager = make_member(roles=[Role.MANAGER])
    community.set_rule(key="vote.quorum_percent", value="50", citation="Bylaws §2.6", actor=board)
    community.set_rule(key="vote.pass_percent", value="51", citation="Bylaws §2.7", actor=board)
    return {"board": board, "owners": owners, "manager": manager}


def _lot(user):
    from core import policy

    lot_id = next(iter(policy.roles_for(user).owned_lot_ids))
    with system_access("test"):
        from community.models import Lot

        return Lot.objects.get(pk=lot_id)


def _vote(w, *, secret=False, days=8, opens_at=None):
    vote = services.create_vote(
        title="Repave the pond path?",
        kind=VoteKind.FORMAL,
        options=["Yes", "No"],
        secret=secret,
        actor=w["board"],
    )
    now = timezone.now()
    return services.open_vote(
        vote, opens_at=opens_at or now, closes_at=(opens_at or now) + days * DAY, actor=w["board"]
    )


def _cast(vote, user, label="Yes"):
    option = vote.options.get(label=label)
    services.cast_ballot(vote, option_id=option.pk, lot_id=_lot(user).pk, actor=user)


def _election(w, days=8):
    e = elections.create_election(title="2027 board election", seats=1, actor=w["board"])
    elections.nominate(e, person=w["board"].person, actor=w["board"])
    return elections.open_voting(
        e, opens_at=timezone.now(), closes_at=timezone.now() + days * DAY, actor=w["board"]
    )


def _survey(w, days=3):
    today = timezone.localdate()
    survey = surveys.create_survey(
        title="Pool hours",
        opens_on=today,
        closes_on=today + datetime.timedelta(days=days),
        actor=w["board"],
    )
    surveys.add_question(
        survey, kind="single", prompt="Best time?", choices=["7 am", "9 am"], actor=w["board"]
    )
    return surveys.open_survey(survey, actor=w["board"])


def _answer(survey, user, label="7 am"):
    q = survey.questions.get()
    choice = q.choices.get(label=label)
    surveys.submit_response(survey, answers={q.pk: [str(choice.pk)]}, actor=user)


@pytest.fixture
def clock(monkeypatch):
    """Move "now" for the code under test (no freezegun in this project)."""

    def at(moment):
        monkeypatch.setattr(timezone, "now", lambda: moment)
        return moment

    return at


# --- The window is required ---------------------------------------------------------


def test_a_formal_vote_cannot_open_without_a_window(w):
    vote = services.create_vote(
        title="Fence?", kind=VoteKind.FORMAL, options=["Yes", "No"], actor=w["board"]
    )
    with pytest.raises(services.GovernanceError, match="opens and when it closes"):
        services.open_vote(vote, actor=w["board"])
    now = timezone.now()
    with pytest.raises(services.GovernanceError, match="close after it opens"):
        services.open_vote(vote, opens_at=now, closes_at=now - HOUR, actor=w["board"])
    vote.refresh_from_db()
    assert vote.status == VoteStatus.DRAFT


def test_the_shortest_voting_period_is_a_rule_with_a_default_of_seven_days(w):
    from community.rules import REGISTRY, rule_origin

    assert REGISTRY["vote.min_open_days"].default == "7"
    assert rule_origin("vote.min_open_days") == "default"
    vote = services.create_vote(
        title="Fence?", kind=VoteKind.FORMAL, options=["Yes", "No"], actor=w["board"]
    )
    now = timezone.now()
    with pytest.raises(services.GovernanceError, match="at least 7 days"):
        services.open_vote(vote, opens_at=now, closes_at=now + 6 * DAY, actor=w["board"])
    community.set_rule(
        key="vote.min_open_days", value="3", citation="Bylaws §2.8", actor=w["board"]
    )
    vote = services.open_vote(vote, opens_at=now, closes_at=now + 4 * DAY, actor=w["board"])
    assert vote.status == VoteStatus.OPEN and vote.closes_at == now + 4 * DAY


def test_the_period_counts_from_when_voting_really_opens(w, clock):
    vote = services.create_vote(
        title="Fence?", kind=VoteKind.FORMAL, options=["Yes", "No"], actor=w["board"]
    )
    planned = timezone.now()
    clock(planned + 2 * DAY)  # opened two days late
    with pytest.raises(services.GovernanceError, match="at least 7 days"):
        services.open_vote(vote, opens_at=planned, closes_at=planned + 8 * DAY, actor=w["board"])


def test_the_vote_form_requires_both_times(client, w):
    client.force_login(w["board"])
    response = client.post(
        reverse("votes"), {"title": "Fence?", "options": "Yes\nNo", "opens_at": "", "closes_at": ""}
    )
    assert response.status_code == 200
    assert not Vote.objects.exists()
    html = response.content.decode()
    assert "Voting opens" in html and "Voting closes" in html


def test_opening_from_the_page_takes_the_window(client, w):
    vote = services.create_vote(
        title="Fence?", kind=VoteKind.FORMAL, options=["Yes", "No"], actor=w["board"]
    )
    client.force_login(w["board"])
    url = reverse("vote_action", args=[vote.pk, "open"])
    response = client.post(url, {"opens_at": "", "closes_at": ""})
    assert response.status_code == 200  # the bound form comes back
    vote.refresh_from_db()
    assert vote.status == VoteStatus.DRAFT
    start = timezone.localtime() + HOUR
    response = client.post(
        url,
        {
            "opens_at": start.strftime("%Y-%m-%dT%H:%M"),
            "closes_at": (start + 3 * DAY).strftime("%Y-%m-%dT%H:%M"),
        },
    )
    assert response.status_code == 200 and "at least 7 days" in response.content.decode()
    response = client.post(
        url,
        {
            "opens_at": start.strftime("%Y-%m-%dT%H:%M"),
            "closes_at": (start + 9 * DAY).strftime("%Y-%m-%dT%H:%M"),
        },
    )
    assert response.status_code == 302
    vote.refresh_from_db()
    assert vote.status == VoteStatus.OPEN and vote.opens_at > timezone.now()


def test_an_election_needs_a_window_to_open(w):
    e = elections.create_election(title="2027", seats=1, actor=w["board"])
    elections.nominate(e, person=w["board"].person, actor=w["board"])
    with pytest.raises(elections.ElectionError, match="opens and when it closes"):
        elections.open_voting(e, actor=w["board"])
    now = timezone.now()
    with pytest.raises(elections.ElectionError, match="at least 7 days"):
        elections.open_voting(e, opens_at=now, closes_at=now + 2 * DAY, actor=w["board"])
    e = elections.open_voting(e, opens_at=now, closes_at=now + 8 * DAY, actor=w["board"])
    assert e.voting_opens_at >= now and e.voting_closes_at == now + 8 * DAY


def test_no_ballot_before_a_later_opening_time(w, clock):
    start = timezone.now() + DAY
    vote = _vote(w, opens_at=start)
    owner = w["owners"][0]
    with pytest.raises(services.GovernanceError):
        _cast(vote, owner)
    clock(start + HOUR)
    _cast(vote, owner)
    assert vote.ballots.count() == 1


# --- Closing at the deadline ---------------------------------------------------------


def test_casting_stops_at_the_deadline_and_the_task_closes_the_vote(w, clock):
    vote = _vote(w, secret=True)
    a, b, c = w["owners"]
    _cast(vote, a, "Yes")
    _cast(vote, b, "No")
    assert mixing.pending_count(mixing.VOTE, vote.pk) == 2  # fewer than 2K: still pooled
    later = clock(vote.closes_at + datetime.timedelta(minutes=1))
    with pytest.raises(services.GovernanceError):
        _cast(vote, c)
    assert window.close_due(now=later) == 1
    vote.refresh_from_db()
    assert vote.status == VoteStatus.CLOSED and vote.closed_at_deadline
    assert mixing.pending_count(mixing.VOTE, vote.pk) == 0
    assert SecretChoice.objects.filter(vote=vote).count() == 2
    result = services.results(vote)
    assert result["total"] == 2 and result["pending"] == 0 and result["outcome"]
    event = AuditEvent.objects.filter(action="vote.closed").get()
    assert event.metadata == {"how": "deadline"} and event.actor_id is None
    assert window.close_due(now=later) == 0  # nothing left to close


def test_the_task_closes_elections_and_surveys_too(w, clock):
    e = _election(w)
    lot = _lot(w["owners"][0])
    cand = elections.ballot_candidates(e).get()
    elections.cast_election_ballot(e, lot_id=lot.pk, candidate_ids=[cand.pk], actor=w["owners"][0])
    survey = _survey(w, days=2)
    _answer(survey, w["owners"][1])
    later = clock(e.voting_closes_at + HOUR)
    assert window.close_due(now=later) == 2
    e.refresh_from_db()
    survey.refresh_from_db()
    assert e.state == "closed" and e.closed_at_deadline and e.voting_closed_at
    assert ElectionChoice.objects.filter(election=e).count() == 1
    assert survey.status == "closed" and survey.closed_at_deadline
    hows = set(
        AuditEvent.objects.filter(
            action__in=["election.voting_closed", "survey.closed"]
        ).values_list("metadata__how", flat=True)
    )
    assert hows == {"deadline"}


def test_the_close_task_is_periodic():
    from governance import tasks

    assert tasks.close_due_votes.name.endswith("close_due_votes")
    assert tasks.vote_deadline_reminders.name.endswith("vote_deadline_reminders")
    periodic = {t.task.name for t in tasks.app.periodic_registry.periodic_tasks.values()}
    assert tasks.close_due_votes.name in periodic
    assert tasks.vote_deadline_reminders.name in periodic


def test_no_early_close_unless_the_rule_allows_it_with_a_reason(w):
    vote = _vote(w)
    with pytest.raises(services.GovernanceError, match="until its closing time"):
        services.close_vote(vote, actor=w["board"])
    e = _election(w)
    with pytest.raises(elections.ElectionError, match="until its closing time"):
        elections.close_voting(e, actor=w["board"])
    allow_early_close(w["board"])
    with pytest.raises(services.GovernanceError, match="Say why"):
        services.close_vote(vote, actor=w["board"], reason="  ")
    vote = services.close_vote(vote, actor=w["board"], reason="Question withdrawn")
    assert vote.status == VoteStatus.CLOSED and vote.early_close_reason == "Question withdrawn"
    assert AuditEvent.objects.filter(action="vote.closed").get().metadata == {"how": "early"}


def test_the_page_offers_no_close_button_before_the_deadline(client, w):
    vote = _vote(w)
    client.force_login(w["board"])
    html = client.get(reverse("vote_detail", args=[vote.pk])).content.decode()
    assert reverse("vote_action", args=[vote.pk, "close"]) not in html
    response = client.post(reverse("vote_action", args=[vote.pk, "close"]))
    assert response.status_code == 302
    vote.refresh_from_db()
    assert vote.status == VoteStatus.OPEN


def test_old_informal_polls_can_still_be_closed_by_the_board(w):
    poll = legacy_poll(w["board"])
    services.open_vote(poll, actor=w["board"])
    assert services.close_vote(poll, actor=w["board"]).status == VoteStatus.CLOSED


# --- Extending, never shortening -----------------------------------------------------


def test_extend_once_with_a_reason_and_owners_are_told(w, django_capture_on_commit_callbacks):
    vote = _vote(w)
    old = vote.closes_at
    with pytest.raises(window.WindowError, match="Say why"):
        window.extend(vote, closes_at=old + DAY, reason="", actor=w["board"])
    with pytest.raises(window.WindowError, match="never shortened"):
        window.extend(vote, closes_at=old - HOUR, reason="Typo", actor=w["board"])
    with pytest.raises(PermissionDenied):
        window.extend(vote, closes_at=old + DAY, reason="x", actor=w["manager"])
    mail.outbox.clear()
    with django_capture_on_commit_callbacks(execute=True):
        vote = window.extend(
            vote, closes_at=old + 2 * DAY, reason="Snowstorm closed the clubhouse", actor=w["board"]
        )
    assert vote.closes_at == old + 2 * DAY and vote.extension_reason.startswith("Snowstorm")
    told = {m.to[0] for m in mail.outbox}
    assert {u.email for u in w["owners"]} <= told
    assert all("Snowstorm" in m.body and "now closes" in m.body for m in mail.outbox)
    event = AuditEvent.objects.filter(action="vote.deadline_extended").get()
    assert event.metadata == {"kind": "vote", "hours_added": 48}
    with pytest.raises(window.WindowError, match="already been extended"):
        window.extend(vote, closes_at=old + 5 * DAY, reason="Again", actor=w["board"])


def test_no_extension_once_the_deadline_has_passed(w, clock):
    vote = _vote(w)
    clock(vote.closes_at + HOUR)
    with pytest.raises(window.WindowError, match="has passed"):
        window.extend(vote, closes_at=vote.closes_at + DAY, reason="Late", actor=w["board"])


def test_the_database_refuses_to_shorten_or_move_an_open_window(w):
    vote = _vote(w)
    for change in (
        {"closes_at": vote.closes_at - HOUR},
        {"closes_at": None},
        {"opens_at": vote.opens_at + HOUR},
    ):
        with pytest.raises(IntegrityError), transaction.atomic():
            Vote.objects.filter(pk=vote.pk).update(**change)
    e = _election(w)
    with pytest.raises(IntegrityError), transaction.atomic():
        type(e).objects.filter(pk=e.pk).update(voting_closes_at=e.voting_closes_at - HOUR)


def test_an_election_deadline_can_be_extended_once(w):
    e = _election(w)
    old = e.voting_closes_at
    e = window.extend(e, closes_at=old + DAY, reason="Ballot printing delay", actor=w["board"])
    assert e.voting_closes_at == old + DAY
    with pytest.raises(window.WindowError, match="already been extended"):
        window.extend(e, closes_at=old + 2 * DAY, reason="Again", actor=w["board"])


# --- Reminders ------------------------------------------------------------------------


def test_reminder_48_hours_before_goes_to_homes_that_have_not_voted(
    w, django_capture_on_commit_callbacks
):
    vote = _vote(w)
    a, b, c = w["owners"]
    _cast(vote, a)
    mail.outbox.clear()
    assert window.send_reminders(now=vote.closes_at - 72 * HOUR) == 0  # too early
    with django_capture_on_commit_callbacks(execute=True):
        sent = window.send_reminders(now=vote.closes_at - 47 * HOUR)
    to = sorted(m.to[0] for m in mail.outbox)
    assert sent == len(to) and b.email in to and c.email in to
    assert a.email not in to
    for message in mail.outbox:
        assert "Vote by" in message.subject
        assert re.search(r"/ballot/[A-Za-z0-9_-]{20,100}/", message.body)  # personal link (D24)
    links = [re.search(r"/ballot/([A-Za-z0-9_-]+)/", m.body).group(1) for m in mail.outbox]
    assert len(set(links)) == len(links)
    mail.outbox.clear()
    with django_capture_on_commit_callbacks(execute=True):
        assert window.send_reminders(now=vote.closes_at - 46 * HOUR) == 0  # once only
    assert not mail.outbox
    event = AuditEvent.objects.filter(action="vote.reminders_sent").get()
    assert event.metadata["kind"] == "vote" and event.metadata["count"] == sent


def test_deadline_messages_skip_the_weekly_digest_and_no_link_lands_in_one(
    w, django_capture_on_commit_callbacks
):
    from comms.models import DigestItem
    from community.models import ContactProfile
    from governance.models import BallotLink

    vote = _vote(w)
    digest_owner = w["owners"][0]
    with system_access("test"):
        ContactProfile.objects.update_or_create(
            person=digest_owner.person, defaults={"weekly_digest": True}
        )
    mail.outbox.clear()
    with django_capture_on_commit_callbacks(execute=True):
        sent = window.send_reminders(now=vote.closes_at - 24 * HOUR)
    mine = [m for m in mail.outbox if m.to == [digest_owner.email]]
    assert len(mine) == 1 and re.search(r"/ballot/[A-Za-z0-9_-]{20,100}/", mine[0].body)
    # One personal link per email actually sent, and none waiting unsent.
    with system_access("test"):
        reminder_links = BallotLink.objects.filter(vote=vote).count()
    issued_at_open = len(window._voter_emails())
    assert reminder_links == issued_at_open + sent
    mail.outbox.clear()
    with django_capture_on_commit_callbacks(execute=True):
        window.extend(
            vote, closes_at=vote.closes_at + DAY, reason="Clubhouse closed", actor=w["board"]
        )
    assert [m for m in mail.outbox if m.to == [digest_owner.email]]
    with system_access("test"):
        links = list(DigestItem.objects.values_list("link", flat=True))
    assert not [link for link in links if "/ballot/" in link]
    assert not DigestItem.objects.filter(user=digest_owner).exists()


def test_election_reminders_skip_homes_that_voted(w, django_capture_on_commit_callbacks):
    e = _election(w)
    a = w["owners"][0]
    cand = elections.ballot_candidates(e).get()
    elections.cast_election_ballot(e, lot_id=_lot(a).pk, candidate_ids=[cand.pk], actor=a)
    mail.outbox.clear()
    with django_capture_on_commit_callbacks(execute=True):
        window.send_reminders(now=e.voting_closes_at - 24 * HOUR)
    to = {m.to[0] for m in mail.outbox}
    assert a.email not in to and w["owners"][1].email in to


# --- Turnout -----------------------------------------------------------------------------


def test_turnout_counts_homes_for_formal_votes(w):
    vote = _vote(w)
    t = window.turnout(vote)
    assert (t["voted"], t["eligible"], t["unit"]) == (0, 4, "homes")
    assert t["quorum_needed"] == 2 and t["quorum_met"] is False
    _cast(vote, w["owners"][0])
    _cast(vote, w["owners"][1])
    t = window.turnout(vote)
    assert (t["voted"], t["percent"], t["quorum_met"]) == (2, 50, True)
    assert window.turnout_line(t) == "2 of 4 homes have voted (50%)"


def test_turnout_counts_homes_for_elections_and_people_for_surveys_and_polls(w):
    e = _election(w)
    cand = elections.ballot_candidates(e).get()
    a = w["owners"][0]
    elections.cast_election_ballot(e, lot_id=_lot(a).pk, candidate_ids=[cand.pk], actor=a)
    t = window.turnout(e)
    assert (t["voted"], t["eligible"], t["unit"]) == (1, 4, "homes")
    survey = _survey(w)
    _answer(survey, a)
    t = window.turnout(survey)
    assert (t["voted"], t["eligible"], t["unit"]) == (1, 4, "people")
    assert t["quorum_needed"] is None
    assert window.turnout_line(t) == "1 of 4 people have responded (25%)"
    poll = legacy_poll(w["board"])
    services.open_vote(poll, actor=w["board"])
    services.cast_ballot(poll, option_id=poll.options.first().pk, actor=a)
    t = window.turnout(poll)
    assert (t["voted"], t["eligible"], t["unit"]) == (1, 4, "people")


def test_board_and_manager_see_turnout_and_who_has_voted_without_choices(client, w):
    vote = _vote(w)
    _cast(vote, w["owners"][0], "No")
    url = reverse("vote_detail", args=[vote.pk])
    for user in (w["board"], w["manager"]):
        client.force_login(user)
        html = client.get(url).content.decode()
        assert "1 of 4 homes have voted (25%)" in html
        assert "Quorum: 2 of 4 homes (50%)" in html and "<progress" in html
        assert "Who has voted" in html and "10 Oak Way" in html
        assert "20 Oak Way" not in html
        assert "bar-row" not in html  # no per-option tally
    client.force_login(w["manager"])
    assert 'name="option"' not in client.get(url).content.decode()  # the manager never votes


def test_who_has_voted_export_is_staff_only_and_audited(client, w):
    vote = _vote(w)
    _cast(vote, w["owners"][0], "No")
    url = reverse("vote_who_voted_csv", args=[vote.pk])
    client.force_login(w["owners"][1])
    assert client.get(url).status_code == 403
    html = client.get(reverse("vote_detail", args=[vote.pk])).content.decode()
    assert "Who has voted" not in html and url not in html
    client.force_login(w["board"])
    response = client.get(url)
    body = response.content.decode()
    assert response.status_code == 200 and "10 Oak Way" in body
    assert "No" not in body.splitlines()[1]  # an address and "Yes" (voted), never the choice
    assert body.splitlines()[0].startswith("Address")
    event = AuditEvent.objects.filter(action="vote.turnout_exported").get()
    assert event.metadata == {"kind": "vote", "rows": 1}
    e = _election(w)
    e_url = reverse("election_who_voted_csv", args=[e.pk])
    client.force_login(w["owners"][1])
    assert client.get(e_url).status_code == 403
    client.force_login(w["manager"])
    assert client.get(e_url).status_code == 200


def test_secret_ballots_show_counts_only(client, w):
    vote = _vote(w, secret=True)
    _cast(vote, w["owners"][0], "No")
    client.force_login(w["board"])
    html = client.get(reverse("vote_detail", args=[vote.pk])).content.decode()
    assert "1 of 4 homes have voted" in html
    assert "waiting to be mixed" in html
    assert "bar-row" not in html and "<h2>Results</h2>" not in html


def test_no_running_tallies_while_open_for_any_kind(client, w):
    vote = _vote(w)
    _cast(vote, w["owners"][0], "No")
    e = _election(w)
    cand = elections.ballot_candidates(e).get()
    a = w["owners"][1]
    elections.cast_election_ballot(e, lot_id=_lot(a).pk, candidate_ids=[cand.pk], actor=a)
    survey = _survey(w)
    _answer(survey, w["owners"][2])
    client.force_login(w["board"])
    html = client.get(reverse("vote_detail", args=[vote.pk])).content.decode()
    assert "bar-row" not in html and "<h2>Results</h2>" not in html
    html = client.get(reverse("election_detail", args=[e.pk])).content.decode()
    assert "Count (not yet certified)" not in html and "1 of 4 homes have voted" in html
    assert client.get(reverse("survey_results", args=[survey.pk])).status_code == 404
    response = client.get(reverse("survey_export", args=[survey.pk]))
    assert (
        response.status_code == 302
        and not AuditEvent.objects.filter(action="survey.exported").exists()
    )
    html = client.get(reverse("survey_detail", args=[survey.pk])).content.decode()
    assert "1 of 4 people have responded" in html
    assert reverse("survey_results", args=[survey.pk]) not in html
    # Once closed, results appear.
    allow_early_close(w["board"])
    services.close_vote(vote, actor=w["board"], reason="Test")
    html = client.get(reverse("vote_detail", args=[vote.pk])).content.decode()
    assert "<h2>Results</h2>" in html and "bar-row" in html
    surveys.close_survey(survey, actor=w["board"])
    assert client.get(reverse("survey_results", args=[survey.pk])).status_code == 200


def test_owners_see_their_own_home_and_the_deadline_and_turnout_only_by_rule(client, w):
    vote = _vote(w)
    a, b, _ = w["owners"]
    _cast(vote, a)
    client.force_login(a)
    url = reverse("vote_detail", args=[vote.pk])
    html = client.get(url).content.decode()
    assert "Vote by" in html and "10 Oak Way: you voted" in html
    assert "homes have voted" not in html and "Who has voted" not in html
    community.set_rule(
        key="vote.show_turnout_to_owners", value="true", citation="Board res. 1", actor=w["board"]
    )
    html = client.get(url).content.decode()
    assert "1 of 4 homes have voted (25%)" in html
    assert "Who has voted" not in html  # the list stays with the board and manager
    client.force_login(b)
    assert "1 of 4 homes have voted" in client.get(url).content.decode()


def test_owner_b_cannot_read_owner_as_ballot_on_any_page(client, w):
    vote = _vote(w)
    a, b, _ = w["owners"]
    _cast(vote, a, "No")
    client.force_login(b)
    html = client.get(reverse("vote_detail", args=[vote.pk])).content.decode()
    assert "10 Oak Way" not in html
    for name in ("vote_who_voted_csv",):
        assert client.get(reverse(name, args=[vote.pk])).status_code == 403


# --- Deadlines shown, and My items ---------------------------------------------------


def test_the_deadline_shows_in_the_list_the_link_email_and_my_items(
    client, w, django_capture_on_commit_callbacks
):
    mail.outbox.clear()
    with django_capture_on_commit_callbacks(execute=True):
        vote = _vote(w)
    deadline = window.when(vote.closes_at)
    assert any(f"Vote by {deadline}" in m.body for m in mail.outbox)
    owner = w["owners"][0]
    client.force_login(owner)
    assert f"vote by {deadline}" in client.get(reverse("voting")).content.decode()
    items = [
        i for i in my_items.items_for(owner) if i.url == reverse("vote_detail", args=[vote.pk])
    ]
    assert items and items[0].detail == f"Vote by {deadline}"


def test_board_my_items_when_a_vote_closes_within_48_hours(w, clock):
    vote = _vote(w)
    for owner in w["owners"][:2]:
        _cast(vote, owner)
    assert not [i for i in my_items.items_for(w["board"]) if "closes in" in i.title]
    clock(vote.closes_at - 30 * HOUR)
    rows = [i for i in my_items.items_for(w["board"]) if "closes in" in i.title]
    assert [i.title for i in rows] == ["Vote closes in 30 hours: 50% turnout, quorum met"]
    assert rows[0].url == reverse("vote_detail", args=[vote.pk])
    manager_rows = [i for i in my_items.items_for(w["manager"]) if "closes in" in i.title]
    assert manager_rows
    assert not [i for i in my_items.items_for(w["owners"][2]) if "closes in" in i.title]


# --- Audit ------------------------------------------------------------------------------


def test_new_audit_metadata_is_allowlisted():
    expect = {
        "vote.opened": {"kind", "open_days"},
        "vote.closed": {"how"},
        "vote.deadline_extended": {"kind", "hours_added"},
        "vote.reminders_sent": {"kind", "count"},
        "vote.turnout_exported": {"kind", "rows"},
        "election.voting_opened": {"open_days"},
        "election.voting_closed": {"how"},
        "survey.closed": {"how"},
    }
    for action, fields in expect.items():
        assert AUDIT_ACTIONS[action].metadata_fields == frozenset(fields), action


def test_survey_closes_after_its_last_day_not_before(w, clock):
    survey = _survey(w, days=1)
    end_of_last_day = window.window(survey)[1]
    assert window.close_due(now=end_of_last_day - HOUR) == 0
    assert window.close_due(now=end_of_last_day + HOUR) == 1
    assert Survey.objects.get(pk=survey.pk).status == "closed"


def test_system_close_is_still_possible_for_trusted_code(w):
    vote = _vote(w)
    vote = services.close_vote(vote, actor=SYSTEM)
    assert vote.status == VoteStatus.CLOSED and not vote.closed_at_deadline
