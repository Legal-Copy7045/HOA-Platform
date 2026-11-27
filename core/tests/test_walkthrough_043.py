"""Walkthrough fixes in release 0.43 (WT-002..WT-049): the dashboard order,
one bank status on the Finance overview, names that match the menu, page
titles that start with the heading, breadcrumbs, and the shared CSS fixes
(checklist spacing, panel +/- box, count bubbles, tap targets)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from django.conf import settings
from django.urls import reverse

from community.tests.factories import LotFactory, make_member
from core import my_items, navigation, policy
from ledger import finance_overview

pytestmark = pytest.mark.django_db
CSS = (Path(settings.BASE_DIR) / "static/css/site.css").read_text()


def _page(client, user, url):
    client.force_login(user)
    response = client.get(url)
    assert response.status_code == 200, url
    return response.content.decode()


# --- WT-002: board work before the Getting started checklist ----------------------------


def test_board_dashboard_shows_the_work_before_the_checklist(client):
    board = make_member(board=True, lots=[LotFactory(street_address="1 Alpha Way")])
    html = _page(client, board, reverse("home"))
    assert 'id="getting-started"' in html
    assert html.index('id="part-board"') < html.index('id="getting-started"')


def test_owner_dashboard_keeps_the_checklist_on_top(client):
    owner = make_member(lots=[LotFactory(street_address="2 Alpha Way")])
    html = _page(client, owner, reverse("home"))
    start = html.index('id="getting-started"')
    assert start < html.index('id="todos"') if 'id="todos"' in html else True


# --- WT-003 / WT-034: checklist title and description apart -----------------------------


def test_checklist_title_and_description_are_separated(client):
    owner = make_member(lots=[LotFactory(street_address="3 Alpha Way")])
    html = _page(client, owner, reverse("getting_started"))
    assert re.search(r'</span> <span class="small muted todo-detail">', html)
    assert ".checklist-step .title, .checklist-step .todo-detail { display: block; }" in CSS


# --- WT-004 / WT-005: one bank status, interest with its unit ---------------------------


@pytest.mark.parametrize(
    "feed,books,feed_text,books_text",
    [
        (0, 0, "No bank account connected.", "No bank account in the books yet."),
        (1, 0, "not yet set up in the books", "The bank feed has 1 account connected"),
        (1, 1, "1 bank account in the books", "1 bank account in the books."),
    ],
)
def test_bank_feed_and_books_cards_agree(monkeypatch, feed, books, feed_text, books_text):
    monkeypatch.setattr(finance_overview, "_bank_counts", lambda: (feed, books))
    assert feed_text in finance_overview._bank_feed(None)
    assert books_text in finance_overview._books_accounts(None)


def test_interest_card_shows_a_percent_a_month(monkeypatch):
    from ledger import interest

    class P:
        rate_bp = 1000

    monkeypatch.setattr(interest, "current_policy", lambda: P())
    text = finance_overview._interest(None)
    assert text.endswith("% a month on balances past due.") and "0.833" in text


def test_overview_count_bubbles_stay_on_the_title_line():
    assert ".tiles .finance-card strong > .badge-count { flex: none;" in CSS


# --- WT-010: My items groups named after the menu ----------------------------------------


def test_my_items_groups_are_menu_entries():
    assert "Finance" not in my_items.AREAS and "Finance overview" in my_items.AREAS
    assert "Approvals" not in my_items.AREAS and "Written consent" not in my_items.AREAS
    assert "Resolutions and policies" in my_items.AREAS
    menu = {label for _s, entries in navigation.SECTIONS for label, *_ in entries}
    for area in ("Finance overview", "Resolutions and policies"):
        assert area in menu, area


# --- WT-014: one name for My profile ------------------------------------------------------


def test_my_profile_has_one_name(client):
    owner = make_member(lots=[LotFactory(street_address="4 Alpha Way")])
    html = _page(client, owner, reverse("home"))
    assert f'<a href="{reverse("my_profile")}">My profile</a>' in html
    assert "Update my contact details" not in html
    started = _page(client, owner, reverse("getting_started"))
    assert ">Contact details</a>" in started and ">Statement email</a>" in started


# --- WT-015 / WT-040: a role, never an email, under the name ------------------------------


@pytest.mark.parametrize(
    "groups,expected",
    [
        ({policy.AUTHORIZED}, ["Account contact"]),
        ({policy.VIEW_ONLY}, ["View-only contact"]),
        ({policy.TENANT}, ["Tenant"]),
        ({policy.FORMER_OWNER}, ["Former owner"]),
    ],
)
def test_every_resident_group_has_a_role_label(groups, expected):
    assert navigation.role_labels(policy.Roles(groups=frozenset(groups))) == expected


def test_an_office_follows_the_board_seat():
    roles = policy.Roles(
        groups=frozenset({policy.OWNER, policy.BOARD}), offices=frozenset({"treasurer"})
    )
    assert navigation.role_labels(roles) == ["Owner", "Board", "Treasurer"]


# --- WT-017: setup steps say they are setup ------------------------------------------------


def test_setup_step_headings_and_titles_differ_from_the_work_pages(client):
    board = make_member(board=True, lots=[LotFactory(street_address="5 Alpha Way")])
    html = _page(client, board, reverse("setup_step", args=["vendors"]))
    assert "<h1>Setup: Vendors</h1>" in html
    assert "<title>Setup: Vendors ·" in html
    assert re.search(r"Community setup</a> / Step \d+ of \d+</div>", html)


# --- WT-021: the tab title starts with the heading ----------------------------------------


@pytest.mark.parametrize(
    "url_name,heading",
    [("faq", "Frequently asked questions"), ("requests", "My requests")],
)
def test_tab_title_starts_with_the_heading(client, url_name, heading):
    owner = make_member(lots=[LotFactory(street_address="6 Alpha Way")])
    html = _page(client, owner, reverse(url_name))
    assert f"<h1>{heading}</h1>" in html or f">{heading}</h1>" in html
    assert re.search(rf"<title>\s*{re.escape(heading)} ·", html)


# --- WT-022 / WT-049: breadcrumbs end in the page, and are tap targets -----------------------


def test_help_breadcrumbs_name_the_page(client):
    owner = make_member(lots=[LotFactory(street_address="7 Alpha Way")])
    html = _page(client, owner, reverse("getting_started"))
    assert "Help</a> / Getting started</nav>" in html
    assert "Help</a> / </nav>" not in html
    assert ".crumbs a { min-width: 44px; justify-content: center; }" in CSS


# --- WT-026: the tasks page is not "Board tasks" for the manager ---------------------------


def test_tasks_menu_entry_is_not_board_only_wording():
    labels = {label for _s, entries in navigation.SECTIONS for label, *_ in entries}
    assert "Board tasks" not in labels and "Tasks and deadlines" in labels


# --- Shared CSS: the panel +/- box and icon buttons, row checkboxes -------------------------


def test_panel_plus_is_drawn_not_a_glyph():
    rule = CSS.split("details.panel > summary::before {")[1].split("}")[0]
    assert 'content: ""' in rule and "flex: none" in rule and "linear-gradient" in rule


def test_row_checkboxes_have_a_44px_tap_target(client):
    assert ".tap-check, td label:has(> input[type=checkbox]) { display: inline-flex; " in CSS
    assert "min-width: 44px; min-height: 44px;" in CSS
    for name in ("operations/vendor_checks.html", "documents/records_retention.html"):
        text = (Path(settings.BASE_DIR) / "templates" / name).read_text()
        assert '<label class="tap-check"><input type="checkbox"' in text, name
