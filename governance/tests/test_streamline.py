"""Streamlining (owner-approved 2026-11-26): one Voting page, polls folded into
surveys, written consents on Resolutions and policies, meetings on the
calendar, and the meeting page as the hub."""

import datetime

import pytest
from django.core.exceptions import PermissionDenied
from django.urls import reverse
from django.utils import timezone

from comms.models import Event
from community.models import Role
from community.tests.factories import LotFactory, make_member
from core import features
from documents.models import Document
from documents.tests.helpers import pdf
from governance import elections, surveys
from governance import services as gov
from governance.models import ConsentRequest, Election, Meeting, Resolution, Vote, VoteKind
from governance.tests.helpers import legacy_poll
from governance.tests.test_surveys import _account, _contact_user

pytestmark = pytest.mark.django_db


@pytest.fixture
def w():
    lot_a, lot_b = LotFactory(), LotFactory()
    return {
        "board": make_member(board=True),
        "board2": make_member(board=True),
        "owner": make_member(lots=[lot_a]),
        "owner_b": make_member(lots=[lot_b]),
        "manager": make_member(roles=[Role.MANAGER]),
        "tenant": _contact_user(_account(lot_a), "tenant", "Tess Tenant"),
        "lot_a": lot_a,
    }


def soon(days=20):
    return timezone.now() + datetime.timedelta(days=days)


def _meeting(w, kind="annual", title="Annual owners' meeting", **kw):
    return gov.create_meeting(
        title=title, kind=kind, starts_at=soon(), location="Clubhouse", actor=w["board"], **kw
    )


def _formal(w, title="Repave the loop", **kw):
    return gov.create_vote(
        title=title, kind=VoteKind.FORMAL, options=["Yes", "No"], actor=w["board"], **kw
    )


def _page(client, url):
    response = client.get(url)
    assert response.status_code == 200, url
    return response.content.decode()


# --- One Voting page -----------------------------------------------------------------------


@pytest.mark.parametrize("name", ["votes", "elections", "surveys"])
def test_old_list_urls_redirect_to_voting_with_their_type(client, w, name):
    client.force_login(w["owner"])
    response = client.get(reverse(name))
    assert response.status_code == 302
    assert response["Location"] == f"{reverse('voting')}?type={name}"


@pytest.mark.parametrize("name", ["votes", "elections", "surveys", "voting"])
def test_voting_urls_need_sign_in(client, name):
    response = client.get(reverse(name))
    assert response.status_code == 302
    assert response["Location"].startswith(reverse("account_login"))


def test_tenant_redirect_from_surveys_lands_on_surveys_only(client, w):
    survey = surveys.create_survey(
        title="Pool hours",
        audience="residents",
        opens_on=timezone.localdate(),
        closes_on=timezone.localdate() + datetime.timedelta(days=5),
        actor=w["board"],
    )
    surveys.add_question(survey, kind="text", prompt="When?", actor=w["board"])
    surveys.open_survey(survey, actor=w["board"])
    _formal(w, title="Owners only vote")
    client.force_login(w["tenant"])
    response = client.get(reverse("surveys"), follow=True)
    html = response.content.decode()
    assert response.status_code == 200 and "Pool hours" in html
    assert "Owners only vote" not in html and "New survey" not in html
    # Tenants have no vote.view: the old votes list refuses them.
    assert client.get(reverse("votes")).status_code == 403


