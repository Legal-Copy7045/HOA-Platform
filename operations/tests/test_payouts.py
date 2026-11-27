"""Expense reimbursements and vendor checks (D77)."""

import datetime

import pytest
from django.core.exceptions import PermissionDenied
from django.urls import reverse
from django.utils import timezone

from audit.models import AuditEvent
from community import services as community
from community.models import Role
from community.tests.factories import LotFactory, make_member
from core.append_only import AppendOnlyError
from core.db_roles import append_only_tables
from core.policy import SYSTEM
from documents.models import ScanResult
from documents.tests.helpers import pdf
from ledger import accounting, check_numbers
from operations import reimbursements as rb
from operations import services as ops
from operations import vendor_checks as vc
from operations.models import (
    ExpenseCategory,
    Invoice,
    InvoicePayment,
    ReimbursementClaim,
    Vendor,
    VendorCheck,
)

pytestmark = pytest.mark.django_db
TODAY = timezone.localdate()
D = datetime.timedelta


@pytest.fixture
def w():
    category = ExpenseCategory.objects.create(code="5400", name="Supplies")
    board = make_member(board=True, lots=[LotFactory(street_address="1 Alpha Way")])
    board2 = make_member(board=True)
    board3 = make_member(board=True)
    manager = make_member(roles=[Role.MANAGER])
    volunteer = make_member(lots=[LotFactory(street_address="2 Alpha Way")])
    return {
        "category": category,
        "board": board,
        "board2": board2,
        "board3": board3,
        "manager": manager,
        "volunteer": volunteer,
    }


def _claim(w, who="volunteer", amount=4250, files=None):
    return rb.submit(
        amount_cents=amount,
        spent_on=TODAY - D(days=3),
        payee="Hardware store",
        category=w["category"],
        description="Paint for the pavilion",
        files=files if files is not None else [pdf("receipt.pdf")],
        actor=w[who],
    )


# --- Reimbursements ---------------------------------------------------------------------------


def test_submit_needs_a_receipt_and_scans_it(w, monkeypatch):
    with pytest.raises(rb.ReimbursementError, match="receipt"):
        _claim(w, files=[])
    monkeypatch.setattr("documents.attachments.scan", lambda f: (ScanResult.INFECTED, "test"))
    with pytest.raises(rb.ReimbursementError, match="not accepted"):
        _claim(w)
    assert not ReimbursementClaim.objects.unscoped("t").exists()


def test_approval_by_others_and_invoice_in_category(w):
    claim = _claim(w)
    assert claim.required_approvals == 1
    with pytest.raises(PermissionDenied):
        rb.approve(claim, actor=w["manager"])
    rb.approve(claim, actor=w["board2"])
    st = rb.state(claim)
    assert st["status"] == "approved"
    invoice = st["invoice"]
    assert invoice.state == "approved" and invoice.category == w["category"]
    assert invoice.approvals.count() == 1
    # The journal books it to the category once paid; the 1099 worksheet skips it.
    rb.pay_by_transfer(claim, paid_on=TODAY, reference="TRF-77", actor=w["manager"])
    assert rb.state(claim)["status"] == "paid"
    rows = accounting.journal_rows(TODAY, TODAY)
    assert any(r[3] == "5400 Supplies" and r[4] == "42.50" for r in rows)
    assert all(v["vendor"].name != rb.PAYEE_NAME for v in accounting.vendor_1099_totals(TODAY.year))


def test_two_approvals_above_threshold_and_own_claim_refused(w):
    community.set_rule(
        key="reimbursement.second_approval_cents", value="1000", citation="t", actor=SYSTEM
    )
    claim = _claim(w, who="board")
    assert claim.required_approvals == 2
    with pytest.raises(rb.ReimbursementError, match="own claim"):
        rb.approve(claim, actor=w["board"])
    assert AuditEvent.objects.filter(action="policy.own_matter_refused").exists()
    rb.approve(claim, actor=w["board2"])
    with pytest.raises(rb.ReimbursementError, match="already approved"):
        rb.approve(claim, actor=w["board2"])
    assert rb.state(claim)["status"] == "awaiting"
    rb.approve(claim, actor=w["board3"])
    assert rb.state(claim)["status"] == "approved"
    # D66: a board member's claim is paid by another board member, never the manager.
    with pytest.raises(rb.ReimbursementError, match="board member"):
        rb.pay_by_transfer(claim, paid_on=TODAY, reference="X1", actor=w["manager"])
    with pytest.raises(rb.ReimbursementError, match="own claim"):
        rb.pay_by_transfer(claim, paid_on=TODAY, reference="X1", actor=w["board"])
    rb.pay_by_transfer(claim, paid_on=TODAY, reference="X1", actor=w["board2"])


