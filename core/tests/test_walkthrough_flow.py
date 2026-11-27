"""Walkthrough fixes, builder C (D50): flow, naming, merges and navigation."""

import csv
import datetime
import io

import pytest
from django.urls import reverse
from django.utils import timezone

from community.models import Role
from community.tests.factories import LotFactory, make_member
from core import my_items, navigation

pytestmark = pytest.mark.django_db


@pytest.fixture
def w():
    lot = LotFactory()
    return {
        "lot": lot,
        "board": make_member(board=True),
        "board2": make_member(board=True),
        "owner": make_member(lots=[lot]),
        "manager": make_member(roles=[Role.MANAGER]),
        "auditor": make_member(roles=[Role.AUDITOR]),
        "sysadmin": make_member(roles=[Role.SYSADMIN]),
    }


def _invoice(manager, number="PP-102", vendor="Pond Pros"):
    from operations import services as ops
    from operations.models import ExpenseCategory, Vendor

    inv = ops.create_invoice(
        vendor=Vendor.objects.create(name=vendor),
        number=number,
        invoice_date="2026-11-01",
        amount_cents=100,
        category=ExpenseCategory.objects.create(code="1", name="c"),
        actor=manager,
    )
    ops.INVOICE.apply(inv, "submit", actor=manager)
    return inv


# --- WT-004, WT-005, WT-006: names ------------------------------------------------------------


def test_owner_arc_area_has_one_name(client, w):
    client.force_login(w["owner"])
    html = client.get(reverse("my_lot")).content.decode()
    assert "My home improvement requests (ARC)" in html
    assert "Home improvement approvals" not in html


def test_record_numbers_have_no_letter_prefixes(w):
    from operations import services as ops

    wo = ops.create_work_order(title="Clear north pond outlet", actor=w["board"])
    assert str(wo) == f"Work order #{wo.pk} · Clear north pond outlet"
    _invoice(w["manager"])
    titles = [i.title for i in my_items.items_for(w["board"])]
    assert "Invoice PP-102 · Pond Pros" in titles
    assert not any("#PP-102" in t for t in titles)


def test_my_items_groups_are_named_after_their_menu_entries(w):
    _invoice(w["manager"])
    areas = {i.title: i.area for i in my_items.items_for(w["board"])}
    assert areas["Invoice PP-102 · Pond Pros"] == "Invoices"
    assert areas["Quarterly access review"] == "People"
    assert "Approvals" not in my_items.AREAS and "Compliance" not in my_items.AREAS
    for area in ("Invoices", "People", "Voting", "Second approvals", "Recurring deadlines"):
        assert area in my_items.AREAS and area in my_items.AREA_NAV
    assert my_items.Item("Voting", "Survey: x", "d", "/").nav == "voting"


# --- WT-007: settings and report wording ------------------------------------------------------


def test_percent_typed_and_stored_as_basis_points():
    from community.rules import bp_display, percent_to_bp

    assert percent_to_bp("3") == 300
    assert percent_to_bp("2.5") == 250
    assert percent_to_bp("2.75%") == 275
    assert percent_to_bp("0") == 0
    for bad in ("", "abc", "1.234", "-1", "3,5"):
        with pytest.raises(ValueError):
            percent_to_bp(bad)
    assert bp_display(300) == "3" and bp_display(250) == "2.5" and bp_display(275) == "2.75"


def test_surcharge_saved_as_percent_keeps_the_3_percent_cap(client, w):
    from community.models import GoverningRule
    from community.rules import rule

    key = "payments.credit_card_surcharge_bp"
    client.force_login(w["board"])
    response = client.post(
        reverse("community_rule_save"),
        {"key": key, "value": "2.5", "citation": "Board resolution 2027-09"},
    )
    assert response.status_code == 302
    assert GoverningRule.objects.get(key=key).value == "250" and rule(key) == 250
    client.post(reverse("community_rule_save"), {"key": key, "value": "3.5", "citation": "x"})
    assert GoverningRule.objects.get(key=key).value == "250"  # refused: over 3%
    html = client.get(reverse("community_settings")).content.decode()
    assert "Credit card surcharge (%)" in html and 'value="2.5"' in html
    assert "hundredths of a percent" not in html
    assert "Accept neighbor acknowledgements read from scanned forms" in html


