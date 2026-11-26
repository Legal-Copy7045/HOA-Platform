"""Milestone 3 phase 6: board roster, list tools (filters, paging, export),
FAQ and the welcome message."""

import datetime
import re

import pytest
from django.urls import reverse
from django.utils import timezone

from audit.models import AuditEvent
from comms import services as comms
from community import services
from community.models import Role
from community.tests.factories import LotFactory, days_ago, make_member
from core.lists import safe_cell
from core.policy import SYSTEM

pytestmark = pytest.mark.django_db


@pytest.fixture
def w():
    lot_a, lot_b = LotFactory(), LotFactory()
    return {
        "board": make_member(board=True),
        "auditor": make_member(roles=[Role.AUDITOR]),
        "owner": make_member(lots=[lot_a]),
        "owner_b": make_member(lots=[lot_b]),
        "lot_a": lot_a,
        "lot_b": lot_b,
    }


# --- Roster ------------------------------------------------------------------------------


ROSTER = {"view": "roster"}


def test_roster_lists_accounts_for_the_board_and_auditor(client, w):
    client.force_login(w["auditor"])
    page = client.get(reverse("lot_list"), ROSTER).content
    assert f"Lot {w['lot_a'].number}".encode() in page
    assert b"<h1>Lots and owners</h1>" in page and b"Export CSV" in page
    client.force_login(w["owner"])
    assert client.get(reverse("lot_list"), ROSTER).status_code == 403
    assert client.get(reverse("roster")).status_code == 403


def test_old_roster_url_redirects_to_lots_and_owners(client, w):
    client.force_login(w["board"])
    response = client.get(reverse("roster"))
    assert response.status_code == 302
    assert response["Location"] == reverse("lot_list") + "?view=roster"
    response = client.get(reverse("roster"), {"export": "csv", "previous": "1"})
    assert response["Location"].startswith(reverse("lot_list") + "?")
    for part in ("view=roster", "export=csv", "previous=1"):
        assert part in response["Location"]


def test_lots_and_owners_default_view_has_no_contact_details(client, w):
    from community.models import Person
    from core.scoping import system_access

    with system_access("t"):
        Person.objects.filter(user=w["owner"]).update(email="owner-a@example.com")
    client.force_login(w["board"])
    page = client.get(reverse("lot_list")).content
    assert b"<h1>Lots and owners</h1>" in page and b"owner-a@example.com" not in page
    assert b"Accounts, contacts and balances" in page
    roster = client.get(reverse("lot_list"), ROSTER).content
    assert b"owner-a@example.com" in roster


def test_roster_export_is_audited_and_formula_safe(client, w):
    from community.models import Person
    from core.scoping import system_access

    with system_access("t"):
        Person.objects.filter(user=w["owner"]).update(display_name="=HYPERLINK(1)")
    client.force_login(w["board"])
    response = client.get(reverse("lot_list"), {**ROSTER, "export": "csv"})
    body = response.content.decode()
    assert response["Content-Type"].startswith("text/csv")
    assert "'=HYPERLINK(1)" in body
    assert AuditEvent.objects.filter(action="list.exported").exists()


def test_previous_accounts_only_on_request(client, w):
    buyer = services.record_person(display_name="Buyer", actor=SYSTEM)
    services.transfer_lot(w["lot_a"], new_owners=[buyer], transfer_date=days_ago(1), actor=SYSTEM)
    client.force_login(w["board"])
    current = client.get(reverse("lot_list"), ROSTER).content
    assert b"Dormant" not in current
    previous = client.get(reverse("lot_list"), {**ROSTER, "previous": "1"}).content
    assert b"Dormant" in previous


def test_safe_cell():
    assert safe_cell("=SUM(A1)") == "'=SUM(A1)"
    assert safe_cell("-12.50") == "-12.50"
    assert safe_cell(chr(0xFF1D) + "1") == "'" + chr(0xFF1D) + "1"
    assert safe_cell("Plain") == "Plain"


# --- List tools ------------------------------------------------------------------------------


def test_work_orders_filter_page_and_export(client, w):
    from operations import services as ops

    for i in range(30):
        ops.create_work_order(title=f"Job {i:02d}", actor=w["board"])
    done = ops.create_work_order(title="Old cancelled job", actor=w["board"])
    ops.WORK_ORDER.apply(done, "cancel", actor=w["board"], note="no longer needed")
    client.force_login(w["board"])
    first = client.get(reverse("work_orders")).content
    assert b"Page 1 of 2" in first
    assert b"Old cancelled job" not in first  # open only by default
    closed = client.get(reverse("work_orders"), {"show": "closed"}).content
    assert b"Old cancelled job" in closed
    csv = client.get(reverse("work_orders"), {"show": "all", "export": "csv"}).content.decode()
    assert csv.count("\n") == 32  # header + 31 rows


def test_date_range_filter(client, w):
    from compliance import services as compliance

    compliance.report(
        lot=w["lot_a"],
        category="trash",
        description="Old",
        observed_on=days_ago(40),
        actor=w["board"],
    )
    compliance.report(lot=w["lot_a"], category="trash", description="New", actor=w["board"])
    client.force_login(w["board"])
    since = (timezone.localdate() - datetime.timedelta(days=7)).isoformat()
    page = client.get(reverse("violations"), {"from": since, "show": "all"}).content.decode()
    assert len(re.findall(r'href="/violations/\d+/"', page)) == 1


def test_owners_cannot_export_board_lists(client, w):
    client.force_login(w["owner"])
    response = client.get(reverse("arc_list"), {"export": "csv"})
    assert not response["Content-Type"].startswith("text/csv")
    response = client.get(reverse("violations"), {"export": "csv"})
    assert not response["Content-Type"].startswith("text/csv")


# --- FAQ and welcome ---------------------------------------------------------------------------


def test_faq_board_writes_owners_read_published_only(client, w):
    client.force_login(w["board"])
    client.post(
        reverse("faq"),
        {
            "question": "When is trash day?",
            "answer": "Tuesday",
            "category": "Trash",
            "published": "on",
        },
    )
    hidden = comms.save_faq(
        question="Draft question", answer="Not yet", published=False, actor=w["board"]
    )
    client.force_login(w["owner"])
    page = client.get(reverse("faq")).content
    assert b"When is trash day?" in page and hidden.question.encode() not in page
    assert client.post(reverse("faq"), {"question": "x", "answer": "y"}).status_code == 403
    assert b"Tuesday" in client.get(reverse("faq"), {"q": "trash"}).content


def test_welcome_message_on_everyones_dashboard(client, w):
    services.update_profile(
        legal_name="Example HOA",
        fiscal_year_start_month=1,
        welcome_message="Welcome to the portal!",
        actor=w["board"],
    )
    client.force_login(w["owner"])
    assert b"Welcome to the portal!" in client.get(reverse("home")).content