def test_receipt_scan_gate_blocks_approval(w, settings):
    settings.DOCUMENT_REQUIRE_CLEAN_SCAN = True
    claim = _claim(w)  # the test scanner leaves it unscanned
    with pytest.raises(rb.ReimbursementError, match="malware scan"):
        rb.approve(claim, actor=w["board2"])


def test_decline_and_withdraw(w):
    claim = _claim(w)
    with pytest.raises(rb.ReimbursementError):
        rb.withdraw(claim, actor=w["board2"])
    rb.decline(claim, note="Not an association expense", actor=w["board2"])
    assert rb.state(claim)["status"] == "declined"
    other = _claim(w)
    rb.withdraw(other, actor=w["volunteer"])
    assert rb.state(other)["status"] == "withdrawn"
    with pytest.raises(AppendOnlyError):
        other.save()
    for table in (
        "operations_reimbursementclaim",
        "operations_reimbursementdecision",
        "operations_vendorcheck",
        "operations_vendorcheckevent",
    ):
        assert table in append_only_tables()


def test_claims_are_scoped_to_the_claimant(client, w):
    claim = _claim(w)
    other_owner = make_member(lots=[LotFactory(street_address="3 Alpha Way")])
    client.force_login(other_owner)
    assert client.get(reverse("reimbursement_detail", args=[claim.pk])).status_code == 404
    from documents.attachments import attachments_for

    file_id = attachments_for(claim).first().pk
    assert client.get(reverse("reimbursement_file", args=[claim.pk, file_id])).status_code == 404
    assert b"Hardware store" not in client.get(reverse("reimbursements")).content
    client.force_login(w["volunteer"])
    assert b"Hardware store" in client.get(reverse("reimbursements")).content
    assert client.get(reverse("reimbursement_file", args=[claim.pk, file_id])).status_code == 200
    client.force_login(w["board2"])
    page = client.get(reverse("reimbursement_detail", args=[claim.pk])).content.decode()
    assert reverse("reimbursement_action", args=[claim.pk, "approve"]) in page


def test_submit_page(client, w):
    client.force_login(w["volunteer"])
    assert client.get(reverse("reimbursement_new")).status_code == 200
    response = client.post(
        reverse("reimbursement_new"),
        {
            "amount_cents": "12.00",
            "spent_on": (TODAY - D(days=1)).isoformat(),
            "payee": "Garden centre",
            "category": w["category"].pk,
            "description": "Mulch",
            "receipts": pdf("r.pdf"),
        },
    )
    assert response.status_code == 302
    event = AuditEvent.objects.filter(action="reimbursement.submitted").get()
    assert set(event.metadata) <= {"claim_id", "amount_cents", "need"}


# --- Vendor checks ------------------------------------------------------------------------


def _approved_invoice(w, number="INV-9", amount=30000):
    vendor = Vendor.objects.create(name="Pond Co", address="5 Water Lane\nTown, PA 17000")
    inv = ops.create_invoice(
        vendor=vendor,
        number=number,
        invoice_date=TODAY - D(days=5),
        amount_cents=amount,
        category=w["category"],
        actor=w["manager"],
    )
    ops.INVOICE.apply(inv, "submit", actor=w["manager"])
    ops.approve(inv, actor=w["board2"])
    inv.refresh_from_db()
    return inv


def test_issue_checks_records_payment_and_numbers_are_unique(w):
    inv = _approved_invoice(w)
    data = vc.checks_pdf([inv.pk], on=TODAY, actor=w["manager"])
    assert data.startswith(b"%PDF")
    (check,) = vc.issue([inv.pk], first_number="1042", issued_on=TODAY, actor=w["manager"])
    inv.refresh_from_db()
    assert inv.state == "paid"
    payment = InvoicePayment.objects.get(invoice=inv)
    assert payment.method == "check" and payment.reference == "1042"
    assert check.memo == "Invoice INV-9"
    inv2 = _approved_invoice(w, number="INV-10")
    with pytest.raises(vc.VendorCheckError, match="already recorded"):
        vc.issue([inv2.pk], first_number="1042", issued_on=TODAY, actor=w["manager"])
    assert check_numbers.suggest_next() == "1043"