def test_report_names_are_plain():
    from reports.registry import REPORTS

    titles = {r.title for r in REPORTS.values()}
    assert {"Unpaid balances by age", "Paid invoices by vendor", "Budget lines"} <= titles
    assert "Budget vs actual by month" in titles
    assert not titles & {"AR aging", "Budget", "Paid invoices by service provider"}


# --- WT-012, WT-014: crumbs and long settings pages -------------------------------------------


@pytest.mark.parametrize(
    ("name", "parent"),
    [
        ("community_settings", "settings_hub"),
        ("arc_project_types", "settings_hub"),
        ("arc_neighbors", "settings_hub"),
        ("retention", "settings_hub"),
        ("branding_settings", "settings_hub"),
        ("letters_settings", "settings_hub"),
        ("access_review", "people"),
    ],
)
def test_hub_children_have_a_way_back(client, w, name, parent):
    client.force_login(w["board"])
    html = client.get(reverse(name)).content.decode()
    assert f'<nav class="crumbs" aria-label="Breadcrumb"><a href="{reverse(parent)}">' in html


def test_neighbor_acknowledgements_crumb_leads_to_my_arc_requests(client, w):
    client.force_login(w["owner"])
    html = client.get(reverse("signoffs_mine")).content.decode()
    assert f'href="{reverse("arc_list")}?mine=1">My home improvement requests (ARC)</a>' in html


def test_community_settings_has_an_index_and_folding_sections(client, w):
    client.force_login(w["board"])
    html = client.get(reverse("community_settings")).content.decode()
    assert 'data-testid="settings-index"' in html and 'href="#sec-association"' in html
    assert '<details class="card rules collapsible" id="sec-1" open>' in html  # needs attention
    assert 'id="sec-areas"' in html


def test_arc_types_fold_to_a_line_with_the_citation(client, w):
    from arc.models import ArcProjectType

    ArcProjectType.objects.create(name="Pergola test", citation="Declaration §9.2")
    ArcProjectType.objects.create(name="Gazebo test", citation="")
    client.force_login(w["board"])
    html = client.get(reverse("arc_project_types")).content.decode()
    assert html.count('<details class="card collapsible"') == ArcProjectType.objects.count()
    assert "Declaration §9.2" in html and "missing citation" in html


# --- WT-013: Sysadmin activity -----------------------------------------------------------------


def _event(action, days_ago, actor=None):
    from audit.models import AuditEvent

    return AuditEvent.objects.create(
        action=action,
        actor=actor,
        actor_is_sysadmin=True,
        occurred_at=timezone.now() - datetime.timedelta(days=days_ago),
        metadata={},
    )


def test_sysadmin_activity_shows_30_days_by_day_with_older(client, w):
    _event("system.update_requested", 1, w["sysadmin"])
    _event("backup.files_exported", 45, w["sysadmin"])
    client.force_login(w["board"])
    response = client.get(reverse("sysadmin_activity"))
    html = response.content.decode()
    assert "<code>system.update_requested</code>" in html
    assert "<code>backup.files_exported</code>" not in html
    assert 'data-testid="activity-window"' in html and "Older" in html
    older = client.get(reverse("sysadmin_activity") + response.context["older_url"])
    assert "<code>backup.files_exported</code>" in older.content.decode()
    assert older.context["newer_url"]


def test_sysadmin_activity_filters_by_person_and_action(client, w):
    other = make_member(roles=[Role.SYSADMIN])
    _event("system.update_requested", 1, w["sysadmin"])
    _event("system.task_requested", 2, other)
    client.force_login(w["auditor"])
    url = reverse("sysadmin_activity")
    html = client.get(url, {"who": other.pk}).content.decode()
    assert "system.task_requested" in html and "<code>system.update_requested</code>" not in html
    html = client.get(url, {"action": "system.update_requested"}).content.decode()
    assert "<code>system.task_requested</code>" not in html


def test_sysadmin_activity_csv_is_audited_and_board_or_auditor_only(client, w):
    from audit.models import AuditEvent

    _event("system.update_requested", 1, w["sysadmin"])
    client.force_login(w["auditor"])
    response = client.get(reverse("sysadmin_activity"), {"export": "csv"})
    assert response["Content-Type"].startswith("text/csv")
    rows = list(csv.reader(io.StringIO(response.content.decode())))
    assert rows[0] == ["When", "Who", "Channel", "What", "Details"]
    assert any("system.update_requested" in r for r in rows[1:])
    assert AuditEvent.objects.filter(
        action="list.exported", metadata__list="sysadmin_activity"
    ).exists()
    for who in ("owner", "manager", "sysadmin"):
        client.force_login(w[who])
        assert client.get(reverse("sysadmin_activity"), {"export": "csv"}).status_code == 403


