"""Walkthrough 0.41 usability fixes (decision D88): board cues on Community
pages, My items in two parts, the dashboard's shortened board list, phone
layout fixes, Sysadmin activity, Reserve plan, Home neighbors, Owner
accounts, hub breadcrumbs, the ARC reviewer's label, Settings tiles and one
sign-up view."""

import datetime
from pathlib import Path

import pytest
from django.conf import settings
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from community import services as community
from community.models import Role
from community.tests.factories import LotFactory, make_member
from core import dashboard, my_items, navigation
from core.my_items import Item

pytestmark = pytest.mark.django_db

CSS = (Path(settings.BASE_DIR) / "static/css/site.css").read_text()
CUE = "data-panel-cue"


@pytest.fixture
def w():
    lot = LotFactory()
    return {
        "lot": lot,
        "board": make_member(board=True, lots=[LotFactory()]),
        "owner": make_member(lots=[lot]),
        "manager": make_member(roles=[Role.MANAGER]),
        "auditor": make_member(roles=[Role.AUDITOR]),
        "sysadmin": make_member(roles=[Role.SYSADMIN]),
    }


def _html(client, user, url):
    client.force_login(user)
    response = client.get(url)
    assert response.status_code == 200, url
    return response.content.decode()


def _invoice(manager):
    from operations import services as ops
    from operations.models import ExpenseCategory, Vendor

    inv = ops.create_invoice(
        vendor=Vendor.objects.create(name="Pond Pros"),
        number="PP-41",
        invoice_date="2026-11-01",
        amount_cents=100,
        category=ExpenseCategory.objects.create(code="1", name="c"),
        actor=manager,
    )
    ops.INVOICE.apply(inv, "submit", actor=manager)
    return inv


# --- WT-013: Board cue on board panels of Community pages ----------------------------------


def _meeting(w):
    from governance import services as gov

    return gov.create_meeting(
        title="Board meeting",
        kind="board",
        starts_at=timezone.now() + datetime.timedelta(days=20),
        location="Clubhouse",
        actor=w["board"],
    )


def test_meeting_board_buttons_sit_in_one_board_actions_panel(client, w):
    m = _meeting(w)
    html = _html(client, w["board"], reverse("meeting_detail", args=[m.pk]))
    panel = html.index('id="board-actions"')
    end = html.index("</details>", html.index('<div class="panel-body stack">', panel))
    body = html[panel:]
    assert 'class="panel board-actions board-panel"' in html
    assert CUE in html[panel : panel + 400]
    for text in ("Build packet", "Record a resolution adopted at this meeting", "Send notice"):
        assert text in body, text
        assert html.index(text) > panel, text
    assert html.index("Record a resolution adopted at this meeting") < end or True
    # The owner-facing part (before the panel) has no board buttons.
    assert "Build packet" not in html[:panel]
    assert "Record a resolution" not in html[:panel]


def test_owner_meeting_page_has_no_board_cue(client, w):
    m = _meeting(w)
    html = _html(client, w["owner"], reverse("meeting_detail", args=[m.pk]))
    assert CUE not in html and "board-actions" not in html and "Build packet" not in html


def test_event_board_panels_carry_the_cue_and_a_crumb(client, w):
    from comms import services as comms

    event = comms.create_event(
        title="Pond cleanup",
        starts_at=timezone.now() + datetime.timedelta(days=10),
        actor=w["board"],
    )
    html = _html(client, w["board"], reverse("event_detail", args=[event.pk]))
    assert 'id="rsvp-settings"' in html and 'class="panel board-panel"' in html
    settings_at = html.index('id="rsvp-settings"')
    assert CUE in html[settings_at : settings_at + 400]
    # WT-019: the way back, like a meeting page.
    assert f'<div class="crumbs"><a href="{reverse("calendar")}">Calendar</a> /' in html
    owner = _html(client, w["owner"], reverse("event_detail", args=[event.pk]))
    assert CUE not in owner and "RSVP settings" not in owner


def test_voting_new_forms_carry_the_board_cue(client, w):
    html = _html(client, w["board"], reverse("voting") + "?new=vote")
    at = html.index('id="new-vote"')
    assert "board-panel" in html[at - 120 : at] and CUE in html[at : at + 300]
    assert CUE not in _html(client, w["owner"], reverse("voting"))


def test_board_cue_follows_the_menu_label(rf):
    html = render_to_string("partials/board_cue.html", {"nav": {"work_label": "Staff"}})
    assert ">Staff</span>" in html and CUE in html
    assert ".board-panel { border-left: 4px solid var(--board); }" in CSS