def test_check_numbers_shared_with_disbursement_checks(w):
    from ledger import disbursements as disb

    inv = _approved_invoice(w)
    vc.issue([inv.pk], first_number="0500", issued_on=TODAY, actor=w["manager"])
    assert check_numbers.taken(["0500", "0501"]) == {"0500"}
    for key, value in (
        ("disbursement.surplus_by_share_paid", "false"),
        ("disbursement.surplus_cash_allowed", "true"),
    ):
        community.set_rule(key=key, value=value, citation="t", actor=SYSTEM)
    lot_ids = [a.lot_id for a in disb.surplus_accounts()]
    batch = disb.propose_surplus(
        total_cents=1000,
        year=TODAY.year,
        resolution_ref="R-1",
        by_check=lot_ids,
        actor=w["manager"],
    )
    disb.approve(batch, actor=w["board2"])
    # A refund or surplus check cannot take a number a vendor check used.
    with pytest.raises(disb.DisbursementError, match="0500"):
        disb.issue_checks(batch, first_number="0500", issued_on=TODAY, actor=w["board3"])
    disb.issue_checks(batch, first_number="0600", issued_on=TODAY, actor=w["board3"])
    inv2 = _approved_invoice(w, number="INV-11")
    with pytest.raises(vc.VendorCheckError, match="0600"):
        vc.issue([inv2.pk], first_number="0600", issued_on=TODAY, actor=w["manager"])
    assert check_numbers.suggest_next() == "0602"


def test_void_reopens_invoice_and_clear(w):
    inv = _approved_invoice(w)
    (check,) = vc.issue([inv.pk], first_number="2001", issued_on=TODAY, actor=w["manager"])
    with pytest.raises(PermissionDenied):
        vc.void_check(check, reason="lost", actor=w["manager"])
    vc.void_check(check, reason="Lost in the mail", actor=w["board2"])
    inv.refresh_from_db()
    assert inv.state == "approved" and ops.paid_cents(inv) == 0
    with pytest.raises(vc.VendorCheckError):
        vc.mark_cleared(check, actor=w["manager"])
    (again,) = vc.issue([inv.pk], first_number="2002", issued_on=TODAY, actor=w["manager"])
    vc.mark_cleared(again, on=TODAY, actor=w["manager"])
    assert vc.status(again) == "cleared"
    assert [c.number for c in vc.outstanding_checks(TODAY)] == ["2002"]


def test_outstanding_checks_for_reconciliation(w):
    inv = _approved_invoice(w)
    (_check,) = vc.issue([inv.pk], first_number="3001", issued_on=TODAY, actor=w["manager"])
    rows = vc.outstanding_checks(TODAY)
    assert rows[0].number == "3001" and rows[0].cleared_on is None
    assert vc.outstanding_checks(TODAY - D(days=1)) == []


def test_reimbursement_check_follows_d66(w):
    claim = _claim(w, who="board")
    rb.approve(claim, actor=w["board2"])
    invoice = rb.state(claim)["invoice"]
    rows = {p.invoice.pk: p for p in vc.payables(w["manager"])}
    assert rows[invoice.pk].refusal
    with pytest.raises(vc.VendorCheckError, match="board member"):
        vc.issue([invoice.pk], first_number="4001", issued_on=TODAY, actor=w["manager"])
    with pytest.raises(vc.VendorCheckError, match="own claim"):
        vc.checks_pdf([invoice.pk], on=TODAY, actor=w["board"])
    (check,) = vc.issue([invoice.pk], first_number="4001", issued_on=TODAY, actor=w["board2"])
    assert check.memo == f"Reimbursement claim #{claim.pk}"
    with pytest.raises(vc.VendorCheckError):
        vc.mark_cleared(check, actor=w["manager"])


def test_vendor_checks_page(client, w):
    inv = _approved_invoice(w)
    client.force_login(w["manager"])
    page = client.get(reverse("vendor_checks")).content.decode()
    assert "Pond Co" in page
    response = client.get(reverse("vendor_checks_pdf"), {"invoice": inv.pk, "issued_on": TODAY})
    assert response.status_code == 200
    response = client.post(
        reverse("vendor_checks"),
        {"invoice": inv.pk, "first_number": "777", "issued_on": TODAY.isoformat()},
    )
    assert response.status_code == 302
    check = VendorCheck.objects.get(number="777")
    client.force_login(w["board2"])
    client.post(reverse("vendor_check_action", args=[check.pk, "void"]), {"reason": "Typo"})
    assert vc.status(check) == "voided"
    owner = w["volunteer"]
    client.force_login(owner)
    assert client.get(reverse("vendor_checks")).status_code == 403
    assert Invoice.objects.get(pk=inv.pk).state == "approved"
