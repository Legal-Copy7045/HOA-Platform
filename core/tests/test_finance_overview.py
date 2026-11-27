"""Finance overview and hub tile counts (decision D89).

The owner: "We need a finance section, with a dashboard for all the different
components. The "More Finance" has a icon with (5) in it, but when you click
in, you can't see which section has the 5 items." Every hub tile and every
overview card shows its own waiting count; the menu bubble is their sum."""

import pytest
from django.urls import reverse

from community.models import Role
from community.tests.factories import LotFactory, make_member
from core import features, navigation
from core.my_items import Item
from ledger import finance_overview

pytestmark = pytest.mark.django_db


@pytest.fixture
def board():
    return make_member(board=True, lots=[LotFactory()])


def _entries(response):
    return {i["name"]: i for s in response.context["nav"]["sections"] for i in s["items"]}


def _fake_items(monkeypatch, items):
    from core import my_items

    monkeypatch.setattr(my_items, "items_for", lambda user: list(items))


FINANCE_ITEMS = [
    Item("Finance", "Refund waiting", "x", "/d/", nav="disbursements"),
    Item("Finance", "Refund 2 waiting", "x", "/d/", nav="disbursements"),
    Item("Finance", "Payment problem", "x", "/o/", nav="online_payments"),
    Item("Invoices", "Invoice PP-1", "x", "/i/"),
    Item("Collections", "Follow up", "x", "/c/"),
    Item("Finance", "Moved entries", "x", "/l/", nav="ledger_home"),
]


def test_overview_cards_carry_their_own_counts_and_the_bubble_is_their_sum(
    client, board, monkeypatch
):
    features.set_feature("online_payments", True, actor=board)
    _fake_items(monkeypatch, FINANCE_ITEMS)
    client.force_login(board)
    response = client.get(reverse("finance_overview"))
    assert response.status_code == 200
    html = response.content.decode()
    assert "<h1>Finance overview</h1>" in html
    cards = {c["label"]: c for g in response.context["groups"] for c in g["cards"]}
    assert cards["Disbursements"]["count"] == 2
    assert cards["Online payments"]["count"] == 1
    assert cards["Invoices"]["count"] == 1
    assert cards["Collections"]["count"] == 1
    assert cards["Owner accounts"]["count"] == 1
    total = sum(c["count"] for c in cards.values())
    entry = _entries(response)["finance_overview"]
    assert entry["count"] == total == 6 and entry["current"]
    # The Finance section's own total counts nothing twice.
    finance = next(s for s in response.context["nav"]["sections"] if s["title"] == "Finance")
    assert finance["count"] == 6
    assert html.count('data-testid="card-count"') == 5


def test_finance_menu_is_short_and_more_finance_is_gone(client, board):
    client.force_login(board)
    response = client.get(reverse("home"))
    finance = next(s for s in response.context["nav"]["sections"] if s["title"] == "Finance")
    assert [i["label"] for i in finance["items"]] == [
        "Finance overview",
        "Owner accounts",
        "Invoices",
        "Collections",
        "Accounting",
        "Reports",
    ]
    assert client.get(reverse("finance_more"))["Location"] == reverse("finance_overview")
    # Pages that open from the overview mark it current.
    for name in ("budgets", "reserve_plan", "disbursements", "resales"):
        response = client.get(reverse(name))
        if response.status_code != 200:
            continue
        current = [i["label"] for i in _entries(response).values() if i["current"]]
        assert current == ["Finance overview"], name
        crumbs = response.content.decode().split('aria-label="Breadcrumb">')[1].split("</nav>")[0]
        assert f'href="{reverse("finance_overview")}"' in crumbs, name


