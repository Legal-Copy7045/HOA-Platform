"""Navigation, dashboard, accessibility and wording (UX review 2026-11-25)."""

import datetime
import re
from pathlib import Path
from unittest import mock

import pytest
from django.apps import apps
from django.conf import settings
from django.contrib.messages import constants
from django.urls import reverse
from django.utils import timezone

from community.models import Person, Role
from community.tests.factories import LotFactory, make_member
from compliance import services as compliance
from core import features, my_items
from core.dates import short_date
from core.scoping import system_access
from core.templatetags.portal import _TONES

pytestmark = pytest.mark.django_db

ROOT = Path(settings.BASE_DIR)


def nav(response):
    return response.context["nav"]["sections"]


def labels(response):
    return [i["label"] for s in nav(response) for i in s["items"]]


def section(response, title):
    return next(s for s in nav(response) if s["title"] == title)


def _person(user):
    with system_access("test"):
        return Person.objects.get(user=user)


@pytest.fixture
def w():
    lot_a, lot_b = LotFactory(), LotFactory()
    return {
        "board": make_member(board=True, lots=[LotFactory()]),
        "board2": make_member(board=True),
        "owner": make_member(lots=[lot_a]),
        "owner_b": make_member(lots=[lot_b]),
        "lot_a": lot_a,
        "lot_b": lot_b,
    }


# --- Sidebar: groups, hubs, owner menu ------------------------------------------------------


def test_owner_menu_merges_voting_and_community_info(client, w):
    client.force_login(w["owner"])
    found = labels(client.get(reverse("home")))
    for label in ("Voting", "Community info", "My records requests"):
        assert label in found
    for label in (
        "Votes and polls",
        "Elections",
        "FAQ",
        "Directory",
        "Committees",
        "Settings",
        "Records requests",
    ):
        assert label not in found


def test_rarely_used_setup_pages_move_to_settings(client, w):
    features.set_feature("reservations", True, actor=w["board"])
    client.force_login(w["board"])
    found = labels(client.get(reverse("home")))
    assert "Settings" in found
    for label in (
        "ARC request types",
        "Home neighbors",
        "Letters and certificates",
        "Branding",
        "Features",
        "Community settings",
        "Contact retention",
        "Common area booking rules",
    ):
        assert label not in found
    page = client.get(reverse("settings_hub")).content.decode()
    for name in (
        "community_settings",
        "arc_project_types",
        "arc_neighbors",
        "letters_settings",
        "reservation_settings",
    ):
        assert f'href="{reverse(name)}"' in page
    # WT-004/WT-038: Community setup is the front; Features is its step and
    # Contact retention opens from the Privacy step, so neither is a tile.
    assert f'href="{reverse("setup_step", args=["features"])}"' in page
    assert f'href="{reverse("setup_step", args=["privacy"])}"' in page
    for name in ("feature_settings", "retention", "setup_home"):
        assert f'href="{reverse(name)}"' not in page, name
    # Moved out (streamlining 2026-11-26): the compliance calendar is a tab of
    # Board tasks; the access review is a panel on People.
    assert f'href="{reverse("compliance")}"' not in page
    assert f'href="{reverse("access_review")}"' not in page


def test_settings_hub_is_refused_to_owners_and_trimmed_by_role(client, w):
    client.force_login(w["owner"])
    assert client.get(reverse("settings_hub")).status_code == 403
    manager = make_member(roles=[Role.MANAGER])
    client.force_login(manager)
    page = client.get(reverse("settings_hub")).content.decode()
    assert reverse("community_settings") in page
    assert reverse("feature_settings") not in page and reverse("arc_neighbors") not in page
    sysadmin = make_member(roles=[Role.SYSADMIN])
    client.force_login(sysadmin)
    page = client.get(reverse("settings_hub")).content.decode()
    assert reverse("branding_settings") in page and reverse("community_settings") not in page


