"""Layout of 0.46.0 (D100): the 0.45.0 walkthrough's layout, naming,
placement, duplicate and long-page findings.

The menu: staff sections start closed except the current page's, work before
the person's own home, the choice remembered by static JS (CSP-safe). The
board dashboard: a short summary of My items per part, ordered board work,
Your home, finance glance, Getting started as one line. Long pages: My items
folds by group with "Show N more" and an Overdue filter; Interest, Collections
and a home's ledger page server-side; Month and year close folds the rest of
a check's exceptions. Every action stays on the page."""

from __future__ import annotations

import datetime
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from django.conf import settings
from django.urls import reverse
from django.utils import timezone

from community import services as community
from community.models import Role
from community.tests.factories import LotFactory, make_member
from core import features, my_items, navigation
from core.my_items import Item
from core.policy import SYSTEM

pytestmark = pytest.mark.django_db

ROOT = Path(settings.BASE_DIR)
CSS = (ROOT / "static/css/site.css").read_text()
APP_JS = (ROOT / "static/js/app.js").read_text()


def _rule(key, value, citation="Declaration §6.6 (test)"):
    community.set_rule(key=key, value=value, citation=citation, actor=SYSTEM)


def _html(client, user, url):
    client.force_login(user)
    response = client.get(url)
    assert response.status_code == 200, url
    return response.content.decode()


def _nav(rf, user, path="/"):
    request = rf.get(path)
    request.user = user
    return navigation.build(request)


@pytest.fixture
def w():
    return {
        "board": make_member(board=True, lots=[LotFactory(street_address="1 Layout Way")]),
        "owner": make_member(lots=[LotFactory(street_address="2 Layout Way")]),
        "manager": make_member(roles=[Role.MANAGER]),
        "auditor": make_member(roles=[Role.AUDITOR]),
        "sysadmin": make_member(roles=[Role.SYSADMIN]),
    }


# --- The menu (WT-009, WT-017, WT-019, WT-027, WT-028) ---------------------------------------


def test_staff_sections_start_closed_with_their_counts(rf, w):
    nav = _nav(rf, w["board"])
    titled = [s for s in nav["sections"] if s["title"]]
    assert titled and not [s for s in titled if s["open"]]
    # Collapsed headings still carry their count (the bubble on <summary>).
    template = (ROOT / "templates/partials/nav_section.html").read_text()
    assert "group-count" in template and "section.count" in template


def test_the_current_pages_section_is_the_one_open(rf, w):
    nav = _nav(rf, w["board"], reverse("collections"))
    opened = [s["title"] for s in nav["sections"] if s["title"] and s["open"]]
    assert opened == ["Finance"]
    finance = next(s for s in nav["sections"] if s["title"] == "Finance")
    assert finance["current"] and [i["label"] for i in finance["items"] if i["current"]] == [
        "Collections"
    ]
    nav = _nav(rf, w["board"], reverse("my_account"))
    assert [s["title"] for s in nav["sections"] if s["title"] and s["open"]] == ["My home"]


def test_owner_sections_are_unchanged(rf, w):
    nav = _nav(rf, w["owner"])
    assert {s["title"]: s["open"] for s in nav["sections"] if s["title"]} == {
        "My home": True,
        "Community": True,
    }
    assert not nav["work_first"]


def test_staff_menu_puts_work_before_their_own_home(client, w):
    html = _html(client, w["board"], reverse("home"))
    nav_html = html.split('<nav class="nav">')[1].split("</nav>")[0]
    assert nav_html.index("My items") < nav_html.index('id="nav-work-h"')
    assert nav_html.index('data-nav-group="association"') < nav_html.index(
        'data-nav-group="my-home"'
    )
    assert 'data-testid="nav-own"' in nav_html
    owner = _html(client, w["owner"], reverse("home"))
    assert 'data-testid="nav-own"' not in owner and 'data-nav-group="my-home" open' in owner