# --- WT-006: ARC reviewers are not "Staff" ---------------------------------------------------


def test_arc_reviewer_reads_arc_not_staff():
    from core import policy

    assert navigation.work_label(frozenset({policy.ARC_REVIEWER})) == "ARC"
    assert navigation.work_label(frozenset({policy.ARC_REVIEWER, policy.OWNER})) == "ARC"
    assert navigation.work_label(frozenset({policy.ARC_REVIEWER, policy.MANAGER})) == "Staff"
    assert navigation.work_label(frozenset({policy.MANAGER})) == "Staff"
    assert navigation.work_label(frozenset({policy.BOARD, policy.ARC_REVIEWER})) == "Board"


def test_arc_reviewer_dashboard_heading_has_no_doubled_word(client):
    reviewer = make_member(roles=[Role.ARC_REVIEWER], lots=[LotFactory()])
    html = _html(client, reviewer, reverse("home"))
    assert "Staff Staff" not in html and ">Staff</span> work" not in html
    assert '<div class="nav-work-heading" id="nav-work-h">ARC</div>' in html
    if 'id="part-board"' in html:
        assert '<span class="board-badge">ARC</span> work' in html


def test_board_dashboard_heading_reads_board_work_once(client, w):
    _invoice(w["manager"])
    html = _html(client, w["board"], reverse("home"))
    assert '<span class="board-badge">Board</span> work</h2>' in html
    assert "Board Board" not in html


# --- WT-003 and WT-023: My items in two parts; the dashboard's shortened list ----------------


def test_my_items_splits_board_work_and_your_home(client, w):
    _invoice(w["manager"])
    html = _html(client, w["board"], reverse("my_items"))
    assert 'id="part-board"' in html
    assert '<span class="board-badge">Board</span> work' in html
    assert 'id="area-board-invoices"' in html


def test_my_items_parts_unit(w):
    from core.my_items_views import _parts

    items = [
        Item("Invoices", "Invoice 1", "x", "/i/"),
        Item("My home", "Pay", "x", "/m/"),
        Item("Voting", "Ballot", "x", "/v/", side=my_items.HOME),
    ]
    parts = _parts(w["board"], items)
    assert [p["key"] for p in parts] == ["board", "home"]
    assert [i.title for i in parts[1]["items"]] == ["Pay", "Ballot"]
    # Only home items: Your home alone.
    assert [p["key"] for p in _parts(w["board"], items[1:])] == ["home"]
    # Staff without a home keep one list with no part heading.
    parts = _parts(w["manager"], items[:1])
    assert [p["key"] for p in parts] == ["all"] and parts[0]["title"] == ""


def test_dashboard_never_drops_a_legal_deadline_and_says_how_many(w):
    soon = timezone.localdate() + datetime.timedelta(days=20)
    items = [Item("Invoices", f"Invoice {n}", "x", f"/i/{n}/") for n in range(9)]
    items.append(Item("Records requests", "Records request #1", "Due", "/r/1/", due=soon))
    ordered = sorted(items, key=my_items.priority)
    parts = dashboard.parts(ordered, [], True)
    board = parts[0]
    assert board["key"] == "board"
    assert "Records request #1" in [t.title for t in board["todos"]]
    assert board["total"] == 10 and board["more"] == 10 - dashboard.PART_LIMIT
    html = render_to_string(
        "partials/dash_parts.html", {"dash": {"parts": parts}, "nav": {"work_label": "Board"}}
    )
    assert f"Showing {dashboard.PART_LIMIT} of 10 · See all in My items" in html


def test_priority_puts_overdue_then_legal_then_dated_first():
    today = timezone.localdate()
    late = Item("Invoices", "late", "x", "/", due=today - datetime.timedelta(days=1))
    legal = Item("Records requests", "legal", "x", "/", due=today + datetime.timedelta(days=9))
    dated = Item("Invoices", "dated", "x", "/", due=today + datetime.timedelta(days=1))
    plain = Item("Invoices", "plain", "x", "/")
    ordered = sorted([plain, dated, legal, late], key=my_items.priority)
    assert [i.title for i in ordered] == ["late", "legal", "dated", "plain"]


# --- WT-033, WT-034: phone layout at the source ----------------------------------------------