def test_hub_entry_is_current_on_its_pages_and_its_group_opens(client, w):
    client.force_login(w["board"])
    response = client.get(reverse("community_settings"))
    admin = section(response, "Admin")
    assert admin["open"] and [i["label"] for i in admin["items"] if i["current"]] == ["Settings"]
    response = client.get(reverse("home"))
    admin = section(response, "Admin")
    # Collapsed unless the page is inside or (staff, WT-036) it is the work
    # section with the most waiting.
    work = [s for s in nav(response) if s["work"]]
    assert [s for s in work if s["open"]] == [max(work, key=lambda s: s["count"])]
    assert not admin["open"] or admin["count"]
    # Staff start with My home and Community closed (WT-040); owners keep them open.
    assert not section(response, "My home")["open"]
    assert not section(response, "Community")["open"]
    client.force_login(w["owner"])
    owner = client.get(reverse("home"))
    assert section(owner, "My home")["open"] and section(owner, "Community")["open"]
    client.force_login(w["board"])
    html = response.content.decode()
    assert '<details class="nav-section nav-group" data-nav-group="association"' in html
    assert 'data-nav-group="my-home"' in html


def test_voting_entry_current_on_votes_and_elections(client, w):
    vote, _ = _poll(w["board"])
    client.force_login(w["owner"])
    for url in (
        reverse("votes"),
        reverse("elections"),
        reverse("voting"),
        reverse("vote_detail", args=[vote.pk]),
    ):
        response = client.get(url, follow=True)  # old lists redirect to Voting
        current = [i["label"] for s in nav(response) for i in s["items"] if i["current"]]
        assert current == ["Voting"], url


def test_feature_switches_still_gate_hubs_and_voting(client, w):
    client.force_login(w["owner"])
    features.set_feature("votes", False, actor=w["board"])
    page = client.get(reverse("voting")).content.decode()
    assert 'href="?type=elections#results"' in page and 'href="?type=votes' not in page
    features.set_feature("elections", False, actor=w["board"])
    page = client.get(reverse("voting")).content.decode()
    assert "surveys (to hear your views" in page and 'href="?type=elections' not in page
    features.set_feature("surveys", False, actor=w["board"])
    assert client.get(reverse("voting")).status_code == 404
    assert "Voting" not in labels(client.get(reverse("home")))
    features.set_feature("faq", False, actor=w["board"])
    features.set_feature("directory", False, actor=w["board"])
    features.set_feature("reserves", False, actor=w["board"])
    # Board and committees has no switch (every resident sees who serves), so
    # Community info keeps one page and the menu links straight to it.
    menu = labels(client.get(reverse("home")))
    assert "Community info" not in menu and "Board and committees" in menu
    assert (
        "Owner directory" not in client.get(reverse("community_info"), follow=True).content.decode()
    )


def test_permissions_unchanged_owner_still_refused_board_pages(client, w):
    client.force_login(w["owner"])
    for name in ("community_settings", "arc_project_types", "feature_settings", "compliance"):
        assert client.get(reverse(name)).status_code in (403, 404), name


def _h1(client, url) -> str | None:
    page = client.get(url, follow=True).content.decode()
    h1 = re.search(r"<h1[^>]*>(.*?)</h1>", page, re.S)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", h1.group(1))).strip() if h1 else None


# Pages other streamlining builders rename in parallel (2026-11-26); their
# <h1> is aligned when the branches merge. Empty this once merged.
H1_PENDING: set[str] = set()
# A tenant's Voting goes straight to Surveys (its one page), which the
# governance builder turns into a redirect to the Voting list.
H1_PENDING_PAIRS: set[tuple[str, str | None]] = set()


