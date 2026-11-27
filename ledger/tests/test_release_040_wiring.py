"""Release 0.40.0 integration: the adapters the separate branches left behind
now read the real sources (vendor onboarding D79, document renewals D72, the
books D69-D71, finance gaps D75-D77), and the journal maps the new owner
charge kinds. Each test proves the adapter uses its real source."""

from __future__ import annotations

import datetime

import pytest
from django.utils import timezone

from core import features
from core.policy import SYSTEM
from ledger import cashflow, interest, owner_finance
from ledger import services as ledger
from ledger.books import close, journal, periods, statements, tax
from ledger.models import AccountingSetup, Budget, BudgetLine, EntryKind, LedgerEntry
from ledger.tests.test_books import OCT_END, RUN_DAY, _close_october, w  # noqa: F401
from operations.models import Vendor

pytestmark = pytest.mark.django_db


# --- 1. Taxes read vendor onboarding (D79) -------------------------------------------------


def _profile(vendor, **fields):
    from operations.onboarding_models import ProfileStatus, VendorProfile, VendorType

    values = {
        "status": ProfileStatus.ACTIVE,
        "vendor_type": VendorType.CONTRACTOR,
        "approved_at": timezone.now(),
    }
    values.update(fields)
    return VendorProfile.objects.create(vendor=vendor, **values)


def test_w9_and_1099_come_from_vendor_onboarding(monkeypatch):
    from operations import vendor_onboarding as vo
    from operations.onboarding_models import EntityType

    vendor = Vendor.objects.create(name="Pond Pros (test)", w9_on_file=False)
    calls = []
    monkeypatch.setattr(vo, "w9_on_file", lambda v: calls.append(v) or "from-onboarding")
    assert tax.w9_on_file(vendor) == "from-onboarding" and calls == [vendor]
    monkeypatch.undo()

    # Grandfathered (no onboarding profile): the tick, and 1099 unknown -> yes.
    assert tax.w9_on_file(vendor) is False
    assert vo.eligible_for_1099(vendor) is None and tax.needs_1099(vendor) is True
    # Onboarded with a W-9 date and as a C corporation: W-9 held, no 1099.
    _profile(vendor, entity_type=EntityType.C_CORP, w9_received_on=datetime.date(2026, 3, 1))
    assert tax.w9_on_file(vendor) is True
    assert vo.eligible_for_1099(vendor) is False and tax.needs_1099(vendor) is False


def test_needs_1099_unknown_entity_is_conservative(monkeypatch):
    from operations import vendor_onboarding as vo

    vendor = Vendor.objects.create(name="Gutter Co (test)")
    for value, expected in ((None, True), (True, True), (False, False)):
        monkeypatch.setattr(vo, "eligible_for_1099", lambda v, value=value: value)
        assert tax.needs_1099(vendor) is expected


# --- 2. Renewals read the certificate expiry from vendor onboarding ------------------------


def test_renewals_vendor_coi_from_onboarding_with_fallback(monkeypatch):
    from documents import renewals
    from operations import vendor_onboarding as vo

    day = datetime.date(2027, 5, 1)
    vendor = Vendor.objects.create(name="Snow Movers (test)", insurance_expires_on=day)
    # Grandfathered: the vendor record.
    assert renewals.vendor_coi_expiry(vendor) == day
    # Onboarded with a later certificate: onboarding's date.
    _profile(vendor, coi_expires_on=datetime.date(2027, 9, 1))
    assert renewals.vendor_coi_expiry(vendor) == datetime.date(2027, 9, 1)
    # A certificate recorded on Renewals afterwards moves the vendor record
    # only; the later date wins, so recording a renewal keeps working.
    Vendor.objects.filter(pk=vendor.pk).update(insurance_expires_on=datetime.date(2028, 9, 1))
    vendor.refresh_from_db()
    assert renewals.vendor_coi_expiry(vendor) == datetime.date(2028, 9, 1)
    assert vo.coi_current(vendor, on=datetime.date(2028, 1, 1))
    # The adapter asks vendor onboarding.
    monkeypatch.setattr(vo, "coi_expires_on", lambda v: datetime.date(2030, 1, 1))
    assert renewals.vendor_coi_expiry(vendor) == datetime.date(2030, 1, 1)