def test_one_list_with_open_and_past_filters(client, w):
    board = w["board"]
    draft = _formal(w, title="Draft formal vote")
    closed = _formal(w, title="Closed formal vote")
    from community import services as community

    community.set_rule(key="vote.quorum_percent", value="10", citation="Bylaws §2", actor=board)
    community.set_rule(key="vote.pass_percent", value="51", citation="Bylaws §2", actor=board)
    gov.open_vote(closed, actor=board)
    gov.close_vote(closed, actor=board)
    old_poll = legacy_poll(board, title="Old fountain poll", status="open")
    election = elections.create_election(title="2027 election", seats=2, actor=board)
    client.force_login(w["owner"])
    open_html = _page(client, reverse("voting"))
    assert "2027 election" in open_html
    assert "Draft formal vote" not in open_html  # drafts are the board's
    assert "Closed formal vote" not in open_html and "Old fountain poll" not in open_html
    past_html = _page(client, reverse("voting") + "?status=past")
    assert "Closed formal vote" in past_html and "Old fountain poll" in past_html
    assert "Informal poll (from before polls became surveys)" in past_html
    assert "2027 election" not in past_html
    # The old poll is still readable on its own page.
    assert client.get(reverse("vote_detail", args=[old_poll.pk])).status_code == 200
    # Type filter.
    only = _page(client, reverse("voting") + "?type=elections")
    assert "2027 election" in only
    assert _page(client, reverse("voting") + "?type=votes&status=past").count("Old fountain") == 1
    assert "2027 election" not in _page(client, reverse("voting") + "?type=votes")
    # The board sees its draft under Open.
    client.force_login(board)
    assert "Draft formal vote" in _page(client, reverse("voting"))
    assert draft.status == "draft" and election.pk


def test_type_filter_ignores_switched_off_types(client, w):
    elections.create_election(title="2027 election", seats=2, actor=w["board"])
    features.set_feature("elections", False, actor=w["board"])
    client.force_login(w["owner"])
    html = _page(client, reverse("voting") + "?type=elections")
    assert "2027 election" not in html
    assert 'href="?type=elections' not in html


def test_board_gets_new_buttons_owners_do_not(client, w):
    client.force_login(w["board"])
    html = _page(client, reverse("voting"))
    for label in ("New vote", "New election", "New survey"):
        assert label in html
    assert 'open id="new-vote"' in _page(client, reverse("voting") + "?new=vote")
    for user in (w["owner"], w["manager"]):
        client.force_login(user)
        html = _page(client, reverse("voting"))
        assert "New vote" not in html and "New election" not in html and "New survey" not in html


def test_vote_form_offers_formal_votes_only(client, w):
    client.force_login(w["board"])
    html = _page(client, reverse("voting"))
    assert "informal" not in html.lower().split('id="new-vote"')[1].split("</details>")[0]
    response = client.post(
        reverse("votes"),
        {"title": "Adopt pet rules", "options": "Yes\nNo", "kind": "informal"},
    )
    vote = Vote.objects.get(title="Adopt pet rules")
    assert response.status_code == 302 and vote.kind == VoteKind.FORMAL


def test_service_refuses_new_informal_polls(w):
    with pytest.raises(gov.GovernanceError, match="surveys"):
        gov.create_vote(title="Poll", kind="informal", options=["A", "B"], actor=w["board"])
    assert not Vote.objects.exists()


def test_refused_vote_form_comes_back_with_what_was_typed(client, w):
    client.force_login(w["board"])
    response = client.post(reverse("votes"), {"title": "Keep this title", "options": "Only one"})
    html = response.content.decode()
    assert response.status_code == 200
    assert "Keep this title" in html and 'open id="new-vote"' in html
    assert "Give at least two different options" in html
    assert not Vote.objects.exists()


def test_only_the_board_creates_votes_and_elections(client, w):
    for user in (w["owner"], w["manager"]):
        client.force_login(user)
        assert client.post(reverse("votes"), {"title": "X", "options": "A\nB"}).status_code == 403
        assert client.post(reverse("elections"), {"title": "X", "seats": 1}).status_code == 403
        assert client.post(reverse("surveys"), {"title": "X"}).status_code == 403
    assert not Vote.objects.exists() and not Election.objects.exists()


def test_board_creates_an_election_from_voting(client, w):
    meeting = _meeting(w)
    client.force_login(w["board"])
    response = client.post(
        reverse("elections"),
        {"title": "2027 election", "seats": 2, "term_years": 2, "meeting": meeting.pk},
    )
    election = Election.objects.get(title="2027 election")
    assert response.status_code == 302 and election.meeting_id == meeting.pk
    bad = client.post(reverse("elections"), {"title": "Too many", "seats": 99, "term_years": 2})
    assert bad.status_code == 200 and 'open id="new-election"' in bad.content.decode()
    assert b'role="alert"' in bad.content and b"Too many" in bad.content


