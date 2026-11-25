import csv
import io

import pytest
from django.urls import reverse

from community.models import Role
from community.tests.factories import LotFactory, make_member
from ledger import services as ledger
from reports.registry import REPORTS
from reports.views import _safe_cell

pytestmark = pytest.mark.django_db


@pytest.fixture
def data():
    board = make_member(board=True)
    lot = LotFactory(street_address='=HYPERLINK("http://evil")')
    make_member(lots=[lot])
    ledger.post_charge(
        lot=lot,
        kind="other_charge",
        amount_cents=2500,
        description="=cmd|' /C calc'!A0",
        actor=board,
    )
    return {"board": board}


@pytest.mark.parametrize("key", sorted(REPORTS))
def test_every_report_runs_and_downloads(client, data, key):
    client.force_login(data["board"])
    assert client.get(reverse("report_run", args=[key])).status_code == 200
    response = client.get(reverse("report_run", args=[key]) + "?format=csv")
    assert response["Content-Type"].startswith("text/csv")
    rows = list(csv.reader(io.StringIO(response.content.decode())))
    assert rows  # at least the header


def test_csv_cells_cannot_become_formulas():
    assert _safe_cell("=SUM(A1)") == "'=SUM(A1)"
    assert _safe_cell("@cmd") == "'@cmd"
    assert _safe_cell("-$25.00") == "-$25.00"  # negative money stays a number
    assert _safe_cell("Lot 7") == "Lot 7"


def test_ar_aging_lists_the_charge(client, data):
    client.force_login(data["board"])
    page = client.get(reverse("report_run", args=["ar_aging"])).content.decode()
    assert "$25.00" in page


@pytest.mark.parametrize(
    ("roles", "status"),
    [
        ([Role.AUDITOR], 200),
        ([Role.MANAGER], 200),
        ([Role.ARC_REVIEWER], 403),
        ([Role.SYSADMIN], 403),
    ],
)
def test_who_sees_reports(client, roles, status):
    client.force_login(make_member(roles=roles))
    assert client.get(reverse("reports")).status_code == status


def test_owner_cannot_see_reports(client):
    client.force_login(make_member(lots=[LotFactory()]))
    assert client.get(reverse("report_run", args=["ar_aging"])).status_code == 403