# --- WT-015: the reviewer's position ----------------------------------------------------------


@pytest.fixture
def in_review(w):
    from arc import services as arc

    req = arc.create_request(lot=w["lot"], title="Fence", description="Rear", actor=w["owner"])
    arc.ARC.apply(req, "submit", actor=w["owner"])
    arc.ARC.apply(req, "accept", actor=w["board"])
    req.refresh_from_db()
    return req


def test_position_starts_empty_and_is_required(client, w, in_review):
    from arc.models import ReviewerVote

    reviewer = make_member(roles=[Role.ARC_REVIEWER])
    client.force_login(reviewer)
    html = client.get(reverse("arc_detail", args=[in_review.pk])).content.decode()
    assert '<select id="pos" name="position" required><option value="" selected>' in html
    client.post(reverse("arc_position", args=[in_review.pk]), {"position": ""})
    assert not ReviewerVote.objects.filter(request=in_review).exists()


def test_steps_a_reviewer_can_never_take_are_hidden(w, in_review):
    from arc import services as arc
    from core.workflow import NotForYou

    reviewer = make_member(roles=[Role.ARC_REVIEWER])
    reasons = [reason for _t, reason in arc.ARC.blocked(in_review, reviewer)]
    assert not any(isinstance(r, NotForYou) for r in reasons)
    assert "Only an owner of this home can do this." not in reasons
    # Still refused if tried.
    withdraw = arc.ARC.transitions.get("withdraw")
    if withdraw and in_review.state in withdraw.sources:
        assert arc.ARC._check(in_review, withdraw, reviewer)


# --- WT-016: dashboards for roles with nothing assigned ---------------------------------------


def test_auditor_and_sysadmin_get_shortcuts_instead_of_an_empty_card(client, w):
    client.force_login(w["auditor"])
    html = client.get(reverse("home")).content.decode()
    assert 'id="shortcuts"' in html and "You're all caught up" not in html
    for name in ("reports", "sysadmin_activity", "community_settings"):
        assert f'href="{reverse(name)}"' in html
    client.force_login(w["sysadmin"])
    html = client.get(reverse("home")).content.decode()
    assert 'id="shortcuts"' in html
    assert f'href="{reverse("server_tasks")}"' in html
    assert reverse("community_settings") not in html  # only what they may open
    client.force_login(w["board"])
    html = client.get(reverse("home")).content.decode()
    assert 'id="todos"' in html and 'id="shortcuts"' not in html


def test_quick_actions_sit_right_under_the_to_dos(client, w):
    client.force_login(w["owner"])
    html = client.get(reverse("home")).content.decode()
    assert html.index('id="todos"') < html.index('id="quick-actions"') < html.index("Latest news")
    assert "?mine=1&amp;new=1#new-request" in html


# --- WT-017: Documents ------------------------------------------------------------------------


def test_documents_records_link_opens_the_form_and_packets_are_grouped(client, w):
    from documents.models import Document, Folder

    folder = Folder.objects.create(name="Meeting packets", visibility="owners")
    Document.objects.create(
        title="Owner packet: Board meeting",
        category="other",
        folder=folder,
        created_by=w["board"],
    )
    client.force_login(w["board"])  # sees documents without a published version
    html = client.get(reverse("documents")).content.decode()
    assert "<h2>Meeting packets</h2>" in html and "<h2>Other</h2>" not in html
    client.force_login(w["owner"])
    html = client.get(reverse("documents")).content.decode()
    assert f"{reverse('records_requests')}?mine=1&amp;new=1#new-request" in html
    page = client.get(reverse("records_requests"), {"mine": "1", "new": "1"}).content.decode()
    assert '<details class="panel" id="new-request" open>' in page


# --- WT-040: the phone menu opens on the person's work ----------------------------------------