def test_overview_is_role_scoped(client, board):
    features.set_feature("online_payments", True, actor=board)
    features.set_feature("disbursements", True, actor=board)
    client.force_login(board)
    board_cards = {
        c["label"]
        for g in client.get(reverse("finance_overview")).context["groups"]
        for c in g["cards"]
    }
    assert {"Online payments", "Disbursements", "Resale certificates"} <= board_cards
    for role in (Role.MANAGER, Role.AUDITOR):
        user = make_member(roles=[role])
        client.force_login(user)
        response = client.get(reverse("finance_overview"))
        assert response.status_code == 200, role
        labels = {c["label"] for g in response.context["groups"] for c in g["cards"]}
        assert "Online payments" not in labels  # payments.manage is the board's
        groups = navigation.policy.roles_for(user).groups
        for card in finance_overview.CARDS:
            if card.label in labels:
                assert navigation.policy.PERMISSIONS[card.permission] & groups
        html = response.content.decode()
        assert reverse("online_payments") not in html.split("<h1>")[1]
    for user in (make_member(lots=[LotFactory()]), make_member(roles=[Role.ARC_REVIEWER])):
        client.force_login(user)
        assert client.get(reverse("finance_overview")).status_code in (403, 404)
        assert client.get(reverse("finance_more")).status_code in (403, 404)


def test_switched_off_features_leave_the_overview(client, board):
    features.set_feature("disbursements", False, actor=board)
    client.force_login(board)
    labels = {
        c["label"]
        for g in client.get(reverse("finance_overview")).context["groups"]
        for c in g["cards"]
    }
    assert "Disbursements" not in labels


def test_card_labels_are_the_pages_headings(client, board):
    for key in ALL_FINANCE_FEATURES:
        features.set_feature(key, True, actor=board)
    client.force_login(board)
    for card in finance_overview.CARDS:
        response = client.get(reverse(card.url_name))
        assert response.status_code == 200, card.url_name
        html = response.content.decode()
        if card.anchor:  # a section of a page: its heading and anchor
            assert f'id="{card.anchor[1:]}"' in html, card.label
            continue
        assert f"<h1>{card.label}</h1>" in html, card.url_name


ALL_FINANCE_FEATURES = (
    "online_payments",
    "disbursements",
    "reserves",
    "bank_feeds",
    "books",
    "financial_statements",
    "special_assessments",
    "payment_plans",
    "reimbursements",
    "vendor_checks",
    "overdue_interest",
    "cashflow_forecast",
    "capital_projects",
    "insurance_claims",
    "vendor_onboarding",
)


def test_every_old_more_finance_page_is_a_card_and_marks_the_overview(client, board):
    """The 0.40.0 More finance tiles all open from the overview (D89), which
    is current on them, and their My items land on their own cards."""
    for key in ALL_FINANCE_FEATURES:
        features.set_feature(key, True, actor=board)
    old_hub = (
        "reserve_plan",
        "online_payments",
        "disbursements",
        "books_accounts",
        "books_coding",
        "tax_worksheet",
        "special_assessments",
        "payment_plans",
        "reimbursements",
        "vendor_checks",
        "resales",
        "interest",
        "cashflow",
    )
    client.force_login(board)
    response = client.get(reverse("finance_overview"))
    urls = {c["url"] for g in response.context["groups"] for c in g["cards"]}
    for name in old_hub:
        assert reverse(name) in urls, name
        assert name in navigation.SUBPAGES["finance_overview"], name
        page = client.get(reverse(name))
        current = [i["label"] for i in _entries(page).values() if i["current"]]
        assert current == ["Finance overview"], name


def test_finance_items_land_on_their_cards(client, board, monkeypatch):
    for key in ALL_FINANCE_FEATURES:
        features.set_feature(key, True, actor=board)
    _fake_items(
        monkeypatch,
        [
            Item("Finance", "Notice", "x", "/s/", nav="special_assessments"),
            Item("Finance", "Low", "x", "/c/", nav="cashflow"),
            Item("Finance", "Approve", "x", "/r/", nav="reimbursements"),
            Item("Collections", "Plan", "x", "/p/", nav="payment_plans"),
            Item("Finance", "Close", "x", "/b/", nav="books_close"),
        ],
    )
    client.force_login(board)
    response = client.get(reverse("finance_overview"))
    cards = {c["label"]: c["count"] for g in response.context["groups"] for c in g["cards"]}
    for label in (
        "Special assessments",
        "Cash-flow forecast",
        "Reimbursements",
        "Payment plans",
        "Month and year close",
    ):
        assert cards[label] == 1, label
    assert _entries(response)["finance_overview"]["count"] == sum(cards.values()) == 5


def test_navs_of_finance_items_are_their_pages():
    """No finance item counts toward the retired More finance hub."""
    from pathlib import Path

    from django.conf import settings

    root = Path(settings.BASE_DIR)
    for path in (
        "ledger/special_assessments.py",
        "ledger/cashflow.py",
        "operations/reimbursements.py",
    ):
        assert 'nav="finance_more"' not in (root / path).read_text(), path
    assert 'nav="payment_plans"' in (root / "ledger/payment_plans.py").read_text()


