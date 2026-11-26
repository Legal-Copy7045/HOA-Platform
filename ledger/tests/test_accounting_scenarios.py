"""Accounting scenarios end to end (independent QA, M5).

Each scenario drives the services (and the board's or owner's pages where
they would act) and then checks the global invariants in
``ledger/tests/accounting_invariants.py``. A scenario that exposes a bug is
kept as ``xfail(strict=True)`` with the bug in its reason.
"""

import csv
import datetime
import io
import zoneinfo
from unittest import mock

import pytest
from django.urls import reverse
from django.utils import timezone

from community import services as community
from community.models import Person, Role
from community.tests.factories import LotFactory, days_ago, make_member
from core.models import FeatureSetting
from core.policy import SYSTEM
from core.scoping import system_access
from core.workflow import WorkflowError
from ledger import accounting as acc
from ledger import allocation, letters, services
from ledger.allocation import DASH
from ledger.bankfeeds import matching
from ledger.bankfeeds import services as bank
from ledger.bankfeeds.base import TransactionInfo
from ledger.models import CollectionLetter, EntryKind, LedgerEntry
from ledger.tests.accounting_invariants import Books, journal
from operations.models import ExpenseCategory, Invoice, InvoicePayment, Vendor

pytestmark = pytest.mark.django_db
D = datetime.date
TODAY = timezone.localdate()
DUES = 4500


def ago(n):
    return TODAY - datetime.timedelta(days=n)


@pytest.fixture
def books(app_role_connection):
    b = Books()
    b.app_role = app_role_connection
    return b


@pytest.fixture
def w():
    board = make_member(board=True)
    for key, value in (
        ("dues.monthly_cents", str(DUES)),
        ("late_fee.cents", "2500"),
        ("late_fee.grace_days", "15"),
    ):
        community.set_rule(key=key, value=value, citation="Declaration §7.2 (QA)", actor=SYSTEM)
    lot, other = LotFactory(), LotFactory()
    return {
        "board": board,
        "manager": make_member(roles=[Role.MANAGER]),
        "lot": lot,
        "owner": make_member(lots=[lot]),
        "other": other,
        "owner_b": make_member(lots=[other]),
    }


def _check(books, **kw):
    books.check(app_role_connection=books.app_role, **kw)


def _charge(w, cents, desc="Pool key", on=None, lot=None):
    return services.post_charge(
        lot=lot or w["lot"],
        kind=EntryKind.OTHER_CHARGE,
        amount_cents=cents,
        description=desc,
        posted_on=on,
        actor=w["board"],
    )


def _pay(w, cents, on, lot=None, **kw):
    return services.record_payment(
        lot=lot or w["lot"], amount_cents=cents, received_on=on, actor=w["board"], **kw
    )


def _dues(year, month):
    services.post_monthly_assessments(year=year, month=month, actor=SYSTEM)
    with system_access("t"):
        return {
            e.lot_id: e
            for e in LedgerEntry.objects.filter(
                kind=EntryKind.ASSESSMENT, period=f"{year}-{month:02d}"
            )
        }


def _open(lot):
    return {c.entry.pk: c.open_cents for c in allocation.open_charges(lot.pk)}


def _rule_assessments_first():
    community.set_rule(
        key="ledger.assessments_before_fees", value="true", citation="Decl. §7.6 (QA)", actor=SYSTEM
    )


# --- 1. Applying payments ------------------------------------------------------------------


def test_exact_payment_pays_one_charge(w, books):
    charge = _charge(w, 4500, on=D(2026, 3, 5))
    payment = _pay(w, 4500, D(2026, 3, 6), reference="101")
    assert allocation.charge_status(charge) == "paid"
    assert allocation.applications_of(payment.pk) == [(charge.pk, 4500)]
    assert services.balance(w["lot"].pk) == 0 and allocation.unapplied_credit(w["lot"].pk) == 0
    _check(books)