def test_nav_labels_match_page_headings(client, w):
    """Each menu label, and each tile on a hub page, is the page's <h1>
    (docs/glossary.md), for every role."""
    from core import navigation
    from core.tests.test_nav_streamline import role_users

    wrong = []
    for role, user in role_users(w["board"]).items():
        client.force_login(user)
        response = client.get(reverse("home"))
        for s in nav(response):
            for item in s["items"]:
                if item["label"] == "Dashboard":  # the greeting
                    continue
                pairs = [(item["label"], item["url"])]
                for hub in ("settings_hub", "community_info", "finance_more"):
                    if item["url"] == reverse(hub):
                        links = navigation.hub_links(response.wsgi_request, hub)
                        # The board's Settings front shows the setup steps
                        # instead of these tiles (WT-004).
                        pairs += [
                            (link["label"], link["url"])
                            for link in links
                            if link["name"] not in navigation.COVERED_BY_SETUP
                        ]
                for label, url in pairs:
                    h1 = _h1(client, url)
                    if (
                        h1 != label
                        and label not in H1_PENDING
                        and (label, h1) not in H1_PENDING_PAIRS
                    ):
                        wrong.append((role, label, h1))
    assert not wrong, wrong


def test_badge_text_is_visually_hidden_not_aria_label(client, w):
    v = compliance.report(lot=w["lot_a"], category="trash", description="Bins", actor=w["board"])
    compliance.VIOLATION.apply(v, "send_notice", actor=w["board"])
    client.force_login(w["owner"])
    html = client.get(reverse("home")).content.decode()
    assert '<span class="sr-only"> waiting on you</span>' in html
    assert 'aria-label="1 waiting on you"' not in html


def test_nav_items_built_once_per_request(client, w):
    client.force_login(w["board"])
    with mock.patch("core.my_items.items_for", wraps=my_items.items_for) as spy:
        client.get(reverse("home"))
        assert spy.call_count == 1
        spy.reset_mock()
        client.get(reverse("my_items"))
        assert spy.call_count == 1


# --- Dashboard ------------------------------------------------------------------------------


def test_dashboard_todos_come_before_tiles_and_use_mine(client, w):
    client.force_login(w["board"])
    html = client.get(reverse("home")).content.decode()
    # A board member with a home: two headed parts, to-dos before tiles (D64).
    assert html.index('id="part-') < html.index('class="stat')
    assert f'href="{reverse("arc_list")}?mine=1"' in html
    assert f'href="{reverse("violations")}?mine=1"' in html
    assert "My home improvement requests (ARC)" in html


def test_dashboard_todo_row_is_one_link(client, w):
    v = compliance.report(lot=w["lot_a"], category="trash", description="Bins", actor=w["board"])
    compliance.VIOLATION.apply(v, "send_notice", actor=w["board"])
    client.force_login(w["owner"])
    html = client.get(reverse("home")).content.decode()
    row = re.search(r'<li class="todo">(.*?)</li>', html, re.S).group(1)
    assert row.count("<a ") == 1 and 'aria-label="Open"' not in html
    assert "Notice: Trash and recycling" in row and "Cure by" not in html


def test_balance_todo_shows_amount_and_due_date(client, w):
    from ledger import services as ledger

    ledger.post_charge(
        lot=w["lot_a"],
        kind="other_charge",
        amount_cents=12345,
        description="Key fob",
        posted_on=timezone.localdate() - datetime.timedelta(days=40),
        actor=w["board"],
    )
    client.force_login(w["owner"])
    html = client.get(reverse("home")).content.decode()
    assert f"$123.45 due for {w['lot_a'].label}" in html
    assert "due since" in html
    item = next(i for i in my_items.items_for(w["owner"]) if i.nav == "my_account")
    assert item.due == timezone.localdate() - datetime.timedelta(days=40)


def test_dashboard_and_board_overview_share_one_list(client, w):
    from accounts import mfa_reset
    from accounts.models import VerificationMethod

    target = make_member(lots=[LotFactory()])
    with mock.patch("community.tasks.notify_board.defer"):
        mfa_reset.start_reset(
            subject=target, verification_method=VerificationMethod.values[0], actor=w["board"]
        )
    titles = [i.title for i in my_items.items_for(w["board2"])]
    assert "Sign-in reset" in titles
    client.force_login(w["board2"])
    assert "Sign-in reset" in client.get(reverse("home")).content.decode()
    counts = client.get(reverse("my_items")).context["counts"]
    assert any(c["area"] == "Second approvals" for c in counts)