def test_vote_can_belong_to_a_coming_owners_meeting_only(w):
    annual = _meeting(w)
    vote = _formal(w, meeting=annual)
    assert vote.meeting_id == annual.pk
    board_meeting = _meeting(w, kind="board", title="Board meeting")
    with pytest.raises(gov.GovernanceError, match="owners' meeting"):
        _formal(w, title="Wrong meeting", meeting=board_meeting)


def test_give_a_proxy_on_voting_for_coming_owner_meetings(client, w):
    annual = _meeting(w)
    _meeting(w, kind="board", title="Board meeting")
    client.force_login(w["owner"])
    html = _page(client, reverse("voting"))
    assert f"{reverse('meeting_detail', args=[annual.pk])}#proxy" in html
    assert "Board meeting" not in html
    elections.grant_proxy(
        annual,
        lot=w["lot_a"],
        holder=elections._person(w["board"]),
        actor=w["owner"],
    )
    assert "You gave a proxy" in _page(client, reverse("voting"))
    for user in (w["tenant"], w["manager"]):
        client.force_login(user)
        response = client.get(reverse("voting"))
        assert b"#proxy" not in response.content


# --- Written consent on Resolutions and policies ---------------------------------------------


def test_consents_list_redirects_to_resolutions(client, w):
    client.force_login(w["board"])
    response = client.get(reverse("consents"))
    assert response.status_code == 302
    assert response["Location"] == f"{reverse('resolutions')}#consents"
    client.force_login(w["owner"])
    assert client.get(reverse("consents")).status_code == 403


def test_board_adopts_by_written_consent_from_resolutions(client, w):
    client.force_login(w["board"])
    html = _page(client, reverse("resolutions"))
    assert "Adopt by written consent" in html and "Waiting for signatures" in html
    response = client.post(
        reverse("consents"),
        {"title": "Approve snow contract", "text": "Resolved: sign it.", "citation": "Bylaws §4"},
    )
    consent = ConsentRequest.objects.get(title="Approve snow contract")
    assert response.status_code == 302
    assert response["Location"] == reverse("consent_detail", args=[consent.pk])
    html = _page(client, reverse("resolutions"))
    assert reverse("consent_detail", args=[consent.pk]) in html
    # Consent signatures stay their own records; nothing is a resolution yet.
    assert not Resolution.objects.exists()
    bad = client.post(reverse("consents"), {"title": "Keep me", "text": ""})
    assert bad.status_code == 200 and b"Keep me" in bad.content
    assert b'open id="adopt-consent"' in bad.content


def test_owners_never_see_consents_on_resolutions(client, w):
    elections.propose_consent(title="Board only consent", text="x", actor=w["board"])
    client.force_login(w["owner"])
    html = _page(client, reverse("resolutions"))
    assert "Board only consent" not in html
    assert "Waiting for signatures" not in html and "Adopt by written consent" not in html


def test_consents_switched_off_hides_the_section(client, w):
    features.set_feature("consents", False, actor=w["board"])
    client.force_login(w["board"])
    html = _page(client, reverse("resolutions"))
    assert "Waiting for signatures" not in html and "Adopt by written consent" not in html
    assert client.get(reverse("consents")).status_code == 404


# --- Meetings and the calendar --------------------------------------------------------------


@pytest.mark.parametrize("kind", ["annual", "special", "board", "committee"])
def test_meetings_put_themselves_on_the_calendar(w, kind):
    meeting = _meeting(w, kind=kind, title=f"{kind} meeting")
    meeting.refresh_from_db()
    event = Event.objects.get(pk=meeting.calendar_event_id)
    assert event.title == f"{kind} meeting" and event.starts_at == meeting.starts_at
    assert event.audience == "members" and event.location == "Clubhouse"


def test_executive_sessions_never_reach_the_calendar(client, w):
    meeting = _meeting(w, kind="executive", title="Executive session: litigation")
    meeting.refresh_from_db()
    assert meeting.calendar_event_id is None
    assert not Event.objects.filter(title__contains="Executive").exists()
    for user in (w["owner"], w["tenant"], w["board"]):
        client.force_login(user)
        assert b"Executive session" not in client.get(reverse("calendar")).content
    gov.set_meeting_status(meeting, "cancelled", actor=w["board"])
    assert not Event.objects.exists()


