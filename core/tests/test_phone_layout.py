"""Phone layout and the shared UI shell (walkthrough fixes, decision D48).

Tap targets, stacked tables, in-place filter swaps, one control per form,
the phone tab bar, the error pages and the month choice for the monthly
charge. Layout itself is checked in a browser by the walkthrough; these
tests pin the markup and the server behaviour it relies on."""

import datetime
import re
from pathlib import Path

import pytest
from django.conf import settings
from django.urls import reverse
from django.utils import timezone

from community import services as community
from community.models import Account, Person
from community.tests.factories import LotFactory, make_member
from core.policy import SYSTEM
from core.scoping import system_access
from ledger import services as ledger

pytestmark = pytest.mark.django_db

ROOT = Path(settings.BASE_DIR)
CSS = (ROOT / "static/css/site.css").read_text()
JS = (ROOT / "static/js/app.js").read_text()
XHR = {"HTTP_X_REQUESTED_WITH": "XMLHttpRequest"}


@pytest.fixture
def w():
    lot_a, lot_b = LotFactory(), LotFactory()
    owner = make_member(lots=[lot_a])
    owner_b = make_member(lots=[lot_b])
    data = {
        "board": make_member(board=True, lots=[LotFactory()]),
        "board2": make_member(board=True),
        "owner": owner,
        "owner_b": owner_b,
        "lot_a": lot_a,
        "lot_b": lot_b,
    }
    community.set_rule(
        key="dues.monthly_cents",
        value="7500",
        citation="Declaration §7.2 (demo)",
        actor=data["board"],
    )
    for kind in ("authorized", "view_only"):
        user = make_member()
        with system_access("test"):
            person = Person.objects.get(user=user)
            account = Account.objects.get(lot=lot_a, closed_on__isnull=True)
        community.add_account_contact(account, person=person, kind=kind, actor=SYSTEM)
        data[kind] = user
    return data


def _page(client, user, url):
    client.force_login(user)
    response = client.get(url)
    assert response.status_code == 200, url
    return response.content.decode()


# --- Resale link and error pages (WT-001, WT-048) -----------------------------------------


@pytest.mark.parametrize("who", ["authorized", "view_only"])
def test_contacts_see_no_resale_link_and_are_told_why(client, w, who):
    page = _page(client, w[who], reverse("my_account"))
    assert reverse("resales") not in page
    assert "Only an owner on the deed can ask for a resale certificate." in page


def test_owner_still_sees_the_resale_link(client, w):
    page = _page(client, w["owner"], reverse("my_account"))
    assert f'href="{reverse("resales")}"' in page
    assert "Only an owner on the deed" not in page


@pytest.mark.parametrize("who", ["authorized", "view_only"])
def test_refused_page_keeps_the_portal_layout(client, w, who):
    client.force_login(w[who])
    response = client.get(reverse("resales"))
    assert response.status_code == 403
    page = response.content.decode()
    assert 'name="viewport"' in page and 'class="shell"' in page
    assert "You don't have access to this page" in page and f'href="{reverse("home")}"' in page


def test_missing_and_out_of_scope_pages_look_the_same(client, w):
    client.force_login(w["owner"])
    other = client.get(reverse("lot_detail", args=[w["lot_b"].pk]))
    missing = client.get("/no-such-page/")
    assert other.status_code == missing.status_code == 404
    for response in (other, missing):
        page = response.content.decode()
        assert 'data-testid="error-404"' in page and 'name="viewport"' in page
        assert w["lot_b"].label not in page


# --- Stacked money tables (WT-027) ---------------------------------------------------------


def test_money_tables_stack_on_phones_with_labels(client, w):
    ledger.post_monthly_assessments(year=2026, month=1, actor=SYSTEM)
    page = _page(client, w["owner"], reverse("my_account"))
    assert page.count('class="stack-sm') >= 2
    assert 'data-label="Balance"' in page and 'data-label="Open"' in page
    page = _page(client, w["board"], reverse("lot_ledger", args=[w["lot_a"].pk]))
    assert '<table class="stack-sm">' in page and 'data-label="Amount"' in page
    for url in (
        reverse("ledger_home"),
        reverse("invoices"),
        reverse("collections"),
        reverse("lot_list") + "?view=signups",  # the board's sign-ups (D50)
    ):
        assert "stack-sm" in _page(client, w["board"], url), url
    assert "table.stack-sm thead { display: none; }" in CSS