def test_my_items_uses_area_icons(client, w):
    client.force_login(w["board"])
    v = compliance.report(lot=w["lot_b"], category="trash", description="Bins", actor=w["board"])
    compliance.VIOLATION.apply(v, "send_notice", actor=w["board"])
    compliance.VIOLATION.apply(v, "respond", actor=w["owner_b"], note="Done")
    html = client.get(reverse("my_items")).content.decode()
    assert "#flag" in html
    assert my_items.Item("Second approvals", "t", "d", "/").icon == "check-circle"


def test_proxy_offered_on_dashboard_when_a_meeting_has_a_vote(client, w):
    from governance import elections
    from governance import services as gov

    meeting = gov.create_meeting(
        title="Annual meeting",
        kind="annual",
        location="Clubhouse",
        starts_at=timezone.now() + datetime.timedelta(days=10),
        actor=w["board"],
    )
    client.force_login(w["owner"])
    assert "Give a proxy" not in client.get(reverse("home")).content.decode()
    elections.create_election(title="2027 election", seats=1, meeting=meeting, actor=w["board"])
    html = client.get(reverse("home")).content.decode()
    assert f"{reverse('meeting_detail', args=[meeting.pk])}#proxy" in html


# --- Voting, elections, proxies -------------------------------------------------------------


def _poll(board, **kw):
    from governance import services as gov
    from governance.tests.helpers import legacy_poll, window_kw

    vote = legacy_poll(board, **kw)
    return gov.open_vote(vote, actor=board, **window_kw()), list(vote.options.all())


def test_after_voting_the_page_says_when(client, w):
    from governance import services as gov

    vote, (yes, _) = _poll(w["board"])
    gov.cast_ballot(vote, option_id=yes.pk, actor=w["owner"])
    client.force_login(w["owner"])
    html = client.get(reverse("vote_detail", args=[vote.pk])).content.decode()
    assert f"You voted on {timezone.localdate():%b} {timezone.localdate().day}" in html
    assert "Cast ballot</button>" not in html


def test_secret_ballot_only_says_recorded(client, w):
    from governance import services as gov

    vote, (yes, _) = _poll(w["board"], secret=True)
    gov.cast_ballot(vote, option_id=yes.pk, actor=w["owner"])
    client.force_login(w["owner"])
    html = client.get(reverse("vote_detail", args=[vote.pk])).content.decode()
    assert "Your ballot was recorded" in html and "You voted on" not in html
    # An informal poll (from before polls became surveys) is listed under Past.
    past = client.get(reverse("voting") + "?status=past").content.decode()
    assert "Your ballot was recorded" in past


@pytest.fixture
def election(w):
    from governance import elections
    from governance import services as gov

    meeting = gov.create_meeting(
        title="Annual meeting",
        kind="annual",
        location="Clubhouse",
        starts_at=timezone.now() + datetime.timedelta(days=20),
        actor=w["board"],
    )
    e = elections.create_election(title="2027 election", seats=2, meeting=meeting, actor=w["board"])
    for user in (w["owner_b"], w["board"]):
        elections.nominate(e, person=_person(user), statement="x" * 300, actor=user)
    from governance.tests.helpers import window_kw

    # The board member stands as a candidate: another opens voting (D87).
    return elections.open_voting(e, actor=w["board2"], **window_kw())


def test_election_ballot_cards_limit_and_confirmation(client, w, election):
    client.force_login(w["owner"])
    html = client.get(reverse("election_detail", args=[election.pk])).content.decode()
    assert 'data-max-choices="2"' in html and "data-confirm-ballot" in html
    assert 'class="choice"' in html and "<details><summary>Read " in html
    assert "Casting asks for your password or passkey again" in html