def test_recording_a_certificate_on_renewals_is_the_expiry():
    from community.tests.factories import make_member
    from documents import renewals

    board = make_member(board=True)
    vendor = Vendor.objects.create(name="Lawn Care (test)")
    _profile(vendor, coi_expires_on=datetime.date(2026, 12, 1))
    renewals.record_vendor_certificate(
        vendor, expires_on=datetime.date(2027, 12, 1), insurer="Mutual", actor=board
    )
    vendor.refresh_from_db()
    assert vendor.insurance_expires_on == datetime.date(2027, 12, 1)
    assert renewals.vendor_coi_expiry(vendor) == datetime.date(2027, 12, 1)


# --- 3. Insurance claims name the association policy (D72) ---------------------------------


def test_claim_policy_label_reads_the_renewals_policy():
    from community.tests.factories import make_member
    from documents.renewal_models import InsurancePolicy, PolicyKind
    from operations import insurance_claims as ic
    from operations.models_options import InsuranceClaim

    board = make_member(board=True)
    claim = InsuranceClaim.objects.create(
        carrier="Keystone Mutual",
        policy_number="GL 12345",
        incident_on=datetime.date(2026, 8, 1),
        description="Storm damage to the pavilion",
        created_by=board,
    )
    # No policy recorded: the typed text.
    assert ic.policy_for(claim) is None
    assert ic.policy_label(claim) == "Keystone Mutual · GL 12345"
    old = InsurancePolicy.objects.create(
        kind=PolicyKind.GENERAL_LIABILITY,
        carrier="Keystone Mutual",
        policy_number="GL12345",
        coverage_cents=100_000_000,
        starts_on=datetime.date(2025, 7, 1),
        ends_on=datetime.date(2026, 6, 30),
        recorded_by=board,
    )
    current = InsurancePolicy.objects.create(
        kind=PolicyKind.GENERAL_LIABILITY,
        carrier="Keystone Mutual",
        policy_number="gl 12345",
        coverage_cents=100_000_000,
        starts_on=datetime.date(2026, 7, 1),
        ends_on=datetime.date(2027, 6, 30),
        recorded_by=board,
        renews=old,
    )
    # The term in force on the incident date, matched on the number.
    assert ic.policy_for(claim) == current
    assert ic.policy_label(claim) == "General liability insurance · Keystone Mutual · gl 12345"
    other = InsuranceClaim.objects.create(
        carrier="Another Carrier",
        policy_number="",
        incident_on=datetime.date(2026, 8, 1),
        description="Fence",
        created_by=board,
    )
    assert ic.policy_label(other) == "Another Carrier"


# --- 4. Fund balances and budget against actual from the books ----------------------------


def test_cashflow_and_owner_finance_use_the_books_once_a_month_is_closed(w):  # noqa: F811
    today = datetime.date(2026, 11, 6)
    features.set_feature("books", True, actor=SYSTEM)
    before = cashflow.opening_balances(today)
    assert before.closed is False and close.closed_fund_positions() is None
    assert owner_finance.fund_balances(today).closed is False
    _close_october(w)
    positions = close.fund_positions(OCT_END)
    assert close.closed_fund_positions() == (OCT_END, positions)
    opening = cashflow.opening_balances(today)
    assert opening.closed is True and opening.as_of == OCT_END
    assert (opening.operating, opening.reserve) == (
        positions["operating"]["cash"],
        positions["reserve"]["cash"],
    )
    assert (opening.operating, opening.reserve) == (48500, 550125)
    funds = owner_finance.fund_balances(today)
    assert funds.closed is True and funds.as_of == OCT_END
    bs = statements.balance_sheet(OCT_END)["funds"]
    assert funds.operating == bs["operating"]["fund_balance"]
    assert funds.reserve == bs["reserve"]["fund_balance"]


def test_books_off_keeps_the_interim_figures(w):  # noqa: F811
    _close_october(w)
    features.set_feature("books", False, actor=SYSTEM)
    assert close.closed_fund_positions() is None
    assert cashflow.opening_balances(datetime.date(2026, 11, 6)).closed is False