def test_partial_payment_leaves_the_charge_partly_paid(w, books):
    charge = _charge(w, 4500, "Gate remote", D(2026, 3, 5))
    _pay(w, 1250, D(2026, 3, 6))
    (view,) = allocation.open_charges(w["lot"].pk)
    assert view.entry.pk == charge.pk and view.open_cents == 3250 and view.status == "partial"
    assert [p.amount_cents for p in view.payments] == [1250]
    _check(books)


def test_payment_spread_oldest_first_then_dues_first_with_the_rule(w, books):
    jan, feb, mar = (_dues(2026, m) for m in (1, 2, 3))
    fees = {}
    for lot in (w["lot"], w["other"]):
        fees[lot.pk] = _charge(w, 3000, "Fine", D(2026, 2, 10), lot=lot)
    _pay(w, 10_000, D(2026, 3, 10))  # oldest first
    lot = w["lot"].pk
    assert _open(w["lot"]) == {fees[lot].pk: 2000, mar[lot].pk: 4500}
    _check(books)
    _rule_assessments_first()
    _pay(w, 10_000, D(2026, 3, 10), lot=w["other"])  # dues before the fine
    other = w["other"].pk
    assert _open(w["other"]) == {fees[other].pk: 3000, mar[other].pk: 3500}
    assert jan[other].pk not in _open(w["other"]) and feb[other].pk not in _open(w["other"])
    _check(books)


def test_directed_payment_pays_the_chosen_charges_then_follows_the_rule(w, books):
    jan, _feb, mar = (_dues(2026, m)[w["lot"].pk] for m in (1, 2, 3))
    payment = _pay(w, 5000, D(2026, 3, 10), apply_to=[mar.pk])
    assert dict(allocation.applications_of(payment.pk)) == {mar.pk: 4500, jan.pk: 500}
    assert _open(w["lot"])[jan.pk] == 4000
    _check(books)


def test_directed_to_another_homes_charge_is_refused_by_service_and_page(client, w, books):
    theirs = _charge(w, 1000, "B fee", D(2026, 3, 1), lot=w["other"])
    mine = _charge(w, 1000, "A fee", D(2026, 3, 1))
    with pytest.raises(services.LedgerError, match="open charges"):
        _pay(w, 1000, D(2026, 3, 2), apply_to=[theirs.pk])
    client.force_login(w["board"])
    client.post(
        reverse("lot_ledger_action", args=[w["lot"].pk, "payment"]),
        {
            "amount_cents": "10.00",
            "received_on": "2026-03-02",
            "method": "check",
            "reference": "10",
            "apply_to": [str(theirs.pk)],
        },
    )
    with system_access("t"):
        assert not LedgerEntry.objects.filter(kind="payment").exists()
    _pay(w, 1000, D(2026, 3, 2), reference="9")
    with pytest.raises(services.LedgerError, match="open charges"):  # already paid
        _pay(w, 1000, D(2026, 3, 2), apply_to=[mine.pk])
    assert allocation.charge_status(theirs) == "open"
    assert allocation.charge_status(mine) == "paid"
    _check(books)


# --- 2. Overpayment and credit -------------------------------------------------------------


def test_overpayment_becomes_credit_and_draws_down_as_dues_post(w, books):
    lot = w["lot"].pk
    mar = _dues(2026, 3)[lot]
    _pay(w, 10_000, D(2026, 3, 15), reference="300")
    assert allocation.charge_status(mar) == "paid"
    assert allocation.unapplied_credit(lot) == 5500
    upcoming = services.upcoming_charges(services.balance(lot), lot_id=lot, on=D(2026, 3, 15))
    # D30: future months are listed (not yet paid); the credit covers April only.
    assert [(u.due_on, u.covered_by_credit) for u in upcoming] == [
        (D(2026, 4, 1), True),
        (D(2026, 5, 1), False),
        (D(2026, 6, 1), False),
    ]
    assert _open(w["lot"]) == {}
    apr = _dues(2026, 4)[lot]
    assert allocation.charge_status(apr) == "paid" and allocation.unapplied_credit(lot) == 1000
    may = _dues(2026, 5)[lot]
    assert _open(w["lot"]) == {may.pk: 3500} and allocation.unapplied_credit(lot) == 0
    _check(books)