def test_main_actions_come_first(client, w):
    """WT-043: Pay online / statement before the statement; the lot account's
    payment, charge and credit forms before the entries."""
    page = _page(client, w["owner"], reverse("my_account"))
    assert page.index("Download statement (PDF)") < page.index("<h3>Statement</h3>")
    page = _page(client, w["board"], reverse("lot_ledger", args=[w["lot_a"].pk]))
    assert page.index("<summary>Record a payment</summary>") < page.index("<h2>All entries</h2>")


# --- One control per form (owner feedback, WT-009) ---------------------------------------


def test_request_header_button_only_while_the_panel_is_closed(client, w):
    from operations import services as operations

    operations.submit_request(
        lot=w["lot_a"],
        request_type="general",
        subject="Pond",
        description="When?",
        actor=w["owner"],
    )
    client.force_login(w["owner"])
    page = client.get(reverse("requests") + "?mine=1").content.decode()
    assert 'data-opens="new-request"' in page and 'href="?mine=1&amp;new=1#new-request"' in page
    page = client.get(reverse("requests") + "?mine=1&new=1").content.decode()
    assert 'data-opens="new-request"' not in page and "Ask the board a question</a>" not in page
    assert '<details class="panel" id="new-request" open>' in page


def test_arc_header_button_only_while_the_panel_is_closed(client, w):
    client.force_login(w["owner"])
    page = client.get(reverse("arc_list") + "?mine=1&new=1").content.decode()
    assert 'data-opens="new-request"' not in page


def test_voting_new_buttons_open_their_panel_with_no_second_heading(client, w):
    client.force_login(w["board"])
    page = client.get(reverse("voting")).content.decode()
    assert 'data-opens="new-vote"' in page
    # The closed panel is not shown: only the header button offers "New vote".
    assert '<details class="panel board-panel" hidden id="new-vote" data-hide-closed>' in page
    page = client.get(reverse("voting") + "?new=vote").content.decode()
    assert 'data-opens="new-vote"' not in page
    assert '<details class="panel board-panel" open id="new-vote" data-hide-closed>' in page
    assert 'data-opens="new-election"' in page


def test_resolutions_buttons_open_their_panel(client, w):
    client.force_login(w["board"])
    page = client.get(reverse("resolutions")).content.decode()
    assert 'data-opens="record"' in page and 'hidden id="record" data-hide-closed' in page
    page = client.get(reverse("resolutions") + "?new=record").content.decode()
    assert 'data-opens="record"' not in page and 'open id="record" data-hide-closed' in page


def test_opener_script_hides_the_button_while_open():
    assert "[data-opens]" in JS and "button.hidden = panel.open" in JS


# --- Filters switch in place (owner feedback) -----------------------------------------------


@pytest.mark.parametrize(
    "who,url",
    [
        ("owner", "/requests/?mine=1"),
        ("board", "/requests/"),
        ("board", "/voting/"),
        ("board", "/resolutions/"),
        ("board", "/lots/"),
        ("board", "/finance/ledger/"),
        ("board", "/finance/invoices/"),
        ("board", "/board/invitations/"),
    ],
)
def test_filter_navs_swap_the_list_in_place(client, w, who, url):
    client.force_login(w[who])
    plain = client.get(url, follow=True)
    page = plain.content.decode()
    assert "data-swap>" in page or "data-swap " in page
    assert 'data-swap-region id="results"' in page
    # Each filter link falls back to a full load that lands on the list.
    navs = re.findall(r"<nav [^>]*data-swap[^>]*>(.*?)</nav>", page, re.S)
    assert navs and all(
        "#results" in href for nav in navs for href in re.findall(r'href="([^"]+)"', nav)
    )
    # The script asks with X-Requested-With and gets the same list back.
    final = plain.redirect_chain[-1][0] if plain.redirect_chain else url
    swapped = client.get(final, **XHR)
    assert swapped.status_code == 200
    assert 'data-swap-region id="results"' in swapped.content.decode()