def test_cancelling_a_meeting_cancels_its_event(client, w):
    meeting = _meeting(w)
    client.force_login(w["manager"])
    response = client.post(reverse("meeting_action", args=[meeting.pk, "cancelled"]))
    assert response.status_code == 302
    meeting.refresh_from_db()
    assert meeting.status == "cancelled"
    assert Event.objects.get(pk=meeting.calendar_event_id).cancelled
    # Held meetings stay on the calendar.
    held = _meeting(w, kind="board", title="Board meeting")
    gov.set_meeting_status(held, "held", actor=w["board"])
    held.refresh_from_db()
    assert not Event.objects.get(pk=held.calendar_event_id).cancelled


def test_calendar_sends_meeting_events_to_the_meeting_page(client, w):
    meeting = _meeting(w)
    meeting.refresh_from_db()
    client.force_login(w["board"])
    html = _page(client, reverse("calendar"))
    assert reverse("event_cancel", args=[meeting.calendar_event_id]) not in html
    client.post(reverse("event_cancel", args=[meeting.calendar_event_id]))
    assert not Event.objects.get(pk=meeting.calendar_event_id).cancelled
    client.force_login(w["owner"])
    html = _page(client, reverse("event_detail", args=[meeting.calendar_event_id]))
    assert reverse("meeting_detail", args=[meeting.pk]) in html
    html = _page(client, reverse("meeting_detail", args=[meeting.pk]))
    assert reverse("event_detail", args=[meeting.calendar_event_id]) in html


def test_only_meeting_managers_create_meetings_and_their_events(w):
    with pytest.raises(PermissionDenied):
        _meeting({"board": w["owner"]})
    assert not Event.objects.exists() and not Meeting.objects.exists()


# --- The meeting page as the hub -------------------------------------------------------------


def test_meeting_page_lists_its_votes_elections_and_resolutions(client, w):
    meeting = _meeting(w)
    _formal(w, title="Draft pet rules vote", meeting=meeting)
    elections.create_election(title="2027 election", seats=2, meeting=meeting, actor=w["board"])
    gov.record_resolution(
        kind="resolution",
        title="Adopt the budget",
        text="Resolved.",
        adopted_on=timezone.localdate(),
        meeting=meeting,
        actor=w["board"],
    )
    client.force_login(w["board"])
    html = _page(client, reverse("meeting_detail", args=[meeting.pk]))
    for text in ("Draft pet rules vote", "2027 election", "Adopt the budget"):
        assert text in html
    assert "Record a resolution adopted at this meeting" in html
    client.force_login(w["owner"])
    html = _page(client, reverse("meeting_detail", args=[meeting.pk]))
    assert "2027 election" in html and "Adopt the budget" in html
    assert "Draft pet rules vote" not in html  # drafts are the board's
    assert "Record a resolution adopted at this meeting" not in html


def test_record_resolution_prefills_the_meeting(client, w):
    meeting = _meeting(w)
    client.force_login(w["board"])
    response = client.get(reverse("resolutions") + f"?meeting={meeting.pk}")
    form = response.context["form"]
    assert form.initial["meeting"] == meeting.pk
    assert form.initial["adopted_on"] == timezone.localdate(meeting.starts_at)
    assert b'open id="record"' in response.content
    response = client.post(
        reverse("resolutions"),
        {
            "kind": "resolution",
            "title": "Adopt the budget",
            "text": "Resolved.",
            "adopted_on": timezone.localdate().isoformat(),
            "meeting": meeting.pk,
            "votes_for": 5,
            "votes_against": 0,
            "abstentions": 0,
        },
    )
    assert response["Location"] == reverse("meeting_detail", args=[meeting.pk])
    assert Resolution.objects.get(title="Adopt the budget").meeting_id == meeting.pk
    # Owners get no form, and an unknown meeting id prefills nothing.
    client.force_login(w["owner"])
    response = client.get(reverse("resolutions") + f"?meeting={meeting.pk}")
    assert b"Record a resolution or policy" not in response.content


def test_executive_meeting_is_not_offered_or_named_to_owners(client, w):
    secret = _meeting(w, kind="executive", title="Executive session: litigation")
    gov.record_resolution(
        kind="resolution",
        title="Settle the claim",
        text="Resolved.",
        adopted_on=timezone.localdate(),
        meeting=secret,
        actor=w["board"],
    )
    client.force_login(w["owner"])
    html = _page(client, reverse("resolutions"))
    assert "Settle the claim" in html and "Executive session" not in html
    response = client.get(reverse("resolutions") + f"?meeting={secret.pk}")
    assert b"Executive session" not in response.content