def test_every_menu_entry_is_still_drawn(client, w):
    """Reordering drops nothing: each section the menu builds is rendered once."""
    for user in w.values():
        client.force_login(user)
        response = client.get(reverse("home"))
        html = response.content.decode()
        for section in response.context["nav"]["sections"]:
            for item in section["items"]:
                assert html.count(f'href="{item["url"]}"') >= 1, item["label"]
            if section["title"]:
                assert html.count(f'data-nav-group="{section["key"]}"') == 1, section["title"]


def test_open_state_is_remembered_by_static_script_not_inline(client, w):
    assert "localStorage" in APP_JS and '"nav-group:"' in APP_JS
    assert 'addEventListener("toggle"' in APP_JS
    html = _html(client, w["board"], reverse("home"))
    # CSP: every script is a file; no inline script or handler.
    assert not re.search(r"<script(?![^>]*\ssrc=)[^>]*>", html)
    assert " onclick=" not in html and " ontoggle=" not in html


def test_former_owner_history_is_a_top_level_entry(rf):
    from core.tests.test_nav_streamline import _former_owner

    nav = _nav(rf, _former_owner())
    assert [(s["title"], [i["label"] for i in s["items"]]) for s in nav["sections"]] == [
        ("", ["Dashboard", "My history"])
    ]


def test_phone_tab_bar_per_role(rf, w):
    def tabs(user):
        return [t["name"] for t in _nav(rf, user)["tabbar"]]

    assert tabs(w["board"]) == ["home", "my_items", "requests"]
    assert tabs(w["manager"]) == ["home", "my_items", "requests"]
    assert tabs(w["auditor"]) == ["home", "reports", "accounting"]
    assert tabs(w["owner"])[:2] == ["home", "my_account"]


def test_account_links_are_two_by_two_44px_buttons_on_phones():
    block = CSS.split("WT-019). */")[1].split("}\n}")[0]
    assert "grid-template-columns: 1fr 1fr" in block
    assert int(re.search(r"min-height:\s*(\d+)px", block).group(1)) >= 44


def test_arc_menu_label_fits_one_line_and_matches_the_heading(client, w):
    html = _html(client, w["owner"], reverse("arc_list") + "?mine=1")
    assert "<h1>My ARC requests</h1>" in html
    assert f'href="{reverse("arc_list")}?mine=1"' in html
    assert "Architectural Review Committee (ARC)" in html  # ARC explained once


# --- The dashboard (WT-001, WT-018, WT-020, WT-010, WT-006) -----------------------------------


def _many_items(monkeypatch, *, board=0, home=0, overdue=0, area="Invoices"):
    late = timezone.localdate() - datetime.timedelta(days=3)
    items = [Item(area, f"Board item {n}", "x", f"/b/{n}/") for n in range(board)]
    items += [
        Item("Work orders", f"Late item {n}", "x", f"/l/{n}/", due=late) for n in range(overdue)
    ]
    items += [Item("My home", f"Home item {n}", "x", f"/h/{n}/") for n in range(home)]
    monkeypatch.setattr(my_items, "items_for", lambda user: list(items))
    return items


def test_board_dashboard_summarises_my_items_instead_of_copying_it(client, w, monkeypatch):
    _many_items(monkeypatch, board=12, home=5)
    html = _html(client, w["board"], reverse("home"))
    shown = re.findall(r"Board item \d+", html)
    assert len(shown) == 3 and len(set(shown)) == 3  # the top three, once each
    assert len(re.findall(r"Home item \d+", html)) == 3
    assert "Showing 3 of 12 · See all in My items" in html
    assert "Showing 3 of 5 · See all in My items" in html
    assert html.count(f'href="{reverse("my_items")}"') >= 2


def test_board_dashboard_order(client, w, monkeypatch):
    _many_items(monkeypatch, board=4, home=2)
    html = _html(client, w["board"], reverse("home"))
    order = ['id="part-board"', 'id="part-home"', 'id="finance-glance"']
    positions = [html.index(marker) for marker in order]
    assert positions == sorted(positions)
    if 'id="getting-started"' in html:
        assert html.index('id="finance-glance"') < html.index('id="getting-started"')
        # One line for staff: no copy of the steps (they are My items' too).
        part = html.split('id="getting-started"')[1].split("</section>")[0]
        assert 'class="list checklist"' not in part
    # The balance tile sits in Your home, not on its own before it.
    assert html.index('id="part-home"') < html.index('data-testid="home-balances"')