def test_swap_script_is_csp_safe_and_keeps_history():
    for part in (
        "nav[data-swap] a[href]",
        "[data-swap-region]",
        "history.pushState",
        "popstate",
        '"X-Requested-With": "XMLHttpRequest"',
        "aria-current",
    ):
        assert part in JS, part


def test_request_filter_swap_shows_only_that_filter(client, w):
    from operations import services as operations

    operations.submit_request(
        lot=w["lot_a"],
        request_type="general",
        subject="Pond question",
        description="When?",
        actor=w["owner"],
    )
    client.force_login(w["owner"])
    page = client.get(reverse("requests") + "?show=closed&mine=1", **XHR).content.decode()
    assert "Pond question" not in page
    assert re.search(r'href="\?show=closed&amp;mine=1#results" aria-current="page"', page)


# --- Lists: find a home, homes with a balance, sign-up status (WT-029, WT-050) --------------


def test_find_a_home_by_number_address_or_owner(client, w):
    client.force_login(w["board"])
    with system_access("test"):
        owner_name = Person.objects.get(user=w["owner"]).display_name
    for q in (str(w["lot_a"].number), f"Lot {w['lot_a'].number}", owner_name.upper()):
        page = client.get(reverse("lot_list"), {"q": q}).content.decode()
        assert w["lot_a"].label in page and w["lot_b"].label not in page, q
    page = client.get(reverse("lot_list"), {"q": "zzz-nothing"}).content.decode()
    assert "No home matches" in page


def test_owner_accounts_with_a_balance_filter(client, w):
    ledger.post_monthly_assessments(year=2026, month=1, actor=SYSTEM)
    lot_c = LotFactory()
    client.force_login(w["board"])
    page = client.get(reverse("ledger_home") + "?balance=1").content.decode()
    assert w["lot_a"].label in page
    # A home with nothing posted has no balance (created after the charge).
    assert f">{lot_c.label}</a>" not in page
    assert f">{lot_c.label}</a>" in client.get(reverse("ledger_home")).content.decode()


def test_invitations_status_pills(client, w):
    """The status pills live on the board's sign-ups tab of Homes and owners (D50)."""
    client.force_login(w["board"])
    page = client.get(reverse("lot_list") + "?view=signups&status=signed_up").content.decode()
    assert 'href="?view=signups&amp;status=needs#results"' in page
    assert re.search(
        r'href="\?view=signups&amp;status=signed_up#results" aria-current="true"', page
    )


def test_owner_cannot_open_lists_through_the_new_filters(client, w):
    client.force_login(w["owner"])
    assert client.get(reverse("lot_list"), {"q": "1"}).status_code in (403, 404)
    assert client.get(reverse("ledger_home") + "?balance=1").status_code in (403, 404)
    assert client.get(reverse("invitations") + "?status=needs").status_code in (403, 404)
    signups = reverse("lot_list") + "?view=signups&status=needs"
    assert client.get(signups).status_code in (403, 404)


# --- Month to charge in words (owner feedback) --------------------------------------------


def _months(page):
    return re.findall(r'<option value="(\d{4}-\d\d)"([^>]*)>([^<]+)</option>', page)


def test_month_to_charge_is_a_list_of_months_in_words(client, w):
    client.force_login(w["board"])
    page = client.get(reverse("ledger_home")).content.decode()
    assert 'type="month"' not in page
    months = _months(page)
    assert len(months) == 7
    today = timezone.localdate().replace(day=1)
    nxt = (today + datetime.timedelta(days=32)).replace(day=1)
    chosen = [m for m in months if "selected" in m[1]]
    assert chosen == [(f"{nxt:%Y-%m}", " selected", f"{nxt:%B %Y}")]