def test_budget_vs_actual_from_the_closed_books(w, monkeypatch):  # noqa: F811
    from community.models import CommunityProfile
    from ledger.models import BudgetStatus, IncomeSource, LineKind

    profile = CommunityProfile.get()
    profile.fiscal_year_start_month = 10  # FY2026: October 2026 to September 2027
    profile.save()
    budget = Budget.objects.create(fiscal_year=2026, status=BudgetStatus.ADOPTED)
    BudgetLine.objects.create(
        budget=budget,
        kind=LineKind.INCOME,
        name="Other owner income",
        income_source=IncomeSource.OTHER,
        annual_cents=120000,
    )
    ledger.post_charge(
        lot=w["lot"],
        kind="other_charge",
        amount_cents=4000,
        description="Clubhouse key",
        posted_on=datetime.date(2026, 10, 10),
        actor=SYSTEM,
    )
    LedgerEntry.objects.create(
        lot=w["lot"],
        kind=EntryKind.INTEREST,
        amount_cents=300,
        description="Interest on overdue balance",
        posted_on=datetime.date(2026, 10, 12),
        period="2026-10",
    )
    monkeypatch.setattr(owner_finance.timezone, "localdate", lambda: RUN_DAY)
    open_bva = owner_finance.budget_vs_actual_ytd()
    assert open_bva.closed is False
    _close_october(w)
    bva = owner_finance.budget_vs_actual_ytd()
    assert bva.closed is True and bva.closed_months == 1
    assert bva.through == datetime.date(2026, 10, 1)
    (line,) = bva.lines
    # Interest charged to owners counts as other owner income.
    assert (line.budget_ytd, line.actual_ytd, line.annual) == (10000, 4300, 120000)


# --- 5. The journal maps the new owner charge kinds ----------------------------------------


def test_journal_maps_interest_legal_costs_and_special_assessments(w):  # noqa: F811
    from ledger import special_assessments as sa

    gl = AccountingSetup.get().accounts()
    assert gl["interest"] == "4150 Interest on overdue balances"
    assert gl["legal_cost"] == "4250 Collection costs recovered"
    for kind, cents in ((EntryKind.INTEREST, 300), (EntryKind.LEGAL_COST, 7500)):
        LedgerEntry.objects.create(
            lot=w["lot"],
            kind=kind,
            amount_cents=cents,
            description=str(kind),
            posted_on=datetime.date(2026, 10, 12),
            period="2026-10" if kind == EntryKind.INTEREST else "",
        )
    levy = sa.levy(
        title="Pond dredging",
        purpose="Dredge the pond",
        total_cents=20000,
        due_dates=[datetime.date(2026, 10, 10)],
        fund="reserve",
        resolution_ref="Resolution 2026-20",
        actor=SYSTEM,
    )
    assert levy.fund == "reserve"
    lines = journal.journal(datetime.date(2026, 10, 1), OCT_END)
    credits = {(ln.account, ln.fund): ln.credit for ln in lines if ln.credit}
    assert credits[(gl["interest"], "operating")] == 300
    assert credits[(gl["legal_cost"], "operating")] == 7500
    special = [ln for ln in lines if ln.account == gl["special_assess"]]
    assert sum(ln.credit for ln in special) == 20000
    assert {ln.fund for ln in special} == {"reserve"}
    # Each fund still balances by itself (interfund lines added).
    b = journal.sum_lines(lines)
    for fund in ("operating", "reserve"):
        assert sum(v for (f, _a), v in b.net.items() if f == fund) == 0


def test_1120h_counts_member_charges_as_exempt_function_income(w):  # noqa: F811
    from community.models import CommunityProfile

    profile = CommunityProfile.get()
    profile.fiscal_year_start_month = 10
    profile.save()
    for kind, cents in ((EntryKind.INTEREST, 300), (EntryKind.LEGAL_COST, 7500)):
        LedgerEntry.objects.create(
            lot=w["lot"],
            kind=kind,
            amount_cents=cents,
            description=str(kind),
            posted_on=datetime.date(2026, 10, 12),
            period="2026-10" if kind == EntryKind.INTEREST else "",
        )
    _close_october(w)  # bank interest of 125 on the reserve account
    t = statements.tax_worksheet(2026)
    assert t["exempt_function_cents"] == 7800
    assert t["other_income_cents"] == 125  # bank interest is not member income
    f = tax.form_1120h(2026)
    assert {n: c for n, _label, c in f["lines"]}["2"] == 125