def test_groups_link_to_their_card_on_my_items(w, monkeypatch):
    from core import dashboard

    _many_items(monkeypatch, board=2, overdue=2)
    links = dashboard.group_links(my_items.items_for(w["board"]), "board")
    assert {g["area"]: g["url"] for g in links} == {
        "Invoices": reverse("my_items") + "#area-board-invoices",
        "Work orders": reverse("my_items") + "#area-board-work-orders",
    }
    assert {g["area"]: g["overdue"] for g in links}["Work orders"] == 2


def test_staff_without_a_home_get_the_top_five(client, w, monkeypatch):
    _many_items(monkeypatch, board=9)
    html = _html(client, w["manager"], reverse("home"))
    assert len(re.findall(r"Board item \d+", html)) == 5
    assert "Showing 5 of 9 · See all in My items" in html
    assert html.index('id="todos"') < html.index('id="finance-glance"')


def test_quick_actions_do_not_repeat_the_menu(client, w):
    html = _html(client, w["owner"], reverse("home"))
    quick = html.split('id="quick-actions"')[1].split("</nav>")[0]
    for label in ("My profile", "Sign-in and security", "Governing documents"):
        assert label not in quick, label
    assert "Ask the board a question" in quick and "My ARC requests" in quick


def test_sysadmin_dashboard_has_a_fix_button_per_failing_check(client, w):
    from core import dashboard

    assert dashboard.check_action("backups") == {
        "label": "Take a backup",
        "url": reverse("server_tasks") + "#backups",
    }
    assert dashboard.check_action("disk")["url"] == reverse("system_status")
    html = _html(client, w["sysadmin"], reverse("home"))
    assert 'id="system-summary"' in html and 'id="shortcuts"' not in html
    if 'data-testid="failing-checks"' in html:
        assert 'class="btn small secondary todo-action"' in html


def test_board_reaches_meetings_from_the_dashboard(client, w):
    html = _html(client, w["board"], reverse("home"))
    assert f'href="{reverse("calendar")}?type=meetings#results"' in html
    owner = _html(client, w["owner"], reverse("home"))
    assert 'data-testid="dash-meetings"' not in owner


# --- My items folds by group (WT-020) ---------------------------------------------------------


def test_my_items_groups_fold_and_show_more(client, w, monkeypatch):
    from core.my_items_views import GROUP_SHOWN

    _many_items(monkeypatch, board=10, overdue=2)
    html = _html(client, w["manager"], reverse("my_items"))
    # Every item is still on the page; past the first few they sit behind "Show N more".
    assert len(set(re.findall(r"Board item \d+", html))) == 10
    assert f"Show {10 - GROUP_SHOWN} more" in html
    assert 'data-testid="more-area-all-invoices"' in html
    # Each group's heading is a 44px summary with its count.
    assert re.search(r'<details class="card fold-group" id="area-all-invoices" open>', html)
    assert "Overdue (2)" in html and "All (12)" in html


def test_my_items_later_groups_start_closed_unless_overdue(w):
    from core.my_items_views import _groups

    late = timezone.localdate() - datetime.timedelta(days=1)
    items = [
        Item("Owner requests", "a", "x", "/a/"),
        Item("Invoices", "b", "x", "/b/"),
        Item("Work orders", "c", "x", "/c/"),
        Item("Renewals", "d", "x", "/d/", due=late),
    ]
    part = {"key": "all", "items": items, "counts": my_items.counts(items)}
    opened = {g["area"]: g["open"] for g in _groups(part)}
    assert opened == {
        "Owner requests": True,
        "Invoices": True,
        "Work orders": False,
        "Renewals": True,
    }


def test_my_items_overdue_filter(client, w, monkeypatch):
    _many_items(monkeypatch, board=6, overdue=2)
    html = _html(client, w["manager"], reverse("my_items") + "?show=overdue")
    assert len(set(re.findall(r"Late item \d+", html))) == 2
    assert not re.findall(r">Board item \d+<", html)


