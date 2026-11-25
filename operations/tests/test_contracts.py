"""Recurring invoices against board-approved contracts (Milestone 3 phase 5)."""

import datetime

import pytest
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.urls import reverse

from audit.models import AuditEvent
from community.models import Role
from community.tests.factories import LotFactory, make_member
from core.workflow import WorkflowError
from operations import services
from operations.models import Contract, ExpenseCategory, Invoice, Vendor

pytestmark = pytest.mark.django_db
START = datetime.date(2026, 1, 1)


@pytest.fixture
def w():
    data = {
        "manager": make_member(roles=[Role.MANAGER]),
        "b1": make_member(board=True),
        "b2": make_member(board=True),
        "vendor": Vendor.objects.create(name="Green Lawns (demo)"),
        "other_vendor": Vendor.objects.create(name="Pond Pros (demo)"),
        "cat": ExpenseCategory.objects.create(code="5200", name="Landscaping"),
    }
    data["contract"] = services.create_contract(
        vendor=data["vendor"],
        title="Mowing",
        category=data["cat"],
        schedule="monthly",
        amount_cap_cents=80000,
        starts_on=START,
        citation="Board resolution 2026-02",
        actor=data["manager"],
    )
    return data


def _approve(w):
    w["contract"] = services.CONTRACT.apply(
        w["contract"], "approve", actor=w["b1"], note="Minutes of Jan 12"
    )
    return w["contract"]


def _invoice(w, cents, day, number, contract=True):
    inv = services.create_invoice(
        vendor=w["vendor"],
        number=number,
        invoice_date=day,
        amount_cents=cents,
        category=w["cat"],
        contract=w["contract"] if contract else None,
        actor=w["manager"],
    )
    services.INVOICE.apply(inv, "submit", actor=w["manager"])
    inv.refresh_from_db()
    return inv


def test_contract_needs_another_board_members_approval(w):
    with pytest.raises(PermissionDenied):
        services.CONTRACT.apply(w["contract"], "approve", actor=w["manager"], note="x")
    entered_by_board = services.create_contract(
        vendor=w["vendor"],
        title="Leaves",
        category=w["cat"],
        schedule="annual",
        amount_cap_cents=1000,
        starts_on=START,
        citation="R1",
        actor=w["b1"],
    )
    with pytest.raises(WorkflowError):
        services.CONTRACT.apply(entered_by_board, "approve", actor=w["b1"], note="x")
    services.CONTRACT.apply(entered_by_board, "approve", actor=w["b2"], note="x")


def test_draft_contract_preapproves_nothing(w):
    inv = _invoice(w, 50000, datetime.date(2026, 3, 5), "A1")
    assert inv.state == "pending_approval" and inv.contract_period == ""


def test_invoice_within_cap_is_preapproved_and_audited(w):
    _approve(w)
    inv = _invoice(w, 75000, datetime.date(2026, 3, 5), "A1")
    assert inv.state == "approved"
    assert inv.contract_period == "2026-03" and inv.required_approvals == 0
    assert AuditEvent.objects.filter(action="invoice.contract_preapproved").exists()


def test_over_cap_needs_normal_approvals(w):
    _approve(w)
    inv = _invoice(w, 80001, datetime.date(2026, 3, 5), "A1")
    assert inv.state == "pending_approval" and inv.contract_period == ""


def test_second_invoice_in_the_same_period_needs_approval(w):
    _approve(w)
    _invoice(w, 70000, datetime.date(2026, 3, 5), "A1")
    second = _invoice(w, 70000, datetime.date(2026, 3, 20), "A2")
    assert second.state == "pending_approval"
    april = _invoice(w, 70000, datetime.date(2026, 4, 2), "A3")
    assert april.state == "approved"


def test_outside_the_contract_dates_needs_approval(w):
    _approve(w)
    services.end_contract(w["contract"], ends_on=datetime.date(2026, 6, 30), actor=w["b1"])
    late = _invoice(w, 1000, datetime.date(2026, 7, 2), "A9")
    assert late.state == "pending_approval"
    early = _invoice(w, 1000, datetime.date(2025, 12, 30), "A0")
    assert early.state == "pending_approval"


def test_contract_must_match_the_vendor(w):
    with pytest.raises(services.OperationsError):
        services.create_invoice(
            vendor=w["other_vendor"],
            number="X",
            invoice_date=START,
            amount_cents=10,
            category=w["cat"],
            contract=w["contract"],
            actor=w["manager"],
        )


def test_approved_terms_are_frozen_by_the_database(w):
    _approve(w)
    with pytest.raises(IntegrityError), transaction.atomic():
        Contract.objects.filter(pk=w["contract"].pk).update(amount_cap_cents=999999)
    services.end_contract(w["contract"], ends_on=datetime.date(2026, 12, 31), actor=w["b1"])
    with pytest.raises(services.OperationsError):
        services.end_contract(w["contract"], ends_on=datetime.date(2027, 6, 30), actor=w["b1"])
    with pytest.raises(IntegrityError), transaction.atomic():
        Contract.objects.filter(pk=w["contract"].pk).update(ends_on=datetime.date(2030, 1, 1))


def test_owners_cannot_see_contracts(client, w):
    owner = make_member(lots=[LotFactory()])
    client.force_login(owner)
    assert client.get(reverse("contracts")).status_code == 403
    assert client.get(reverse("contract_detail", args=[w["contract"].pk])).status_code == 403


def test_contract_pages_for_the_board(client, w):
    _approve(w)
    _invoice(w, 70000, datetime.date.today().replace(day=1), "NOW")
    client.force_login(w["b2"])
    page = client.get(reverse("contracts")).content
    assert b"Mowing" in page and b"received" in page
    assert client.get(reverse("contract_detail", args=[w["contract"].pk])).status_code == 200
    assert Invoice.objects.filter(contract=w["contract"]).count() == 1