# --- Finance gaps (D75-D76) wired into interest and the forecast ---------------------------


def test_interest_principal_includes_special_assessments_and_reads_payment_plans(monkeypatch):
    from ledger import payment_plans

    assert EntryKind.SPECIAL_ASSESSMENT in interest.principal_kinds(False)
    monkeypatch.setattr(payment_plans, "plan_kept", lambda lot_id: lot_id == 7)
    assert interest.on_payment_plan(7) is True and interest.on_payment_plan(8) is False


def test_cashflow_special_installments_by_fund(w):  # noqa: F811
    from ledger import special_assessments as sa

    today = timezone.localdate()
    first = cashflow.add_months(today.replace(day=1), 1)
    months = [cashflow.add_months(first, i) for i in range(3)]
    assert cashflow.special_installments(months) == [0, 0, 0]
    for fund in ("reserve", "operating"):
        sa.levy(
            title=f"Levy {fund}",
            purpose="Work",
            total_cents=20000 if fund == "reserve" else 6000,
            due_dates=[months[0] + datetime.timedelta(days=4), months[2]],
            fund=fund,
            resolution_ref="Resolution 2026-21",
            actor=SYSTEM,
        )
    assert cashflow.special_installments(months, "reserve") == [10000, 0, 10000]
    assert cashflow.special_installments(months, "operating") == [3000, 0, 3000]
    inputs = cashflow.Inputs(
        start=first,
        opening_operating=0,
        opening_reserve=0,
        billed=(0, 0, 0),
        collection_rate_bp=10000,
        special=(10000, 0, 10000),
        special_operating=(3000, 0, 3000),
        months=3,
    )
    rows = cashflow.project(inputs)
    assert [r.collections for r in rows] == [3000, 0, 3000]
    assert rows[-1].reserve_close == 20000 and rows[-1].operating_close == 6000
    assert periods.month_key(months[0]) == months[0].strftime("%Y-%m")


# --- 6. Vendor checks never offer an unapproved vendor; reimbursements still pay ------------


def test_vendor_checks_respect_vendor_onboarding_and_reimbursements_pay():
    from community.tests.factories import make_member
    from operations import reimbursements, vendor_checks
    from operations.models import (
        ExpenseCategory,
        Invoice,
        ReimbursementClaim,
        ReimbursementInvoice,
    )
    from operations.onboarding_models import ProfileStatus
    from operations.services import payment_block_reason

    board = make_member(board=True)
    category = ExpenseCategory.objects.create(code="5100", name="Grounds")
    pending = Vendor.objects.create(name="New Vendor (test)")
    _profile(pending, status=ProfileStatus.SUBMITTED, approved_at=None)
    invoice = Invoice.objects.create(
        vendor=pending,
        number="N-1",
        invoice_date=datetime.date(2026, 11, 1),
        amount_cents=5000,
        category=category,
        state="approved",
        entered_by=board,
    )
    (row,) = [p for p in vendor_checks.payables(board) if p.invoice == invoice]
    assert "isn't an approved vendor" in row.refusal
    # The reimbursement payee is inactive by design (D77); its invoices are
    # governed by the claim's own approvals, not by vendor onboarding.
    payee = reimbursements._payee_vendor()
    assert payee.active is False
    paid_back = Invoice.objects.create(
        vendor=payee,
        number="R-1",
        invoice_date=datetime.date(2026, 11, 1),
        amount_cents=2500,
        category=category,
        state="approved",
        entered_by=board,
    )
    assert payment_block_reason(paid_back) is not None  # not a reimbursement yet
    claim = ReimbursementClaim.objects.create(
        claimant=board.person,
        submitted_by=board,
        amount_cents=2500,
        spent_on=datetime.date(2026, 10, 30),
        payee="Garden centre",
        category=category,
        description="Mulch",
    )
    ReimbursementInvoice.objects.create(claim=claim, invoice=paid_back)
    assert payment_block_reason(paid_back) is None
    (row,) = [p for p in vendor_checks.payables(None) if p.invoice == paid_back]
    assert row.refusal == ""