# --- Interest on overdue balances (WT-021) ----------------------------------------------------


def _overdue_homes(n):
    from ledger import services as ledger
    from ledger.cashflow import add_months

    lots = [
        make_member(lots=[LotFactory(street_address=f"{k + 10} Interest Way")]) for k in range(n)
    ]
    _rule("dues.monthly_cents", "4500", "Budget resolution (test)")
    for back in (3, 2):
        m = add_months(timezone.localdate().replace(day=1), -back)
        ledger.post_monthly_assessments(year=m.year, month=m.month, actor=SYSTEM)
    return lots


def test_interest_pages_this_months_homes_and_folds_posted_months(client, w):
    from ledger import interest
    from ledger.finance_option_views import INTEREST_PAGE_SIZE

    _overdue_homes(INTEREST_PAGE_SIZE + 3)
    features.set_feature("overdue_interest", True, actor=SYSTEM)
    _rule("interest.annual_rate_bp", "1200")
    _rule("interest.grace_days", "30", "Collection policy (test)")
    today = timezone.localdate()
    total = len(interest.candidates(year=today.year, month=today.month))
    assert total > INTEREST_PAGE_SIZE
    url = reverse("interest")
    page1 = _html(client, w["board"], url)
    assert page1.count('class="cell-title"><a href=') == INTEREST_PAGE_SIZE
    assert f"{total} homes" in page1 and "Page 1 of 2" in page1 and "page=2" in page1
    page2 = _html(client, w["board"], url + "?page=2")
    assert page2.count('class="cell-title"><a href=') == total - INTEREST_PAGE_SIZE
    # The Post button charges every home whichever page is shown.
    if "Post this month" in page1:
        assert f"on {total} accounts" in page2
    # Posted charges: by month, the newest open, each with its Reverse.
    from ledger.cashflow import add_months

    m = add_months(today.replace(day=1), -1)
    interest.post_interest(year=m.year, month=m.month, actor=SYSTEM)
    html = _html(client, w["board"], url)
    first = interest.recent_postings()[0]
    assert f'id="posted-{first.posted_on:%Y-%m}" open' in html
    assert html.count('name="do" value="reverse"') == len(interest.recent_postings())


def test_interest_months_unit():
    from ledger.finance_option_views import interest_months

    e = lambda d, c: SimpleNamespace(posted_on=d, amount_cents=c)  # noqa: E731
    months = interest_months(
        [
            e(datetime.date(2026, 11, 15), 100),
            e(datetime.date(2026, 11, 15), 50),
            e(datetime.date(2026, 10, 15), 7),
        ]
    )
    assert [(m["key"], len(m["entries"]), m["total"], m["open"]) for m in months] == [
        ("2026-11", 2, 150, True),
        ("2026-10", 1, 7, False),
    ]


# --- Collections and a home's ledger (WT-021) -------------------------------------------------


def test_collections_pages_overdue_homes_with_the_total_on_top(client, w):
    from ledger import services as ledger
    from ledger.views import COLLECTIONS_PAGE_SIZE

    _overdue_homes(COLLECTIONS_PAGE_SIZE + 2)
    count = len(ledger.delinquent_lots())
    assert count > COLLECTIONS_PAGE_SIZE
    html = _html(client, w["board"], reverse("collections"))
    assert 'data-testid="overdue-summary"' in html and f"{count} homes owe" in html
    assert html.count('class="row-main" href="/finance/ledger/lot/') == COLLECTIONS_PAGE_SIZE
    page2 = _html(client, w["board"], reverse("collections") + "?page=2")
    assert (
        page2.count('class="row-main" href="/finance/ledger/lot/') == count - COLLECTIONS_PAGE_SIZE
    )
    # Empty age columns are left out on phones only.
    assert "hide-sm" in html and "table.stack-sm td.hide-sm { display: none; }" in CSS