def test_staff_menu_opens_work_with_waiting_items(rf, w):
    _invoice(w["manager"])

    def sections(user):
        request = rf.get("/")
        request.user = user
        return {s["title"]: s for s in navigation.build(request)["sections"]}

    board = sections(w["board"])
    # WT-036: only the work section with the most waiting opens (a short
    # phone drawer); the others show their count on the closed heading.
    work = [s for s in board.values() if s["work"]]
    opened = [s for s in work if s["open"]]
    assert board["Finance"]["count"]
    assert len(opened) == 1 and opened[0]["count"] == max(s["count"] for s in work)
    assert not board["My home"]["open"] if "My home" in board else True
    assert not board["Community"]["open"]
    owner = sections(w["owner"])
    assert owner["My home"]["open"] and owner["Community"]["open"]
    sysadmin = sections(w["sysadmin"])
    assert sysadmin["Admin"]["open"]  # nothing waiting: the first work section opens


def test_phone_drawer_puts_sign_out_at_the_top():
    from pathlib import Path

    css = (Path(__file__).resolve().parents[2] / "static/css/site.css").read_text()
    assert ".sidebar .sidebar-foot { order: -1;" in css


# --- Accounting setup (owner request 2026-11-26) -----------------------------------------------


def test_accounting_shows_a_summary_and_setup_has_its_own_tab(client, w):
    client.force_login(w["board"])
    html = client.get(reverse("accounting")).content.decode()
    assert 'data-testid="setup-summary"' in html and "Set up accounting" in html
    assert "Account for each kind of entry" not in html
    page = client.get(reverse("accounting"), {"tab": "setup"}).content.decode()
    for heading in (
        "Money coming in",
        "Money owed to the association",
        "Bank and card payments",
        "Money paid back to owners",  # D52 surplus fund
    ):
        assert f"<h2>{heading}</h2>" in page
    assert 'placeholder="1000 Operating cash"' in page
    assert "Leave empty to use the standard account (4000 Assessment income)" in page
    assert "<h1>Accounting</h1>" in page


def test_accounting_setup_needs_balance_and_date_together(client, w):
    from ledger.models import AccountingSetup

    client.force_login(w["board"])
    url = reverse("accounting")
    response = client.post(url, {"section": "setup", "opening_cash": "1250.00"})
    assert response.status_code == 200
    assert "Enter the date of the opening balance." in response.content.decode()
    assert 'value="1250.00"' in response.content.decode()  # shown again as typed
    response = client.post(url, {"section": "setup", "opening_cash_on": "2026-01-01"})
    assert "Enter the opening balance for that date." in response.content.decode()
    assert AccountingSetup.get().opening_cash_on is None


def test_accounting_setup_round_trips_unchanged(client, w):
    from ledger.models import AccountingSetup

    client.force_login(w["board"])
    url = reverse("accounting")
    data = {
        "section": "setup",
        "gl_cash": "1010 Main checking",
        "opening_cash": "1250.00",
        "opening_cash_on": "2026-01-01",
    }
    response = client.post(url, data)
    assert response.status_code == 302 and response["Location"] == url + "?tab=setup"
    before = AccountingSetup.get().accounts()
    assert before["cash"] == "1010 Main checking"
    assert before["assessment"] == "4000 Assessment income"
    page = client.get(url, {"tab": "setup"}).content.decode()
    assert 'value="1010 Main checking"' in page and 'value="1250.00"' in page
    # Saving the form as shown changes nothing.
    client.post(url, {**data})
    setup = AccountingSetup.get()
    assert setup.accounts() == before
    assert setup.opening_cash_cents == 125000
    assert setup.opening_cash_on == datetime.date(2026, 1, 1)
    summary = client.get(url).content.decode()
    # 14 with special assessment income (D75), interest and collection costs (D80, D82).
    assert "Accounting set up: 14 accounts mapped (1 changed from the standard)" in summary
    assert "$1,250.00" in summary


def test_accounting_setup_is_for_those_who_manage_it(client, w):
    client.force_login(w["auditor"])
    assert client.get(reverse("accounting"), {"tab": "setup"}).status_code == 403
    assert client.post(reverse("accounting"), {"section": "setup"}).status_code == 403
    html = client.get(reverse("accounting")).content.decode()
    assert 'data-testid="setup-summary"' not in html
    client.force_login(w["owner"])
    assert client.get(reverse("accounting"), {"tab": "setup"}).status_code == 403