def test_board_uploads_minutes_from_the_meeting_page(client, w):
    meeting = _meeting(w, kind="board", title="Board meeting")
    gov.set_meeting_status(meeting, "held", actor=w["board"])
    client.force_login(w["manager"])
    url = reverse("meeting_action", args=[meeting.pk, "minutes"])
    response = client.post(url, {"how": "upload", "file": pdf()})
    assert response.status_code == 302
    meeting.refresh_from_db()
    doc = Document.objects.unscoped("t").get(pk=meeting.minutes_document_id)
    assert doc.category == "minutes" and doc.visibility == "owners"
    assert doc.title.startswith("Minutes: Board meeting")
    # A second upload is refused on the form; nothing new is stored.
    again = client.post(url, {"how": "upload", "file": pdf()})
    assert again.status_code == 200 and b"already attached" in again.content
    assert Document.objects.unscoped("t").count() == 1


def test_executive_session_minutes_are_board_only(client, w):
    meeting = _meeting(w, kind="executive", title="Executive session")
    client.force_login(w["board"])
    client.post(
        reverse("meeting_action", args=[meeting.pk, "minutes"]),
        {"how": "upload", "file": pdf(), "title": "Exec minutes"},
    )
    doc = Document.objects.unscoped("t").get(title="Exec minutes")
    assert doc.visibility == "board"
    client.force_login(w["owner"])
    assert b"Exec minutes" not in client.get(reverse("documents")).content


def test_minutes_upload_without_a_file_comes_back_with_errors(client, w):
    meeting = _meeting(w)
    client.force_login(w["board"])
    response = client.post(
        reverse("meeting_action", args=[meeting.pk, "minutes"]),
        {"how": "upload", "title": "Typed title"},
    )
    assert response.status_code == 200
    assert b'role="alert"' in response.content and b"Typed title" in response.content
    assert not Document.objects.unscoped("t").exists()


def test_owners_cannot_upload_minutes_or_send_notice(client, w):
    meeting = _meeting(w)
    client.force_login(w["owner"])
    for action, data in (("minutes", {"how": "upload", "file": pdf()}), ("notice", {})):
        response = client.post(reverse("meeting_action", args=[meeting.pk, action]), data)
        assert response.status_code == 403
    assert not Document.objects.unscoped("t").exists()
    meeting.refresh_from_db()
    assert meeting.notice_sent_at is None


def test_notice_marks_the_annual_meeting_deadline_done(client, w):
    from operations import services as operations
    from operations.models import ComplianceItem

    operations.add_starter_items(actor=w["board"])
    item = ComplianceItem.objects.get(title__startswith="Annual owners' meeting")
    meeting = _meeting(w)
    client.force_login(w["board"])
    response = client.get(reverse("meeting_detail", args=[meeting.pk]))
    assert response.context["deadline_default"] == item.pk
    assert f'value="{item.pk}" selected'.encode() in response.content
    client.post(reverse("meeting_action", args=[meeting.pk, "notice"]), {"deadline": item.pk})
    item.refresh_from_db()
    assert item.last_done_on == timezone.localdate()
    meeting.refresh_from_db()
    assert meeting.notice_sent_at is not None


def test_notice_without_a_deadline_leaves_board_tasks_alone(client, w):
    from operations import services as operations
    from operations.models import ComplianceItem

    operations.add_starter_items(actor=w["board"])
    board_meeting = _meeting(w, kind="board", title="Board meeting")
    client.force_login(w["board"])
    response = client.get(reverse("meeting_detail", args=[board_meeting.pk]))
    assert response.context["deadline_default"] is None
    client.post(reverse("meeting_action", args=[board_meeting.pk, "notice"]))
    assert not ComplianceItem.objects.filter(last_done_on__isnull=False).exists()
    executive = _meeting(w, kind="executive", title="Executive session")
    assert gov.notice_deadlines(executive) == ([], None)
    with pytest.raises(gov.GovernanceError):
        gov.complete_notice_deadline(executive, 1, actor=w["board"])