def test_election_ballot_recorded_message(client, w, election):
    from governance import elections

    cand = elections.ballot_candidates(election).first()
    elections.cast_election_ballot(
        election, lot_id=w["lot_a"].pk, candidate_ids=[cand.pk], actor=w["owner"]
    )
    client.force_login(w["owner"])
    html = client.get(reverse("election_detail", args=[election.pk])).content.decode()
    assert f"{w['lot_a'].label}: your ballot was recorded." in html
    assert "data-confirm-ballot" not in html


def test_owner_of_two_lots_is_told_to_cast_per_lot(client, w, election):
    from governance import elections

    two = make_member(lots=[LotFactory(), LotFactory()])
    assert len(elections.lots_actor_may_vote_for(election, two)) == 2
    client.force_login(two)
    html = client.get(reverse("election_detail", args=[election.pk])).content.decode()
    assert "You can cast 2 ballots here, one for each home" in html
    item = next(i for i in my_items.items_for(two) if i.title.startswith("Election"))
    assert "one ballot each" in item.detail


def test_elections_tab_and_proxy_links(client, w, election):
    client.force_login(w["owner"])
    response = client.get(reverse("elections"))
    assert response["Location"] == f"{reverse('voting')}?type=elections"
    html = client.get(response["Location"]).content.decode()
    assert election.title in html
    html = client.get(reverse("elections"), follow=True).content.decode()
    # No second tab bar under the sidebar (streamlining 2026-11-26).
    assert 'aria-label="Meetings and voting"' not in html
    current = re.findall(r'<a href="([^"]+)" aria-current="page">', html)
    assert current == [reverse("voting")]
    html = client.get(reverse("election_detail", args=[election.pk])).content.decode()
    assert f"{reverse('meeting_detail', args=[election.meeting_id])}#proxy" in html


def test_proxy_holder_is_chosen_by_address(client, w, election):
    from governance.models import Proxy

    client.force_login(w["owner"])
    html = client.get(reverse("meeting_detail", args=[election.meeting_id])).content.decode()
    assert 'name="holder_home"' in html and 'name="holder_lot"' not in html
    assert w["lot_b"].street_address in html
    proxy_section = html[html.index('id="proxy"') :]
    assert "data-confirm=" in proxy_section[:2000]
    assert not re.search(r"<p[^>]*>[^<]*(<(?!/p)[^>]*>[^<]*)*<form", proxy_section)
    client.post(
        reverse("proxy_grant", args=[election.meeting_id]),
        {"lot": w["lot_a"].pk, "holder_home": w["lot_b"].pk},
    )
    with system_access("test"):
        proxy = Proxy.objects.get(meeting_id=election.meeting_id, lot=w["lot_a"])
    assert proxy.holder_id == _person(w["owner_b"]).pk


# --- Accessibility and wording ---------------------------------------------------------------


def test_error_messages_use_role_alert(client, w):
    client.force_login(w["owner"])
    request = client.get(reverse("home")).wsgi_request
    from django.contrib.messages.storage.fallback import FallbackStorage
    from django.template.loader import render_to_string

    request._messages = FallbackStorage(request)
    request._messages.add(constants.ERROR, "Choose a lot.")
    request._messages.add(constants.SUCCESS, "Saved.")
    from django.contrib.messages import get_messages

    html = render_to_string("partials/messages.html", {"messages": get_messages(request)})
    assert re.search(r'role="alert">\s*<div class="message error">', html)
    assert re.search(r'role="status">\s*<div class="message success">', html)