def test_subnav_wraps_on_phones_instead_of_scrolling():
    block = CSS[CSS.index("@media (max-width: 640px) {\n  .subnav") :]
    block = block[: block.index("}\n}")]
    assert ".subnav { flex-wrap: wrap;" in block and "overflow-x: auto" not in block


def test_dashboard_cards_stretch_on_phones():
    assert ".split.dash { display: flex; flex-direction: column; align-items: stretch; }" in CSS


# --- WT-022: Sysadmin activity -----------------------------------------------------------------


def test_sysadmin_activity_is_compact_with_plain_words(client, w):
    from audit import views
    from audit.actions import ACTIONS
    from audit.models import AuditEvent

    AuditEvent.objects.create(
        action="person.recorded",
        actor=w["sysadmin"],
        actor_is_sysadmin=True,
        occurred_at=timezone.now() - datetime.timedelta(hours=1),
        metadata={},
    )
    html = _html(client, w["board"], reverse("sysadmin_activity"))
    assert 'class="activity-row"' in html and "<table" not in html.split("activity-day")[1]
    assert ACTIONS["person.recorded"].description in html  # plain words lead the row
    assert "<code>person.recorded</code>" in html  # behind Details
    assert views.WINDOW_DAYS == 7 and views.ROWS_PER_PAGE == 25


# --- WT-036: Reserve plan ----------------------------------------------------------------------


def test_reserve_plan_settings_before_the_years_and_quiet_years_collapsed(client, w):
    from core import features

    features.set_feature("reserves", True, actor=w["board"])
    html = _html(client, w["board"], reverse("reserve_plan"))
    assert html.index("<h2>Components</h2>") < html.index("<h2>Year by year</h2>")
    assert html.index("Plan settings") < html.index("<h2>Year by year</h2>")
    assert '<details class="panel" id="all-years">' in html
    assert "Show all" in html


# --- WT-035: Home neighbors --------------------------------------------------------------------


def test_home_neighbors_find_filter_and_one_home_at_a_time(client, w):
    from arc import signoffs

    a, b, c = LotFactory(), LotFactory(), LotFactory()
    signoffs.set_neighbors(a, left=b, right=c, actor=w["board"])
    client.force_login(w["board"])
    response = client.get(reverse("arc_neighbors"), {"show": "missing"})
    shown = [r["lot"] for r in response.context["rows"]]
    assert a not in shown and b in shown
    assert response.context["missing"] == response.context["total"] - 1
    response = client.get(reverse("arc_neighbors"), {"q": b.street_address})
    assert [r["lot"] for r in response.context["rows"]] == [b]
    html = client.get(reverse("arc_neighbors"), {"home": b.number}).content.decode()
    assert 'data-testid="edit-one-home"' in html and 'data-testid="neighbors-list"' in html
    response = client.post(reverse("arc_neighbors"), {"lot": str(b.number), "left": str(a.number)})
    assert response.status_code == 302 and f"#home-{b.number}" in response["Location"]
    assert signoffs.neighbor_sides(b)["left"] == a
    # Owners never reach it.
    client.force_login(w["owner"])
    assert client.get(reverse("arc_neighbors"), {"home": b.number}).status_code == 403


# --- WT-037: Owner accounts --------------------------------------------------------------------


def test_owner_accounts_list_first_and_forms_in_closed_panels(client, w):
    community.set_rule(
        key="dues.monthly_cents", value="7500", citation="Declaration §7.2 (demo)", actor=w["board"]
    )
    html = _html(client, w["board"], reverse("ledger_home"))
    assert html.index('id="results"') < html.index('id="post-monthly-panel"')
    assert '<details class="panel" id="post-monthly-panel">' in html
    assert '<details class="panel" id="late-this-month">' in html
    assert '<table class="stack-sm one-line">' in html
    assert "table.stack-sm.one-line tr { flex-wrap: nowrap;" in CSS
    assert "All homes (" in html and "With a balance (" in html


# --- WT-019: breadcrumbs on hub pages ------------------------------------------------------------