def test_posted_months_are_marked_and_the_default_moves_on(client, w):
    today = timezone.localdate().replace(day=1)
    nxt = (today + datetime.timedelta(days=32)).replace(day=1)
    after = (nxt + datetime.timedelta(days=32)).replace(day=1)
    ledger.post_monthly_assessments(year=nxt.year, month=nxt.month, actor=SYSTEM)
    client.force_login(w["board"])
    months = {
        v: (attrs, label)
        for v, attrs, label in _months(client.get(reverse("ledger_home")).content.decode())
    }
    assert "disabled" in months[f"{nxt:%Y-%m}"][0]
    assert months[f"{nxt:%Y-%m}"][1].endswith("— already posted")
    assert "selected" in months[f"{after:%Y-%m}"][0]


def test_partly_posted_month_stays_open(client, w):
    today = timezone.localdate().replace(day=1)
    ledger.post_monthly_assessments(year=today.year, month=today.month, actor=SYSTEM)
    LotFactory()  # a home added after the month was posted
    client.force_login(w["board"])
    months = {
        v: (attrs, label)
        for v, attrs, label in _months(client.get(reverse("ledger_home")).content.decode())
    }
    attrs, label = months[f"{today:%Y-%m}"]
    assert "disabled" not in attrs and label.endswith("— posted to some homes")


def test_submitted_month_is_accepted_as_before(client, w):
    client.force_login(w["board"])
    today = timezone.localdate()
    client.post(reverse("ledger_home"), {"period": f"{today:%Y-%m}"})
    from ledger.models import LedgerEntry

    with system_access("test"):
        assert LedgerEntry.objects.filter(period=f"{today:%Y-%m}", kind="assessment").exists()


# --- Phone tab bar (WT-041) ----------------------------------------------------------------


def _tabs(client, user):
    client.force_login(user)
    return [t["label"] for t in client.get(reverse("home")).context["nav"]["tabbar"]]


def test_tab_bar_for_owners_and_staff(client, w):
    assert _tabs(client, w["owner"]) == ["Dashboard", "My account", "Voting"]
    assert _tabs(client, w["board"]) == ["Dashboard", "My items", "Search"]
    page = client.get(reverse("home")).content.decode()
    assert '<nav class="tabbar" aria-label="Quick links">' in page
    assert page.count("data-nav-toggle") == 2


def test_tab_bar_only_offers_pages_the_menu_offers(client, w):
    """A view-only contact's tabs are menu entries they already see."""
    client.force_login(w["view_only"])
    response = client.get(reverse("home"))
    menu = {i["url"] for s in response.context["nav"]["sections"] for i in s["items"]}
    for tab in response.context["nav"]["tabbar"]:
        assert tab["url"] in menu or tab["name"] == "search"


# --- Tap targets and overflow at the source (WT-028, WT-034..038) -------------------------


@pytest.mark.parametrize(
    "rule",
    [
        "td > a:first-child:not(.btn)",
        "label:has(> input[type=checkbox])",
        ".card-head > a.small",
        ".list > li > a.title",
        "button.secondary, button.danger {",
        "input[type=file]::file-selector-button",
        ".split > aside .btn",
        ".tabbar {",
        "@media (max-width: 640px) {\n  .subnav { flex-wrap: wrap;",
    ],
)
def test_site_css_fixes_at_the_source(rule):
    assert rule in CSS


def test_file_inputs_and_date_fields_are_styled():
    assert "min-height: 44px; display: block; margin: 0 0 .4rem;" in CSS
    assert "input[type=date], input[type=datetime-local], input[type=time] {" in CSS


def test_committee_forms_have_visible_labels(client, w):
    from governance import services as governance

    committee = governance.create_committee(name="Landscaping", actor=w["board"])
    page = _page(client, w["board"], reverse("committee_detail", args=[committee.pk]))
    for text in (
        "Due date (optional)",
        "Starts",
        "Where (optional)",
        "<summary>Add a task</summary>",
    ):
        assert text in page
    assert 'placeholder="New task"' not in page


def test_collection_list_filters_sit_behind_a_disclosure(client, w):
    page = _page(client, w["board"], reverse("arc_list"))
    assert '<details class="list-filter"><summary>' in page and "Filter and export" in page