def test_workflow_buttons_have_labels_and_specific_confirmations(client, w):
    from arc import services as arc
    from arc.services import ARC
    from compliance.services import VIOLATION
    from core.templatetags.portal import is_danger
    from operations.services import INVOICE

    for flow in (ARC, VIOLATION, INVOICE):
        for t in flow.transitions.values():
            if is_danger(t.action) or t.action in ("approve", "deny", "submit", "accept"):
                assert t.confirm and "?" in t.confirm, (flow.name, t.action)
    req = arc.create_request(lot=w["lot_a"], title="Fence", description="Rear", actor=w["owner"])
    client.force_login(w["owner"])
    html = client.get(reverse("arc_detail", args=[req.pk])).content.decode()
    assert "Withdraw this request? The review stops." in html
    assert 'placeholder="Note (optional)"' not in html
    assert re.search(r'<label for="wf-note-withdraw"', html)
    assert "ARC-" not in html


def test_every_state_shown_with_status_has_a_tone():
    known = set().union(*_TONES.values())
    ours = {
        "accounts",
        "community",
        "audit",
        "comms",
        "documents",
        "governance",
        "arc",
        "compliance",
        "operations",
        "ledger",
        "payments",
        "reports",
        "core",
    }
    missing = []
    for model in apps.get_models():
        if model._meta.app_label not in ours:
            continue
        for field in model._meta.get_fields():
            if field.name in ("state", "status", "outcome") and getattr(field, "choices", None):
                missing += [
                    f"{model.__name__}.{field.name}={v}" for v, _ in field.choices if v not in known
                ]
    assert not missing, missing


def test_short_date_shows_the_year_outside_this_month():
    today = datetime.date(2026, 11, 25)
    assert short_date(datetime.date(2026, 11, 3), today) == "Nov 3"
    assert short_date(datetime.date(2026, 12, 3), today) == "Dec 3, 2026"
    assert short_date(datetime.date(2027, 1, 3), today) == "Jan 3, 2027"
    assert short_date(None, today) == ""


def test_owner_visible_jargon_is_gone():
    for name, words in {
        "templates/compliance/detail.html": ["Cure by", "V-{{"],
        "templates/compliance/list.html": ["Cure by", "V-{{"],
        "templates/arc/list.html": ["ARC-{{"],
        "templates/arc/detail.html": ["ARC-{{", "You are recused", "(deemed)"],
        "templates/operations/request_detail.html": ["WO-{{"],
        "templates/documents/detail.html": ["v{{"],
        "templates/community/my_profile.html": ["JSON"],
        "templates/community/my_lot.html": ['class="badge">Account'],
    }.items():
        text = (ROOT / name).read_text()
        for word in words:
            assert word not in text, (name, word)


def test_records_requests_owner_side(client, w):
    from governance import services as gov

    gov.submit_records_request(description="2025 audit", actor=w["owner"])
    gov.submit_records_request(description="Board owner's own ask", actor=w["board"])
    client.force_login(w["board"])
    mine = client.get(reverse("records_requests"), {"mine": "1"}).content.decode()
    assert "Board owner" in mine and "2025 audit" not in mine
    assert "2025 audit" in client.get(reverse("records_requests")).content.decode()


def test_css_sizes_contrast_and_print():
    css = (ROOT / "static/css/site.css").read_text()
    assert "font-size: 17px" in css.split("body {", 1)[1].split("}", 1)[0]
    assert ":focus-visible { outline: 3px solid var(--green-700)" in css
    assert "#cfcec6" not in css  # low-contrast border is gone
    assert not re.search(r"(?<!-)color:\s*var\(--faint\)", css)
    inputs = css.split("input[type=tel], input[type=search]", 1)[1].split("}", 1)[0]
    assert "font-size: 1rem" in inputs and "min-height: 44px" in inputs
    printing = css.split("@media print", 1)[1].split("\n}", 1)[0]
    assert not re.search(r"[ ,]form[ ,{]", printing)
    assert "visibility: hidden" in css.split("@media (max-width: 820px)", 1)[1]
    assert "details.menu-details > .panel-body { position: absolute" in css


def test_menu_script_remembers_groups_safely_and_manages_focus():
    js = (ROOT / "static/js/app.js").read_text()
    assert "localStorage" in js and "try {" in js and "catch" in js
    assert 'setAttribute("inert"' in js and "button.focus()" in js