def test_a_homes_ledger_pages_its_entries_and_keeps_the_actions(client, w):
    from ledger import services as ledger
    from ledger.views import LEDGER_PAGE_SIZE

    lot = LotFactory(street_address="9 Ledger Way")
    make_member(lots=[lot])
    for n in range(LEDGER_PAGE_SIZE + 5):
        ledger.record_payment(
            lot=lot,
            amount_cents=100 + n,
            received_on=timezone.localdate() - datetime.timedelta(days=n),
            actor=w["manager"],
        )
    url = reverse("lot_ledger", args=[lot.pk])
    page1 = _html(client, w["manager"], url)
    assert page1.count('name="entry"') >= LEDGER_PAGE_SIZE  # each row keeps Reverse
    assert f"(newest first; {LEDGER_PAGE_SIZE + 5}; 1\u2013{LEDGER_PAGE_SIZE} shown)" in page1
    # Newest first (review BUG-002, 0.46.0): today's payment ($1.00) opens the
    # page, the oldest ($1.29, 29 days ago) is on the last page.
    assert "1.00" in page1 and "1.29" not in page1
    assert page1.index("1.00") < page1.index("1.01")
    page2 = _html(client, w["manager"], url + "?page=2")
    assert "Page 2 of 2" in page2
    assert "1.29" in page2 and "1.00" not in page2
    assert page2.count('action="' + reverse("lot_ledger_action", args=[lot.pk, "reverse"])) == 5


# --- Month and year close folds a check's exceptions (WT-021) ---------------------------------


def test_close_shows_five_exceptions_and_folds_the_rest(client, w, monkeypatch):
    from ledger.books import close as bclose
    from ledger.books import views as books_views

    items = [
        bclose.Item(f"line-{n}", f"Bank line {n}", obj=SimpleNamespace(pk=1000 + n))
        for n in range(8)
    ]
    check = bclose.Check("bank_lines", "Every bank line is matched or coded", True, "ok", items)
    ev = SimpleNamespace(
        close=None,
        period="2026-10",
        label="October 2026",
        is_closed=False,
        ended=True,
        passed=False,
        blocking=8,
        checks=[check],
        accounts=[],
        closable=False,
        ends_on=datetime.date(2026, 10, 31),
    )
    monkeypatch.setattr(books_views.bclose, "evaluate_month", lambda key: ev)
    html = _html(client, w["board"], reverse("books_close") + "?period=2026-10")
    assert html.count('data-testid="exception-bank_lines"') == 8  # nothing dropped
    assert "Show 3 more" in html and 'data-testid="more-bank_lines"' in html
    assert html.index("Bank line 4") < html.index("Show 3 more") < html.index("Bank line 5")
    ids = re.findall(r'id="(cat\d+)"', html)
    assert len(ids) == len(set(ids)) == 8


# --- Naming, placement and empty cards (WT-002, WT-003, WT-005, WT-011, WT-015) ---------------


def test_finance_cards_with_a_menu_entry_say_they_are_shortcuts(client, w):
    html = _html(client, w["board"], reverse("finance_overview"))
    assert "Shortcut: also in the menu under Maintenance" in html  # Vendors
    assert "Shortcut: also in the menu under Finance" in html  # Owner accounts


def test_setup_count_names_the_review_step(client, w):
    html = _html(client, w["board"], reverse("settings_hub"))
    if "Setup has not been run yet" in html:
        assert re.search(r"\d+ steps? to go, then step \d+, the review of every decision", html)


def test_setup_review_folds_unflagged_steps(client, w):
    html = _html(client, w["board"], reverse("setup_step", args=["review"]))
    assert 'data-testid="review-lines-' in html
    assert "Change" in html  # every step's Change button stays outside the fold


def test_former_owner_history_shows_no_empty_cards(client):
    from core.tests.test_nav_streamline import _former_owner

    html = _html(client, _former_owner(), reverse("my_history"))
    assert "None." not in html
    assert 'data-testid="no-other-records"' in html


def test_new_controls_are_44px_tap_targets():
    for selector in (
        "details.more-items > summary {",
        "details.fold-group > summary.fold-head {",
        ".card-head .card-links a {",
    ):
        rule = CSS.split(selector)[1].split("}")[0]
        assert int(re.search(r"min-height:\s*(\d+)px", rule).group(1)) >= 44, selector