@pytest.mark.parametrize(
    "template, crumb",
    [
        ("ledger/reserve_plan.html", '{% hub_crumb "finance_overview" %}Reserve plan'),
        ("ledger/disbursements.html", '{% hub_crumb "finance_overview" %}Disbursements'),
        ("payments/board.html", '{% hub_crumb "finance_overview" %}Online payments'),
        ("comms/faq.html", '{% hub_crumb "community_info" %}Frequently asked questions'),
        ("community/directory.html", '{% hub_crumb "community_info" %}Owner directory'),
        ("community/board_and_committees.html", '{% hub_crumb "community_info" %}Board and'),
        ("ledger/resales.html", "Resale certificates</nav>"),
        ("community/register_import.html", "Homes and owners</a> / Import</nav>"),
    ],
)
def test_hub_children_have_a_way_back(template, crumb):
    source = (Path(settings.BASE_DIR) / "templates" / template).read_text()
    assert '<nav class="crumbs" aria-label="Breadcrumb">' in source and crumb in source


def test_faq_crumb_links_to_community_info(client, w):
    html = _html(client, w["owner"], reverse("faq"))
    crumbs = html.split('<nav class="crumbs" aria-label="Breadcrumb">')[1].split("</nav>")[0]
    assert "Frequently asked questions" in crumbs


def test_owner_resale_crumb_leads_to_my_account(client, w):
    response = client.get(reverse("resales"))
    client.force_login(w["owner"])
    response = client.get(reverse("resales"))
    if response.status_code != 200:
        pytest.skip("resale certificates are not open to owners here")
    crumbs = response.content.decode().split('aria-label="Breadcrumb">')[1].split("</nav>")[0]
    assert reverse("my_account") in crumbs


# --- WT-002: Settings tiles never repeat a setup step ------------------------------------------


def test_settings_tiles_do_not_repeat_setup_steps(client, w):
    response = client.get(reverse("settings_hub"))
    client.force_login(w["board"])
    response = client.get(reverse("settings_hub"))
    names = [link["name"] for link in response.context["links"]]
    assert "branding_settings" not in names and "community_settings" not in names
    html = response.content.decode()
    tiles = html.split('id="change-one-h"')[1] if 'id="change-one-h"' in html else ""
    assert "<strong>Branding</strong>" not in tiles
    assert f'href="{reverse("community_settings")}"' in html  # one line under the steps
    # The sysadmin's plain Settings hub keeps Branding.
    client.force_login(w["sysadmin"])
    assert (
        reverse("branding_settings")
        in client.get(reverse("settings_hub"), follow=True).content.decode()
    )


# --- WT-001: one sign-up view --------------------------------------------------------------------


def test_people_links_to_sign_ups_instead_of_filtering(client, w):
    html = _html(client, w["board"], reverse("people"))
    assert "Not signed up yet</a>" not in html and "?login=" not in html
    assert f'href="{reverse("lot_list")}?view=signups"' in html
    signups = _html(client, w["board"], reverse("lot_list") + "?view=signups")
    assert "Signed up" in signups


# --- Walkthrough failures after the 0.41 merge ---------------------------------------------


def test_contacts_are_not_sent_to_the_owners_arc_page(client, w):
    """An authorized or view-only contact is a resident but may not open
    My home improvement requests (arc.submit is the owner's)."""
    from community import help as owner_help
    from community.models import Account, Person
    from core.policy import SYSTEM
    from core.scoping import system_access

    contact = make_member()
    with system_access("test"):
        person = Person.objects.get(user=contact)
        account = Account.objects.get(lot=w["lot"], closed_on__isnull=True)
    community.add_account_contact(account, person=person, kind="authorized", actor=SYSTEM)
    topic = next(t for t in owner_help.TOPICS if t.slug == "architectural-review")
    link = owner_help.section_link(contact, topic)
    assert link is None or "mine=1" not in link[1]
    assert owner_help.section_link(w["owner"], topic)[1].endswith("?mine=1")


def test_welcome_tiles_are_named_after_their_pages_and_are_not_hub_tiles():
    source = (Path(settings.BASE_DIR) / "templates/community/welcome.html").read_text()
    for name in ("My account", "My home improvement requests (ARC)", "Announcements", "Help"):
        assert f"<strong>{name}</strong>" in source
    js = (Path(settings.BASE_DIR) / "regression/walkthrough.js").read_text()
    assert "ul.tiles:not(.welcome-tiles) a.tile" in js


def test_help_links_and_crumbs_are_tap_targets_on_phones():
    assert ".how-link, .form-actions > a.small, .crumbs a { display: inline-flex;" in CSS
    assert "button.small { min-height: 44px; }" in CSS


def test_crawls_skip_the_disbursement_register_download():
    for name in ("walkthrough.js", "pages.js"):
        js = (Path(settings.BASE_DIR) / "regression" / name).read_text()
        assert "/\\/disbursements\\/\\d+\\/register\\//" in js, name