def _sell(w, on):
    buyer_user = make_member()
    with system_access("t"):
        buyer = Person.objects.get(user=buyer_user)
    community.transfer_lot(w["lot"], new_owners=[buyer], transfer_date=on, actor=SYSTEM)
    return buyer_user


def _next_month():
    return (TODAY.year + TODAY.month // 12, TODAY.month % 12 + 1)


def test_former_owner_sees_their_credit_read_only_in_my_history(client, w, books):
    _charge(w, 4500, "Dues", ago(60))
    _pay(w, 13_500, ago(50), reference="501")  # 90.00 left as credit
    buyer = _sell(w, ago(20))
    _charge(w, 700, "Buyer key", ago(5))  # after the sale: never shown to the seller
    client.force_login(w["owner"])
    page = client.get(reverse("my_history")).content.decode()
    assert "Balance when you sold" in page and "-$90.00" in page
    assert "Buyer key" not in page
    assert client.get(reverse("my_account")).status_code == 403  # read-only: My history only
    client.force_login(buyer)
    assert "Buyer key" in client.get(reverse("my_account")).content.decode()
    _check(books)


@pytest.mark.xfail(
    strict=True,
    reason="BUG: a seller's unapplied credit stays on the lot and pays the buyer's dues "
    "(ledger is per lot, transfer_lot never settles the old account)",
)
def test_sellers_credit_never_pays_the_buyers_dues(client, w, books):
    _charge(w, 4500, "Dues", ago(60))
    _pay(w, 13_500, ago(50), reference="502")
    buyer = _sell(w, ago(20))
    year, month = _next_month()
    buyer_dues = _dues(year, month)[w["lot"].pk]
    _check(books)
    client.force_login(buyer)
    page = client.get(reverse("my_account")).content.decode()
    assert "$90.00" not in page  # the seller's money is not shown to the buyer
    assert allocation.charge_status(buyer_dues) == "open"  # ...nor spent on the buyer's dues


# --- 3. Late payment, late fees, Eastern dates -----------------------------------------------


def test_late_fee_posted_manually_then_payments_follow_the_rule(w, books):
    octo = _dues(2026, 10)
    assert services.late_fee_candidates(year=2026, month=10, on=D(2026, 10, 16)) == []
    late = services.late_fee_candidates(year=2026, month=10, on=D(2026, 10, 20))
    assert {r["lot"].pk for r in late} == {w["lot"].pk, w["other"].pk}
    assert services.post_late_fees(year=2026, month=10, actor=SYSTEM, on=D(2026, 10, 20)) == 0
    assert services.post_late_fees(year=2026, month=10, actor=w["board"], on=D(2026, 10, 20)) == 2
    with system_access("t"):
        fees = {e.lot_id: e for e in LedgerEntry.objects.filter(kind="late_fee")}
    assert fees[w["lot"].pk].posted_on == D(2026, 10, 20) and fees[w["lot"].pk].amount_cents == 2500
    nov = _dues(2026, 11)
    _pay(w, 9000, D(2026, 11, 5))  # oldest first: October dues, the fee, then November
    a = w["lot"].pk
    assert _open(w["lot"]) == {nov[a].pk: 2500}
    _check(books)
    _rule_assessments_first()
    _pay(w, 9000, D(2026, 11, 5), lot=w["other"])  # dues first: the fee stays open
    b = w["other"].pk
    assert _open(w["other"]) == {fees[b].pk: 2500}
    assert octo[b].pk not in _open(w["other"])
    _check(books)


def test_reversing_a_paid_late_fee_frees_the_money_and_is_not_charged_again(w, books):
    _dues(2026, 10)
    services.post_late_fees(year=2026, month=10, actor=w["board"], on=D(2026, 10, 20))
    nov = _dues(2026, 11)[w["lot"].pk]
    _pay(w, 9000, D(2026, 11, 5))
    with system_access("t"):
        fee = LedgerEntry.objects.get(lot=w["lot"], kind="late_fee")
    assert allocation.charge_status(fee) == "paid" and _open(w["lot"]) == {nov.pk: 2500}
    services.reverse_entry(fee, reason="Waived by the board", actor=w["board"])
    assert _open(w["lot"]) == {} and allocation.unapplied_credit(w["lot"].pk) == 0
    views = {v.entry.pk: v.status for v in allocation.charges(w["lot"].pk, open_only=False)}
    assert views[fee.pk] == "reversed"
    assert services.post_late_fees(year=2026, month=10, actor=w["board"], on=D(2026, 10, 25)) == 0
    _check(books)


@pytest.mark.xfail(
    strict=True,
    reason="BUG: late fee charged although the month's dues were paid on time "
    "(late_fee_candidates tests the lot balance, not whether the assessment is open)",
)
def test_no_late_fee_when_the_months_dues_were_paid_on_time(w, books):
    _dues(2026, 10)
    _pay(w, 4500, D(2026, 10, 1))
    _charge(w, 1000, "Clubhouse key", D(2026, 10, 18))  # not even due yet
    _check(books)
    late = services.late_fee_candidates(year=2026, month=10, on=D(2026, 10, 20))
    assert w["lot"].pk not in {r["lot"].pk for r in late}


def test_entries_at_eastern_midnight_are_dated_in_eastern_time(w, books):
    # 11:30 pm Eastern today is already tomorrow in UTC.
    late_evening = datetime.datetime.combine(
        TODAY, datetime.time(23, 30), tzinfo=zoneinfo.ZoneInfo("America/New_York")
    ).astimezone(datetime.UTC)
    assert late_evening.date() == TODAY + datetime.timedelta(days=1)
    with mock.patch("django.utils.timezone.now", return_value=late_evening):
        assert timezone.localdate() == TODAY
        charge = _charge(w, 2000, "Evening charge")
        credit = services.post_credit(
            lot=w["lot"], amount_cents=500, description="Goodwill", actor=w["board"]
        )
        reversal = services.reverse_entry(credit, reason="Wrong home", actor=w["board"])
        a = services.aging(w["lot"].pk)
        due = services.past_due(w["lot"].pk)
    assert charge.posted_on == credit.posted_on == reversal.posted_on == TODAY
    assert a["current"] == 2000 and due == 0  # due today in Eastern time, not yet past due
    assert services.past_due(w["lot"].pk, today=TODAY + datetime.timedelta(days=1)) == 2000
    _check(books)


# --- 4. Underpayment, aging, collections, statements ----------------------------------------


def test_partial_payments_move_the_aging_buckets(w, books):
    lot = w["lot"].pk
    for m in (1, 2, 3):
        _dues(2026, m)
    as_of = D(2026, 4, 15)
    a = services.aging(lot, as_of)
    assert (a["over_90"], a["61_90"], a["31_60"]) == (4500, 4500, 4500)
    _pay(w, 4500, D(2026, 4, 2))
    a = services.aging(lot, as_of)
    assert (a["over_90"], a["61_90"], a["31_60"]) == (0, 4500, 4500)
    _pay(w, 2000, D(2026, 4, 3))
    a = services.aging(lot, as_of)
    assert (a["over_90"], a["61_90"], a["31_60"]) == (0, 2500, 4500)
    assert sum(a[b] for b in services.BUCKETS) - a["credit"] == services.balance(lot, as_of)
    assert services.past_due(lot, today=as_of) == 7000
    _check(books)


def test_aging_as_of_a_date_before_a_bounce_shows_the_charge_paid(w, books):
    charge = _charge(w, 3000, "Fine", D(2026, 1, 5))
    payment = _pay(w, 3000, D(2026, 1, 10), reference="77")
    services.reverse_entry(payment, reason="Check returned", actor=w["board"])  # posted today
    assert allocation.charge_status(charge) == "open"
    _check(books)
    a = services.aging(w["lot"].pk, D(2026, 2, 1))
    assert services.balance(w["lot"].pk, D(2026, 2, 1)) == 0
    assert sum(a[b] for b in services.BUCKETS) == 0 and a["credit"] == 0


def test_collections_open_close_and_letters_follow_the_past_due_amount(w, books):
    lot = w["lot"]
    _dues(2026, 1)
    _dues(2026, 2)
    as_of = D(2026, 2, 15)
    assert {r[0].pk for r in services.delinquent_lots(as_of=as_of)} == {lot.pk, w["other"].pk}
    _pay(w, 4500, D(2026, 2, 3), lot=w["other"])  # only February left: 14 days, not yet
    assert w["other"].pk not in {r[0].pk for r in services.delinquent_lots(as_of=as_of)}
    case = services.open_case(lot=lot, actor=w["manager"])
    with pytest.raises(services.LedgerError, match="already has an open"):
        services.open_case(lot=lot, actor=w["board"])
    _pdf, kind = acc.generate_case_letter(case, actor=w["manager"])
    assert kind == "first"
    with system_access("t"):
        assert CollectionLetter.objects.get(case=case).amount_cents == services.past_due(lot.pk)
    _pay(w, 9000, D(2026, 2, 20))
    assert lot.pk not in {r[0].pk for r in services.delinquent_lots()}
    with pytest.raises(WorkflowError):
        services.COLLECTION.apply(case, "close", actor=w["manager"])
    services.COLLECTION.apply(case, "close", actor=w["manager"], note="Paid in full")
    clean = services.open_case(lot=lot, actor=w["manager"])  # a new case may open later
    with pytest.raises(acc.AccountingError, match="not past due"):
        acc.generate_case_letter(clean, actor=w["manager"])
    _check(books)


def test_statement_and_resale_certificate_list_the_open_charges_and_total(w, books):
    lot = w["lot"]
    mar = _dues(2026, 3)[lot.pk]
    fine = _charge(w, 3000, "Gate fine", D(2026, 3, 9))
    _pay(w, 1000, D(2026, 3, 10), reference="44")
    numbers = {c.entry.pk: c.number for c in allocation.open_charges(lot.pk)}
    paras, balance = letters.statement_paragraphs(lot)
    text = "\n".join(t for _s, t in paras)
    assert balance == 6500 == services.balance(lot.pk)
    open_rows = [t for t in text.splitlines() if t.startswith(("2026-0", numbers[mar.pk]))]
    assert any(numbers[mar.pk] in r and "$45.00" in r and "$35.00" in r for r in open_rows)
    assert any(numbers[fine.pk] in r and "$30.00" in r for r in text.splitlines())
    assert "PAY THIS AMOUNT:" in text and "$65.00" in text
    resale = services.request_resale(lot=lot, requester_name="Agent", actor=w["board"])
    pdf = services.resale_certificate(resale)
    assert b"OPEN CHARGES" in pdf and numbers[fine.pk].encode() in pdf and b"65.00" in pdf
    _check(books)


@pytest.mark.xfail(
    strict=True,
    reason="BUG: delinquency notices state the whole balance as the 'delinquency', "
    "including charges not yet past due (notice_pdf uses balance, the case letter past_due)",
)
def test_delinquency_notice_states_only_the_past_due_amount(w, books):
    _dues(2026, 9)
    _charge(w, 1000, "Posted today", TODAY)
    case = services.open_case(lot=w["lot"], actor=w["manager"])
    rendered = []
    with mock.patch.object(letters.pdf, "render", side_effect=lambda p, **k: rendered.append(p)):
        acc.generate_case_letter(case, actor=w["manager"])
    with system_access("t"):
        recorded = CollectionLetter.objects.get(case=case).amount_cents
    _check(books)
    assert recorded == 4500
    text = "\n".join(t for _s, t in rendered[0])
    assert "delinquency of $45.00" in text


# --- 5. Corrections ------------------------------------------------------------------------


def test_reversing_a_payment_reopens_its_charges_and_credit_is_reapplied(w, books):
    c1 = _charge(w, 3000, "Old", D(2026, 1, 5))
    c2 = _charge(w, 2000, "New", D(2026, 2, 5))
    p1 = _pay(w, 2000, D(2026, 2, 6), reference="1", apply_to=[c2.pk])
    p2 = _pay(w, 5000, D(2026, 2, 7), reference="2")
    assert dict(allocation.applications_of(p2.pk)) == {c1.pk: 3000}
    assert allocation.unapplied_credit(w["lot"].pk) == 2000
    services.reverse_entry(p1, reason="Check returned", actor=w["board"])
    assert dict(allocation.applications_of(p2.pk)) == {c1.pk: 3000, c2.pk: 2000}
    assert _open(w["lot"]) == {} and allocation.unapplied_credit(w["lot"].pk) == 0
    assert services.balance(w["lot"].pk) == 0
    _check(books)


def test_reversing_a_paid_charge_turns_the_payment_into_credit(w, books):
    mistake = _charge(w, 3000, "Wrong home", D(2026, 1, 5))
    payment = _pay(w, 3000, D(2026, 1, 6))
    services.reverse_entry(mistake, reason="Charged in error", actor=w["board"])
    assert allocation.applications_of(payment.pk) == []
    assert allocation.unapplied_credit(w["lot"].pk) == 3000
    later = _charge(w, 3000, "Real charge", D(2026, 2, 5))
    assert allocation.charge_status(later) == "paid"
    assert allocation.unapplied_credit(w["lot"].pk) == 0 and services.balance(w["lot"].pk) == 0
    _check(books)


def test_a_reversal_cannot_be_reversed_and_an_entry_only_once(client, w, books):
    charge = _charge(w, 3000, "Fine", D(2026, 1, 5))
    reversal = services.reverse_entry(charge, reason="Waived", actor=w["board"])
    with pytest.raises(services.LedgerError, match="cannot itself be reversed"):
        services.reverse_entry(reversal, reason="Undo", actor=w["board"])
    with pytest.raises(services.LedgerError, match="already been reversed"):
        services.reverse_entry(charge, reason="Again", actor=w["board"])
    client.force_login(w["board"])
    url = reverse("lot_ledger_action", args=[w["lot"].pk, "reverse"])
    client.post(url, {"entry": reversal.pk, "reason": "Undo"})
    with system_access("t"):
        assert LedgerEntry.objects.filter(lot=w["lot"]).count() == 2
    assert services.balance(w["lot"].pk) == 0
    _check(books)


def test_bounced_check_reopens_charges_and_reconciles_as_a_returned_item(w, books):
    FeatureSetting.objects.create(key="bank_feeds", enabled=True)
    acc.save_setup(
        gl_accounts="", opening_cash_cents=100_000, opening_cash_on=ago(30), actor=w["board"]
    )
    charge = _charge(w, 4500, "Dues", ago(20))
    payment = _pay(w, 4500, ago(12), reference="2001")
    batch = acc.create_deposit(entry_ids=[payment.pk], deposited_on=ago(10), actor=w["manager"])
    services.reverse_entry(payment, reason="Returned NSF", actor=w["board"])
    assert allocation.charge_status(charge) == "open"
    (returned,) = acc.uncleared(TODAY)["returned"]
    assert returned.reverses_id == payment.pk
    conn = bank.add_file_account(display_name="Operating", actor=w["board"])
    bank.store_lines(
        conn,
        [
            TransactionInfo("dep", ago(9), 4500, "DEPOSIT"),
            TransactionInfo("nsf", TODAY, -4500, "RETURNED ITEM"),
        ],
        source="csv",
        actor=w["board"],
    )
    rows = {r["line"].bank_id: r["candidates"] for r in matching.suggestions()}
    assert [(c.kind, c.object_id) for c in rows["dep"]] == [("deposit", batch.pk)]
    assert [(c.kind, c.object_id) for c in rows["nsf"]] == [("returned", returned.pk)]
    for line_id, cand in rows.items():
        line = next(r["line"] for r in matching.suggestions() if r["line"].bank_id == line_id)
        matching.confirm(line, kind=cand[0].kind, object_id=cand[0].object_id, actor=w["board"])
    ticked = matching.matched_for_statement(TODAY)
    rec = acc.reconcile(
        statement_date=TODAY,
        statement_balance_cents=100_000,
        cleared_deposit_ids=ticked["deposit"],
        cleared_payment_ids=ticked["payment"],
        cleared_returned_ids=ticked["returned"],
        actor=w["manager"],
    )
    assert rec.difference_cents == 0 and rec.book_balance_cents == 100_000
    again = _pay(w, 4500, TODAY, reference="2001")  # the same check, deposited again
    assert allocation.charge_status(charge) == "paid" and again.pk
    _check(books)


# --- 10. Reports ---------------------------------------------------------------------------


def _vendor_payment(w, cents):
    invoice = Invoice.objects.create(
        vendor=Vendor.objects.get_or_create(name="Mow Co")[0],
        number=f"A{cents}",
        invoice_date=TODAY,
        amount_cents=cents,
        category=ExpenseCategory.objects.get_or_create(code="5100", name="Landscaping")[0],
        entered_by=w["manager"],
        state="paid",
    )
    return InvoicePayment.objects.create(
        invoice=invoice, paid_on=TODAY, method="check", amount_cents=cents, recorded_by=w["board"]
    )


def test_reports_aging_statement_journal_and_1099(client, w, books):
    _vendor_payment(w, 70_000)
    before = [(r["vendor"].pk, r["total_cents"]) for r in acc.vendor_1099_totals(TODAY.year)]
    _charge(w, 4500, "Dues", ago(40))
    _charge(w, 4500, "Dues B", ago(40), lot=w["other"])
    _pay(w, 2000, ago(5), reference="61")
    services.post_credit(lot=w["other"], amount_cents=500, description="Goodwill", actor=w["board"])
    assert [(r["vendor"].pk, r["total_cents"]) for r in acc.vendor_1099_totals(TODAY.year)] == (
        before
    )
    client.force_login(w["board"])
    page = client.get(reverse("collections"))
    assert page.status_code == 200 and str(w["lot"].number) in page.content.decode()
    client.force_login(w["owner"])
    assert client.get(reverse("collections")).status_code == 403
    pdf = client.get(reverse("my_statement_pdf", args=[w["lot"].pk]))
    assert pdf.status_code == 200 and pdf["Content-Type"] == "application/pdf"
    assert client.get(reverse("my_statement_pdf", args=[w["other"].pk])).status_code == 404
    data = acc.export_csv(ago(60), TODAY, actor=w["manager"]).decode()
    rows = list(csv.reader(io.StringIO(data)))[1:]
    assert sum(int(r[4].replace(".", "")) for r in rows if r[4]) == sum(
        int(r[5].replace(".", "")) for r in rows if r[5]
    )
    assert len(rows) == len(journal(ago(60), TODAY)) == 10  # 2 charges, payment, credit, vendor
    assert all(r[2] and r[3] for r in rows) and "Lot " in data
    _check(books)


def test_charges_numbers_and_titles_are_stable_through_payments(w, books):
    mar = _dues(2026, 3)[w["lot"].pk]
    (view,) = allocation.open_charges(w["lot"].pk)
    number = view.number
    _pay(w, 4500, D(2026, 3, 3))
    (paid,) = allocation.charges(w["lot"].pk, open_only=False)
    assert paid.entry.pk == mar.pk and paid.number == number and paid.status == "paid"
    assert paid.title == f"Dues {DASH} March 2026" and paid.payments[0].amount_cents == 4500
    assert days_ago(0) == TODAY
    _check(books)