def test_fund_strip_when_the_books_have_a_closed_month(client, board, monkeypatch):
    import datetime

    from ledger.books import close

    features.set_feature("books", True, actor=board)
    monkeypatch.setattr(
        close,
        "closed_fund_positions",
        lambda: (
            datetime.date(2026, 10, 31),
            {
                "operating": {"cash": 1234500, "balance": 0},
                "reserve": {"cash": 50000, "balance": 0},
            },
        ),
    )
    client.force_login(board)
    html = client.get(reverse("finance_overview")).content.decode()
    assert 'data-testid="fund-strip"' in html and "$12,345.00" in html
    assert "October 2026" in html
    monkeypatch.setattr(close, "closed_fund_positions", lambda: None)
    html = client.get(reverse("finance_overview")).content.decode()
    assert 'data-testid="fund-strip"' not in html


def test_overview_queries_are_bounded(client, board):
    """The cards cost the same however many homes there are (no query per
    home or per card row)."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    client.force_login(board)
    client.get(reverse("finance_overview"))  # warm caches

    def queries():
        with CaptureQueriesContext(connection) as ctx:
            assert client.get(reverse("finance_overview")).status_code == 200
        return len(ctx.captured_queries)

    few = queries()
    for _ in range(15):
        LotFactory()
    assert queries() <= few + 2
    # The cards themselves: a few queries each (a rule read is one), never one per home.
    from ledger import finance_overview as fo

    request = client.get(reverse("finance_overview")).wsgi_request
    ctx = fo._Context(request)
    for card in fo.CARDS:
        with CaptureQueriesContext(connection) as cq:
            card.figure(ctx)
        assert len(cq.captured_queries) <= 8, card.label


def test_overview_cards_stack_on_phones():
    from pathlib import Path

    from django.conf import settings

    css = (Path(settings.BASE_DIR) / "static/css/site.css").read_text()
    assert "grid-template-columns: repeat(auto-fill, minmax(230px, 1fr))" in css
    assert ".tiles .tile .badge-count" in css


# --- Hub tiles: each shows its own count; the bubble is their sum -------------------------


def test_hub_tiles_show_their_own_counts_and_sum_to_the_bubble(client, board, monkeypatch):
    _fake_items(
        monkeypatch,
        [
            Item("Records retention", "Dispose", "x", "/r/", nav="records_retention"),
            Item("Records retention", "Dispose 2", "x", "/r/", nav="records_retention"),
        ],
    )
    features.set_feature("records_retention", True, actor=board)
    client.force_login(board)
    response = client.get(reverse("settings_hub"))
    tiles = {link["name"]: link["count"] for link in response.context["links"]}
    assert tiles.get("records_retention") == 2
    entry = _entries(response)["settings_hub"]
    assert entry["count"] == sum(tiles.values()) + response.context["setup_count"]
    assert 'data-testid="tile-count">2<' in response.content.decode()


def test_every_hub_bubble_equals_its_tiles(rf, board, monkeypatch):
    _fake_items(
        monkeypatch,
        [Item("Records retention", "Dispose", "x", "/r/", nav="records_retention")],
    )
    request = rf.get("/")
    request.user = board
    counts = navigation.waiting_counts(request)
    groups = navigation.policy.roles_for(board).groups
    for hub in navigation.HUBS:
        links = navigation.hub_links(request, hub, groups, counts)
        found = navigation._item(request, groups, hub, hub, "x", navigation.HUB, counts)
        if found is None:
            continue
        item, _children = found
        assert item["count"] == counts.get(hub, 0) + sum(link["count"] for link in links), hub


def test_action_rows_never_touch_the_tiles_below():
    """The dashboard's Record payments received sat on the stat tiles."""
    from pathlib import Path

    from django.conf import settings

    css = (Path(settings.BASE_DIR) / "static/css/site.css").read_text()
    assert ".form-actions + .grid, .form-actions + .stat, .quick-row + .grid" in css
    js = (Path(settings.BASE_DIR) / "regression/walkthrough.js").read_text()
    assert "buttons and boxes do not overlap or touch" in js
