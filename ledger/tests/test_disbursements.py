"""Disbursements (D52): surplus distributions and owner refunds, their
approval, posting, checks, accounting and owner pages."""

import csv
import datetime
import io
from unittest import mock

import pytest
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, InternalError, ProgrammingError, connection, transaction
from django.urls import reverse
from django.utils import timezone

from community import services as community
from community.models import Account, Role
from community.tests.factories import LotFactory, PersonFactory, days_ago, make_member
from core.append_only import AppendOnlyError
from core.db_roles import append_only_tables
from core.policy import SYSTEM
from core.scoping import system_access
from ledger import accounting, services
from ledger import disbursements as disb
from ledger.models import (
    DisbursementBatch,
    DisbursementCheck,
    DisbursementCheckEvent,
    DisbursementDecision,
    DisbursementEntry,
    DisbursementLine,
    LedgerEntry,
    ReconciledItem,
    ReservePlanSettings,
    ReserveStudySnapshot,
)

pytestmark = pytest.mark.django_db
TODAY = timezone.localdate()


@pytest.fixture
def w():
    # Most tests here use the equal split with checks allowed; the D67 tests
    # at the end switch to the defaults (share paid, credits only).
    community.set_rule(
        key="disbursement.surplus_by_share_paid",
        value="false",
        citation="Declaration §8.4 (test)",
        actor=SYSTEM,
    )
    community.set_rule(
        key="disbursement.surplus_cash_allowed",
        value="true",
        citation="Board resolution (test)",
        actor=SYSTEM,
    )
    lots = [
        LotFactory(street_address="3 Bravo Way"),
        LotFactory(street_address="1 Alpha Way"),
        LotFactory(street_address="2 Alpha Way"),
    ]
    owners = [make_member(lots=[lot]) for lot in lots]
    # Board members are homeowners first: two own homes here, one does not.
    board = make_member(board=True)
    board_owner = make_member(board=True, lots=[LotFactory(street_address="4 Charlie Way")])
    board_owner2 = make_member(board=True, lots=[LotFactory(street_address="5 Charlie Way")])
    manager = make_member(roles=[Role.MANAGER])
    return {
        "lots": lots,
        "owners": owners,
        "board": board,
        "board_owner": board_owner,
        "board_owner2": board_owner2,
        "manager": manager,
    }


def _account(lot) -> Account:
    with system_access("t"):
        return Account.objects.active().get(lot=lot)


def _surplus(w, total=1000, by_check=(), actor=None):
    return disb.propose_surplus(
        total_cents=total,
        year=2026,
        resolution_ref="Resolution 2026-14",
        by_check=by_check,
        actor=actor or w["manager"],
    )


def _lines(batch):
    with system_access("t"):
        return list(DisbursementLine.objects.filter(batch=batch).select_related("lot", "account"))


def _sold_with_credit(w, credit=12345):
    """Lot 0's owner overpays, then sells: the dormant account keeps the credit."""
    lot = w["lots"][0]
    services.record_payment(
        lot=lot, amount_cents=credit, received_on=days_ago(10), reference="77", actor=w["board"]
    )
    old = _account(lot)
    buyer = PersonFactory()
    with mock.patch("community.tasks.notify_board.defer"):
        community.transfer_lot(lot, new_owners=[buyer], transfer_date=days_ago(2), actor=SYSTEM)
    with system_access("t"):
        old.refresh_from_db()
    assert not old.is_active
    return old


def _refund(w, account, amount=12345, **kw):
    kw.setdefault("forwarding_address", "9 Elsewhere Road\nSomewhere, PA 17000")
    return disb.propose_refund(account=account, amount_cents=amount, actor=w["manager"], **kw)


# --- Splitting a total ----------------------------------------------------------------------


def test_largest_remainder_adds_up_and_ties_go_to_the_earlier_position():
    assert disb.split_largest_remainder(1000, [1, 1, 1]) == [334, 333, 333]
    assert disb.split_largest_remainder(1001, [1, 1, 1]) == [334, 334, 333]
    assert disb.split_largest_remainder(100, [1, 2, 1]) == [25, 50, 25]
    assert disb.split_largest_remainder(10, [1, 1, 1, 1]) == [3, 3, 2, 2]
    for total in (0, 1, 7, 99_999, 4_400_001):
        for weights in ([1] * 44, [3, 1, 2], [5]):
            shares = disb.split_largest_remainder(total, weights)
            assert sum(shares) == total
            assert max(shares) - min(shares) <= max(1, total) if len(set(weights)) == 1 else True


def test_largest_remainder_refuses_nonsense():
    for weights in ([], [0, 0], [-1, 2]):
        with pytest.raises(disb.DisbursementError):
            disb.split_largest_remainder(100, weights)


def test_surplus_split_is_equal_per_home_with_the_remainder_in_address_order(w):
    batch = _surplus(w, total=1003)
    lines = sorted(_lines(batch), key=lambda x: disb.home_sort_key(x.lot))
    labels = [line.lot.label for line in lines]
    # Five homes with owners, in address order (street, then number).
    assert labels == ["1 Alpha Way", "2 Alpha Way", "3 Bravo Way", "4 Charlie Way", "5 Charlie Way"]
    assert [line.amount_cents for line in lines] == [201, 201, 201, 200, 200]
    assert sum(line.amount_cents for line in lines) == batch.total_cents == 1003
    # The same total always gives the same split.
    again = disb.surplus_split(1003)
    assert [r["amount_cents"] for r in again] == [201, 201, 201, 200, 200]


def test_surplus_split_needs_a_cent_per_home(w):
    with pytest.raises(disb.DisbursementError):
        disb.surplus_split(4)


def test_amount_in_words():
    assert disb.amount_in_words(123456) == "One thousand two hundred thirty-four and 56/100"
    assert disb.amount_in_words(5) == "Zero and 05/100"
    assert disb.amount_in_words(100_000_000) == "One million and 00/100"
    assert (
        disb.amount_in_words(2_001_119_00)
        == "Two million one thousand one hundred nineteen and 00/100"
    )
    with pytest.raises(disb.DisbursementError):
        disb.amount_in_words(-1)


# --- Proposing ------------------------------------------------------------------------------


def test_surplus_needs_a_resolution(w):
    with pytest.raises(disb.DisbursementError, match="resolution"):
        disb.propose_surplus(total_cents=1000, year=2026, actor=w["board"])
    from governance import services as governance

    resolution = governance.record_resolution(
        kind="resolution",
        title="Return the 2026 surplus",
        text="Resolved.",
        adopted_on=TODAY,
        actor=w["board"],
    )
    batch = disb.propose_surplus(
        total_cents=1000, year=2026, resolution=resolution, actor=w["board"]
    )
    assert batch.resolution_id == resolution.pk


def test_proposing_posts_nothing_and_is_audited(w):
    from audit.models import AuditEvent

    before = LedgerEntry.objects.unscoped("t").count()
    batch = _surplus(w)
    assert LedgerEntry.objects.unscoped("t").count() == before
    event = AuditEvent.objects.get(action="disbursement.proposed")
    assert event.metadata == {
        "batch_id": batch.pk,
        "kind": "surplus",
        "lines": 5,
        "amount_cents": 1000,
        "need": 1,
    }


def test_owners_cannot_propose(w):
    with pytest.raises(PermissionDenied):
        _surplus(w, actor=w["owners"][0])


def test_refund_is_never_more_than_the_credit(w):
    old = _sold_with_credit(w)
    with pytest.raises(disb.DisbursementError, match="available"):
        _refund(w, old, amount=12346)
    _refund(w, old, amount=12000)
    # Proposed refunds are reserved: only 3.45 is left to propose.
    with pytest.raises(disb.DisbursementError, match=r"\$3.45"):
        _refund(w, old, amount=346)


def test_refund_to_a_former_owner_needs_a_mailing_address(w):
    old = _sold_with_credit(w)
    with pytest.raises(disb.DisbursementError, match="forwarding address"):
        _refund(w, old, forwarding_address="")
    batch = _refund(w, old)
    (line,) = _lines(batch)
    assert line.method == "check" and line.address_source == "forwarding"
    assert "Elsewhere Road" in line.mail_to
    assert batch.title == "Refund to the former owner of 3 Bravo Way"


# --- Approval rules -----------------------------------------------------------------------


def test_the_proposer_never_approves(w):
    batch = _surplus(w, actor=w["board"])
    with pytest.raises(disb.DisbursementError, match="proposed"):
        disb.approve(batch, actor=w["board"])


def test_the_manager_prepares_but_never_approves_or_voids(w):
    batch = _surplus(w)
    with pytest.raises(PermissionDenied):
        disb.approve(batch, actor=w["manager"])
    with pytest.raises(PermissionDenied):
        disb.decline(batch, note="No", actor=w["manager"])


def test_one_approval_under_the_threshold_posts_the_credits(w):
    batch = _surplus(w, total=1000)
    state = disb.approve(batch, actor=w["board"])  # not an owner here
    assert state.status == "approved" and state.needed == 1
    with system_access("t"):
        links = list(DisbursementEntry.objects.filter(line__batch=batch).select_related("entry"))
    assert len(links) == 5
    entry = links[0].entry
    assert entry.kind == "credit" and entry.amount_cents < 0
    assert entry.description == "Surplus distribution credit \u2013 2026"
    assert sum(link.entry.amount_cents for link in links) == -1000


def test_second_approval_needed_at_the_threshold(w):
    community.set_rule(
        key="disbursement.second_approval_cents", value="1000", citation="Res. (t)", actor=SYSTEM
    )
    old = _sold_with_credit(w, credit=5000)
    batch = disb.propose_refund(
        account=old, amount_cents=1000, forwarding_address="9 Elsewhere Rd", actor=w["manager"]
    )
    assert batch.required_approvals == 2
    state = disb.approve(batch, actor=w["board"])
    assert state.status == "awaiting" and state.needed == 2
    with pytest.raises(disb.DisbursementError, match="already approved"):
        disb.approve(batch, actor=w["board"])
    assert disb.approve(batch, actor=w["board_owner"]).status == "approved"
    # Lowering the rule afterwards does not change a batch already proposed.
    community.set_rule(
        key="disbursement.second_approval_cents", value="999999", citation="Res. (t)", actor=SYSTEM
    )
    with system_access("t"):
        assert DisbursementBatch.objects.get(pk=batch.pk).required_approvals == 2


def test_recipients_may_approve_a_surplus_but_then_two_are_needed(w):
    batch = _surplus(w, total=1000)
    state = disb.approve(batch, actor=w["board_owner"])
    assert state.status == "awaiting" and state.needed == 2
    assert state.approvals[0].is_recipient is True
    assert not LedgerEntry.objects.unscoped("t").filter(kind="credit").exists()
    state = disb.approve(batch, actor=w["board_owner2"])
    assert state.status == "approved"
    assert [d.is_recipient for d in state.approvals] == [True, True]
    assert LedgerEntry.objects.unscoped("t").filter(kind="credit").count() == 5


def test_an_owner_of_the_refunded_account_cannot_approve_it(w):
    lot = w["lots"][0]
    services.record_payment(
        lot=lot, amount_cents=900, received_on=days_ago(3), reference="5", actor=w["board"]
    )
    community.record_ownership(
        person=w["board_owner"].person, lot=lot, start_date=days_ago(300), actor=SYSTEM
    )
    batch = _refund(w, _account(lot), amount=900)
    with pytest.raises(disb.DisbursementError, match="owner on the account"):
        disb.approve(batch, actor=w["board_owner"])
    assert disb.approve(batch, actor=w["board"]).status == "approved"


def test_declining_needs_a_reason_and_ends_the_batch(w):
    batch = _surplus(w)
    with pytest.raises(disb.DisbursementError, match="reason"):
        disb.decline(batch, note=" ", actor=w["board"])
    disb.decline(batch, note="Wait for the audit", actor=w["board"])
    assert disb.batch_state(batch).status == "declined"
    with pytest.raises(disb.DisbursementError):
        disb.approve(batch, actor=w["board_owner"])
    assert not LedgerEntry.objects.unscoped("t").filter(kind="credit").exists()


def test_refund_credit_is_checked_again_at_approval(w):
    old = _sold_with_credit(w)
    batch = _refund(w, old)
    # A charge posted on the dormant account since uses part of the credit.
    services.post_charge(
        lot=w["lots"][0],
        kind="other_charge",
        amount_cents=100,
        description="Final water bill",
        account_id=old.pk,
        actor=w["board"],
    )
    with pytest.raises(disb.DisbursementError, match="only"):
        disb.approve(batch, actor=w["board"])
    assert not DisbursementDecision.objects.filter(batch=batch).exists()


# --- Checks -------------------------------------------------------------------------------


def _approved_refund(w, amount=12345):
    old = _sold_with_credit(w, credit=12345)
    batch = _refund(w, old, amount=amount)
    disb.approve(batch, actor=w["board"])
    return old, batch


def test_refund_posts_when_the_check_is_issued(w):
    old, batch = _approved_refund(w)
    assert disb.account_credit(old) == 12345  # nothing posted on approval
    (check,) = disb.issue_checks(batch, first_number="1042", issued_on=TODAY, actor=w["manager"])
    assert check.number == "1042"
    entry = DisbursementEntry.objects.unscoped("t").get(check_record=check).entry
    assert entry.kind == "refund" and entry.amount_cents == 12345
    assert entry.account_id == old.pk and entry.description == "Refund by check #1042"
    assert disb.account_credit(old) == 0
    assert disb.line_status(_lines(batch)[0]) == "issued"
    with pytest.raises(disb.DisbursementError, match="no checks"):
        disb.issue_checks(batch, first_number="1043", issued_on=TODAY, actor=w["manager"])


def test_issue_checks_rechecks_the_credit(w):
    old, batch = _approved_refund(w)
    services.post_charge(
        lot=w["lots"][0],
        kind="other_charge",
        amount_cents=5,
        description="Fee",
        account_id=old.pk,
        actor=w["board"],
    )
    with pytest.raises(disb.DisbursementError, match="less than this check"):
        disb.issue_checks(batch, first_number="1042", issued_on=TODAY, actor=w["manager"])
    assert not DisbursementCheck.objects.unscoped("t").exists()


def test_check_numbers_are_unique_and_dates_sane(w):
    _old, batch = _approved_refund(w)
    with pytest.raises(disb.DisbursementError, match="digits"):
        disb.issue_checks(batch, first_number="10A", issued_on=TODAY, actor=w["manager"])
    with pytest.raises(disb.DisbursementError, match="future"):
        disb.issue_checks(
            batch,
            first_number="1",
            issued_on=TODAY + datetime.timedelta(days=1),
            actor=w["manager"],
        )
    batch2 = _surplus(w, total=1000, by_check=[w["lots"][1].pk])
    disb.approve(batch2, actor=w["board"])
    disb.issue_checks(batch2, first_number="0099", issued_on=TODAY, actor=w["manager"])
    with pytest.raises(disb.DisbursementError, match="0099"):
        disb.issue_checks(batch, first_number="0099", issued_on=TODAY, actor=w["manager"])


def test_checks_cannot_be_issued_before_approval(w):
    old = _sold_with_credit(w)
    batch = _refund(w, old)
    with pytest.raises(disb.DisbursementError, match="approved"):
        disb.issue_checks(batch, first_number="1", issued_on=TODAY, actor=w["board"])


def test_void_reverses_the_refund_and_reopens_the_credit(w):
    old, batch = _approved_refund(w)
    (check,) = disb.issue_checks(batch, first_number="1042", issued_on=TODAY, actor=w["manager"])
    with pytest.raises(PermissionDenied):
        disb.void_check(check, reason="Lost in the mail", actor=w["manager"])
    with pytest.raises(disb.DisbursementError, match="reason"):
        disb.void_check(check, reason="", actor=w["board"])
    event = disb.void_check(check, reason="Lost in the mail", actor=w["board"])
    assert event.reversal.kind == "reversal" and event.reversal.amount_cents == -12345
    assert event.reversal.account_id == old.pk
    assert disb.account_credit(old) == 12345
    assert disb.check_status(check) == "voided"
    (line,) = _lines(batch)
    assert disb.line_status(line) == "to_issue"
    (again,) = disb.issue_checks(batch, first_number="1043", issued_on=TODAY, actor=w["manager"])
    assert disb.account_credit(old) == 0 and disb.live_check(line) == again
    with pytest.raises(disb.DisbursementError):
        disb.void_check(check, reason="Twice", actor=w["board"])


def test_cleared_checks_cannot_be_voided(w):
    _old, batch = _approved_refund(w)
    (check,) = disb.issue_checks(batch, first_number="7", issued_on=TODAY, actor=w["manager"])
    disb.mark_cleared(check, on=TODAY, actor=w["manager"])
    assert disb.check_status(check) == "cleared"
    with pytest.raises(disb.DisbursementError):
        disb.void_check(check, reason="Too late", actor=w["board"])
    with pytest.raises(disb.DisbursementError):
        disb.mark_cleared(check, on=TODAY, actor=w["manager"])


def test_surplus_check_changes_no_owner_account(w):
    lot = w["lots"][1]
    batch = _surplus(w, total=1000, by_check=[lot.pk])
    before = services.balance(lot.pk)
    disb.approve(batch, actor=w["board"])
    disb.issue_checks(batch, first_number="500", issued_on=TODAY, actor=w["manager"])
    assert services.balance(lot.pk) == before
    # The other homes got statement credits.
    assert LedgerEntry.objects.unscoped("t").filter(kind="credit").count() == 4


# --- Append-only ------------------------------------------------------------------------------

APPEND_ONLY = [
    DisbursementBatch,
    DisbursementLine,
    DisbursementDecision,
    DisbursementEntry,
    DisbursementCheck,
    DisbursementCheckEvent,
]


def test_records_are_append_only_in_the_model(w):
    batch = _surplus(w)
    batch.title = "Changed"
    with pytest.raises(AppendOnlyError):
        batch.save()
    with pytest.raises(AppendOnlyError):
        batch.delete()
    with pytest.raises(AppendOnlyError):
        DisbursementBatch.objects.filter(pk=batch.pk).update(title="x")
    with pytest.raises(AppendOnlyError):
        DisbursementLine.objects.unscoped("t").filter(batch=batch).update(amount_cents=1)


def test_records_are_append_only_in_the_database(w):
    batch = _surplus(w)
    refused = (InternalError, IntegrityError, ProgrammingError)
    for table in ("ledger_disbursementbatch", "ledger_disbursementline"):
        with pytest.raises(refused), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(f"UPDATE {table} SET id = id WHERE id > 0")  # nosec B608
        with pytest.raises(refused), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(f"DELETE FROM {table}")  # nosec B608
    assert DisbursementBatch.objects.filter(pk=batch.pk).exists()


@pytest.mark.parametrize("model", APPEND_ONLY)
def test_runtime_role_cannot_change_disbursement_records(settings, model):
    table = model._meta.db_table
    assert table in append_only_tables()
    with connection.cursor() as cursor:
        for privilege in ("UPDATE", "DELETE", "TRUNCATE"):
            cursor.execute(
                "SELECT has_table_privilege(%s, %s, %s)", [settings.DB_APP_ROLE, table, privilege]
            )
            assert cursor.fetchone()[0] is False, privilege


# --- Accounting --------------------------------------------------------------------------------


def _journal(start=None, end=None):
    rows = accounting.journal_rows(start or TODAY - datetime.timedelta(days=400), end or TODAY)
    return [r for r in rows if r[1] == "Disbursements"]


def _balanced(rows):
    debit = sum(int(r[4].replace(".", "")) for r in rows if r[4])
    credit = sum(int(r[5].replace(".", "")) for r in rows if r[5])
    assert debit == credit
    return debit


def test_journal_lines_for_each_kind_balance(w):
    gl = accounting.AccountingSetup.get().accounts()
    assert gl["surplus_fund"] == "3100 Reserve and surplus fund"
    _old, refund = _approved_refund(w)
    (check,) = disb.issue_checks(refund, first_number="900", issued_on=TODAY, actor=w["manager"])
    batch = _surplus(w, total=1000, by_check=[w["lots"][1].pk])
    disb.approve(batch, actor=w["board"])
    disb.issue_checks(batch, first_number="500", issued_on=TODAY, actor=w["manager"])
    rows = _journal()
    _balanced(rows)
    credits = [r for r in rows if "surplus distribution credit" in r[6]]
    assert len(credits) == 8  # four homes, two lines each
    assert {r[3] for r in credits if r[4]} == {gl["surplus_fund"]}
    assert {r[3] for r in credits if r[5]} == {gl["receivable"]}
    surplus_check = [r for r in rows if "surplus check #500" in r[6]]
    assert [(r[3], r[4], r[5]) for r in surplus_check] == [
        (gl["surplus_fund"], "2.00", ""),
        (gl["cash"], "", "2.00"),
    ]
    refund_rows = [r for r in rows if "credit refunded by check" in r[6]]
    assert [(r[3], r[4], r[5]) for r in refund_rows] == [
        (gl["receivable"], "123.45", ""),
        (gl["cash"], "", "123.45"),
    ]
    # Voiding the refund check books it back.
    disb.void_check(check, reason="Returned", actor=w["board"])
    rows = _journal()
    _balanced(rows)
    voided = [r for r in rows if "credit refunded by check" in r[6]][2:]
    assert [(r[3], r[4], r[5]) for r in voided] == [
        (gl["cash"], "123.45", ""),
        (gl["receivable"], "", "123.45"),
    ]
    # The whole journal (owner payments and charges too) still balances.
    _balanced(accounting.journal_rows(TODAY - datetime.timedelta(days=400), TODAY))


def test_a_surplus_credit_moved_by_a_backdated_sale_keeps_its_journal_lines(w):
    gl = accounting.AccountingSetup.get().accounts()
    batch = _surplus(w, total=1000)
    disb.approve(batch, actor=w["board"])
    # The sale is recorded afterwards with an earlier date: the credit posted
    # today belongs to the buyer and is moved (reversal + copy, D34-fixmoney-1).
    with mock.patch("community.tasks.notify_board.defer"):
        community.transfer_lot(
            w["lots"][0], new_owners=[PersonFactory()], transfer_date=days_ago(3), actor=SYSTEM
        )
    assert LedgerEntry.objects.unscoped("t").filter(moved_from__isnull=False).count() == 1
    rows = _journal()
    _balanced(rows)
    fund = [r for r in rows if r[3] == gl["surplus_fund"]]
    net = sum(int(r[4].replace(".", "") or 0) for r in fund) - sum(
        int(r[5].replace(".", "") or 0) for r in fund
    )
    assert net == 1000  # moved, not paid twice or taken back
    assert not [r for r in accounting.journal_rows(TODAY, TODAY) if r[1] == "Owner credits"]


def test_issued_checks_are_outstanding_until_reconciled(w):
    from ledger.models import AccountingSetup

    setup = AccountingSetup.get()
    setup.opening_cash_cents = 100000
    setup.opening_cash_on = TODAY - datetime.timedelta(days=30)
    setup.save()
    _old, batch = _approved_refund(w)
    (check,) = disb.issue_checks(batch, first_number="1042", issued_on=TODAY, actor=w["manager"])
    book = accounting.book_balance(TODAY)
    open_items = accounting.uncleared(TODAY)
    assert [c.pk for c in open_items["checks"]] == [check.pk]
    # The statement does not show the check yet: it is outstanding.
    rec = accounting.reconcile(
        statement_date=TODAY,
        statement_balance_cents=book + 12345,
        cleared_deposit_ids=[],
        cleared_payment_ids=[],
        cleared_check_ids=[],
        actor=w["board"],
    )
    assert rec.outstanding_payments_cents >= 12345 and rec.difference_cents == 0
    # Marked cleared: ticked by default on the next statement.
    disb.mark_cleared(check, on=TODAY, actor=w["manager"])
    rec = accounting.reconcile(
        statement_date=TODAY,
        statement_balance_cents=book,
        cleared_deposit_ids=[d.pk for d in accounting.uncleared(TODAY)["deposits"]],
        cleared_payment_ids=[],
        actor=w["board"],
    )
    assert ReconciledItem.objects.filter(kind="check", object_id=check.pk).exists()
    assert accounting.uncleared(TODAY)["checks"] == []


def test_voided_checks_leave_the_cash_book(w):
    _old, batch = _approved_refund(w)
    before = accounting.book_balance(TODAY)
    (check,) = disb.issue_checks(batch, first_number="1042", issued_on=TODAY, actor=w["manager"])
    assert accounting.book_balance(TODAY) == before - 12345
    disb.void_check(check, reason="Wrong address", actor=w["board"])
    assert accounting.book_balance(TODAY) == before
    assert accounting.uncleared(TODAY)["checks"] == []


def _bank_line(w, amount, description, on=TODAY):
    from ledger.bankfeeds import services as bank_feed
    from ledger.models import BankConnection, BankTransaction

    connection_ = BankConnection.objects.first() or bank_feed.add_file_account(
        display_name="Operating", actor=w["board"]
    )
    return BankTransaction.objects.create(
        connection=connection_,
        bank_id=f"b{amount}{description}",
        posted_on=on,
        amount_cents=amount,
        description=description,
        source="csv",
    )


def test_bank_feed_suggests_clearing_by_amount_and_check_number(w):
    from ledger.bankfeeds import matching

    _old, batch = _approved_refund(w)
    (check,) = disb.issue_checks(batch, first_number="1042", issued_on=TODAY, actor=w["manager"])
    _bank_line(w, -12345, "CHECK 1041")  # wrong number
    _bank_line(w, -12000, "CHECK 1042")  # wrong amount
    right = _bank_line(w, -12345, "CHECK 1042 PAID")
    assert disb.clearing_suggestions([check]) == {check.pk: right}
    found = [
        c for row in matching.suggestions() if row["line"].pk == right.pk for c in row["candidates"]
    ]
    assert [(c.kind, c.object_id) for c in found] == [("check", check.pk)]
    match = matching.confirm(right, kind="check", object_id=check.pk, actor=w["board"])
    assert match.kind == "check"
    assert check.pk in matching.matched_for_statement(TODAY)["check"]
    disb.mark_cleared(check, on=TODAY, bank_transaction=right, actor=w["manager"])
    assert disb.check_status(check) == "cleared"


# --- My items and the reserve prompt -----------------------------------------------------------


def _titles(user):
    from core.my_items import items_for

    return [i.title for i in items_for(user)]


def test_sale_with_credit_gives_the_board_a_refund_item(w):
    _sold_with_credit(w)
    assert "Refund $123.45 to the former owner of 3 Bravo Way" in _titles(w["board"])
    assert "Refund $123.45 to the former owner of 3 Bravo Way" in _titles(w["manager"])
    assert not any("Refund" in t for t in _titles(w["owners"][1]))


def test_refund_item_goes_away_once_proposed(w):
    old = _sold_with_credit(w)
    _refund(w, old)
    assert not any(t.startswith("Refund $") for t in _titles(w["board"]))
    assert any(t.startswith("Disbursement: Refund") for t in _titles(w["board"]))
    # The manager proposed it and cannot approve: no approval item for them.
    assert not any(t.startswith("Disbursement:") for t in _titles(w["manager"]))


def test_approved_checks_to_write_and_stale_checks_are_items(w):
    _old, batch = _approved_refund(w)
    assert any(t.startswith("1 check to write") for t in _titles(w["manager"]))
    disb.issue_checks(
        batch,
        first_number="1042",
        issued_on=TODAY,
        actor=w["manager"],
    )
    assert not any("check to write" in t for t in _titles(w["manager"]))
    later = TODAY + datetime.timedelta(days=disb.STALE_DAYS + 1)
    assert disb.stale_checks(TODAY) == []
    assert [c.number for c in disb.stale_checks(later)] == ["1042"]


def _reserves_above_target(w, balance=150000, target=100000):
    plan = ReservePlanSettings.get()
    plan.starting_balance_cents = balance
    plan.balance_as_of = TODAY - datetime.timedelta(days=5)
    plan.save()
    ReserveStudySnapshot.objects.create(
        adopted_by=w["board"],
        adopted_on=TODAY - datetime.timedelta(days=20),
        start_year=TODAY.year,
        horizon_years=30,
        starting_balance_cents=balance,
        annual_contribution_cents=0,
        inflation_bp=300,
        interest_bp=100,
        fully_funded_cents=target,
        needed_contribution_cents=0,
        total_expense_cents=0,
        lowest_end_cents=0,
        lowest_year=TODAY.year,
    )


def test_reserves_above_target_prompt_a_surplus_distribution(w, client):
    assert disb.reserve_surplus() is None
    _reserves_above_target(w)
    surplus = disb.reserve_surplus()
    assert surplus["above_cents"] == 50000
    assert "Consider a surplus distribution" in _titles(w["board"])
    client.force_login(w["board"])
    page = client.get(reverse("disbursements")).content.decode()
    assert "Reserves are $500.00 above target" in page
    # Once proposed, the prompt goes away.
    _surplus(w, total=50000)
    assert "Consider a surplus distribution" not in _titles(w["board"])
    assert "Consider a surplus distribution" not in _titles(w["owners"][0])


def test_no_prompt_without_an_adopted_plan_but_manual_surplus_allowed(w):
    plan = ReservePlanSettings.get()
    plan.starting_balance_cents = 999999
    plan.save()
    assert disb.reserve_surplus() is None
    assert "Consider a surplus distribution" not in _titles(w["board"])
    assert _surplus(w).pk


# --- Owner pages ------------------------------------------------------------------------------


def test_owner_sees_own_statement_credit_not_others(w, client):
    batch = _surplus(w, total=1000)
    disb.approve(batch, actor=w["board"])
    client.force_login(w["owners"][1])  # 1 Alpha Way: 200 cents
    page = client.get(reverse("my_account")).content.decode()
    assert "Surplus distribution credit \u2013 2026" in page
    assert "$2.00" in page
    assert "3 Bravo Way" not in page


def test_former_owner_sees_the_refund_check_in_my_history(w, client):
    _old, batch = _approved_refund(w)
    disb.issue_checks(batch, first_number="1042", issued_on=TODAY, actor=w["manager"])
    client.force_login(w["owners"][0])
    page = client.get(reverse("my_history")).content.decode()
    assert "Refund of $123.45 by check #1042 sent" in page
    assert "9 Elsewhere Road" in page
    assert "Refund by check #1042" in page  # the account line
    # Another owner sees nothing of it.
    client.force_login(w["owners"][1])
    page = client.get(reverse("my_account")).content.decode()
    assert "1042" not in page and "Elsewhere" not in page


# --- Pages, cross-owner and feature switch -------------------------------------------------------


def _pages(batch, check=None):
    pages = [
        ("get", reverse("disbursements")),
        ("get", reverse("disbursement_new") + "?kind=surplus"),
        ("get", reverse("disbursement_detail", args=[batch.pk])),
        ("get", reverse("disbursement_register", args=[batch.pk]) + "?format=csv"),
        ("get", reverse("disbursement_register", args=[batch.pk]) + "?format=pdf"),
        ("get", reverse("disbursement_checks_pdf", args=[batch.pk])),
        ("get", reverse("check_print_settings")),
        ("get", reverse("check_alignment_pdf")),
    ]
    for action in ("approve", "decline", "issue", "clear", "void"):
        pages.append(("post", reverse("disbursement_action", args=[batch.pk, action])))
    return pages


def test_owners_get_no_disbursement_page(w, client):
    old, batch = _approved_refund(w)
    client.force_login(w["owners"][1])
    for method, url in _pages(batch):
        response = getattr(client, method)(url, {"check": 1, "note": "x", "reason": "x"})
        assert response.status_code in (403, 404), url
    response = client.get(reverse("disbursement_new") + f"?kind=refund&account={old.pk}")
    assert response.status_code == 403
    assert disb.batch_state(batch).status == "approved"


def test_feature_off_answers_404(w, client):
    from core import features

    batch = _surplus(w)
    features.set_feature("disbursements", False, actor=w["board"])
    client.force_login(w["board"])
    for method, url in _pages(batch):
        assert getattr(client, method)(url).status_code == 404, url
    assert not any("Disbursement" in t for t in _titles(w["board_owner"]))


def test_board_pages_load_and_the_flow_works_through_the_pages(w, client):
    old = _sold_with_credit(w)
    client.force_login(w["manager"])
    response = client.get(reverse("disbursement_new") + f"?kind=refund&account={old.pk}")
    assert response.status_code == 200 and "123.45" in response.content.decode()
    response = client.post(
        reverse("disbursement_new"),
        {
            "kind": "refund",
            "account": old.pk,
            "amount_cents": "123.45",
            "payee_name": "Former Owner",
            "forwarding_address": "9 Elsewhere Road",
        },
    )
    batch = DisbursementBatch.objects.get()
    assert response["Location"] == reverse("disbursement_detail", args=[batch.pk])
    client.force_login(w["board"])
    page = client.get(reverse("disbursement_detail", args=[batch.pk])).content.decode()
    assert "Approve" in page and "Former Owner" in page
    client.post(reverse("disbursement_action", args=[batch.pk, "approve"]), {"note": ""})
    assert disb.batch_state(batch).status == "approved"
    response = client.get(reverse("disbursement_checks_pdf", args=[batch.pk]))
    assert response["Content-Type"] == "application/pdf"
    client.post(
        reverse("disbursement_action", args=[batch.pk, "issue"]),
        {"first_number": "2001", "issued_on": TODAY.isoformat()},
    )
    check = DisbursementCheck.objects.unscoped("t").get()
    assert check.number == "2001"
    # A refused step re-renders the page with the reason (no redirect).
    response = client.post(
        reverse("disbursement_action", args=[batch.pk, "void"]), {"check": check.pk, "reason": ""}
    )
    assert response.status_code == 200 and "reason" in response.content.decode()
    for name in ("disbursements", "check_print_settings"):
        assert client.get(reverse(name)).status_code == 200
    export = client.get(reverse("disbursement_register", args=[batch.pk]))
    rows = list(csv.reader(io.StringIO(export.content.decode())))
    assert rows[0] == disb.REGISTER_HEADER
    assert rows[1][1] == "Former Owner" and rows[1][3] == "123.45" and rows[1][8] == "2001"


def test_surplus_form_previews_then_proposes(w, client):
    client.force_login(w["board"])
    data = {"kind": "surplus", "total_cents": "10.03", "year": "2026", "resolution_ref": "Res 9"}
    page = client.post(reverse("disbursement_new"), {**data, "action": "preview"})
    assert page.status_code == 200 and "$2.01" in page.content.decode()
    assert not DisbursementBatch.objects.exists()
    response = client.post(reverse("disbursement_new"), {**data, "action": "propose"})
    assert response.status_code == 302
    assert DisbursementBatch.objects.get().total_cents == 1003
    # Without a resolution the bound form comes back with the error.
    page = client.post(
        reverse("disbursement_new"), {**data, "resolution_ref": "", "action": "propose"}
    )
    assert page.status_code == 200 and "resolution" in page.content.decode()


def test_check_pdf_escapes_hostile_payee_names(w):
    old = _sold_with_credit(w)
    hostile = "Evil) Tj ET BT /F1 99 Tf (pwn"
    batch = _refund(w, old, payee_name=hostile, forwarding_address="1 Way) Tj (x\nTown")
    disb.approve(batch, actor=w["board"])
    data = disb.checks_pdf(batch, on=TODAY, actor=w["manager"])
    assert data.startswith(b"%PDF-1.4")
    assert b"Evil\\) Tj ET BT /F1 99 Tf \\(pwn" in data
    assert b"Evil) Tj" not in data and b"Way) Tj" not in data
    register = disb.register_pdf(batch, actor=w["board"])
    assert b"Evil) Tj" not in register


def test_register_csv_is_formula_safe_and_audited(w):
    from audit.models import AuditEvent

    old = _sold_with_credit(w)
    batch = _refund(w, old, payee_name="=HYPERLINK(1)")
    rows = list(csv.reader(io.StringIO(disb.register_csv(batch, actor=w["board"]).decode())))
    assert rows[1][1] == "'=HYPERLINK(1)"
    event = AuditEvent.objects.get(action="disbursement.register_exported")
    assert event.metadata == {"batch_id": batch.pk, "rows": 1, "format": "csv"}


def test_print_settings_and_alignment_test(w, client):
    client.force_login(w["manager"])
    values = {
        f.name: getattr(disb.CheckPrintSettings.get(), f.name)
        for f in disb.CheckPrintSettings._meta.fields
        if f.name.endswith(("_x", "_y"))
    }
    values["nudge_x"] = 12
    assert client.post(reverse("check_print_settings"), values).status_code == 302
    assert disb.CheckPrintSettings.get().nudge_x == 12
    values["nudge_x"] = 500
    page = client.post(reverse("check_print_settings"), values)
    assert page.status_code == 200 and disb.CheckPrintSettings.get().nudge_x == 12
    pdf = client.get(reverse("check_alignment_pdf"))
    assert pdf["Content-Type"] == "application/pdf"
    assert b"SAMPLE PAYEE NAME" in pdf.content


def test_audit_metadata_has_no_names_or_addresses(w):
    from audit.models import AuditEvent

    _old, batch = _approved_refund(w)
    (check,) = disb.issue_checks(batch, first_number="1042", issued_on=TODAY, actor=w["manager"])
    disb.void_check(check, reason="Lost", actor=w["board"])
    text = str(
        list(
            AuditEvent.objects.filter(action__startswith="disbursement.").values_list(
                "metadata", flat=True
            )
        )
    )
    assert "Elsewhere" not in text and "Test Person" not in text
    actions = set(
        AuditEvent.objects.filter(action__startswith="disbursement.").values_list(
            "action", flat=True
        )
    )
    assert {
        "disbursement.proposed",
        "disbursement.approval_given",
        "disbursement.approved",
        "disbursement.check_issued",
        "disbursement.check_voided",
    } <= actions


def test_pa_guidance_cites_the_surplus_funds_section():
    from community import pa_law

    assert "§ 5313" in pa_law.GROUP_NOTES["Disbursements"]
    assert "counsel" in pa_law.guide("disbursement.surplus_by_share_paid").law.lower()
    assert "§8.10" in pa_law.guide("disbursement.surplus_cash_allowed").example


# --- D66: payees and who processes a payment ------------------------------------------------


def _home(w, key):
    with system_access("t"):
        return w[key].person.ownerships.select_related("lot").first().lot


def _co_owner_removed(w):
    """Lot 0 is owned jointly; the second owner is taken off the deed and
    moves away, the first keeps the home (the account stays open)."""
    lot = w["lots"][0]
    removed = PersonFactory()
    with system_access("t"):
        own = community.record_ownership(
            person=removed, lot=lot, start_date=days_ago(300), actor=SYSTEM
        )
    with mock.patch("community.tasks.notify_board.defer"):
        community.end_ownership(own, end_date=days_ago(5), actor=SYSTEM)
    services.record_payment(
        lot=lot, amount_cents=900, received_on=days_ago(3), reference="5", actor=w["board"]
    )
    return lot, removed


def test_refund_payee_leaves_out_a_co_owner_removed_from_the_deed(w):
    lot, removed = _co_owner_removed(w)
    account = _account(lot)
    assert account.is_active
    defaults = disb.refund_defaults(account)
    assert removed.display_name not in defaults["payee_name"]
    assert defaults["payee_name"] == w["owners"][0].person.display_name
    batch = disb.propose_refund(account=account, amount_cents=900, actor=w["manager"])
    (line,) = _lines(batch)
    assert removed.display_name not in line.payee_name


def test_refund_payee_after_a_sale_is_the_owners_at_the_sale(w):
    old = _sold_with_credit(w)
    defaults = disb.refund_defaults(old)
    assert defaults["payee_name"] == w["owners"][0].person.display_name


def _board_seller(w, credit=5000):
    """A board member sells their home with a credit left on the account."""
    lot = _home(w, "board_owner")
    services.record_payment(
        lot=lot, amount_cents=credit, received_on=days_ago(10), reference="8", actor=w["board"]
    )
    old = _account(lot)
    with mock.patch("community.tasks.notify_board.defer"):
        community.transfer_lot(
            lot, new_owners=[PersonFactory()], transfer_date=days_ago(2), actor=SYSTEM
        )
    with system_access("t"):
        old.refresh_from_db()
    assert not old.is_active
    return old


def test_a_seller_on_the_board_takes_no_step_on_their_own_refund(w):
    seller = w["board_owner"]
    old = _board_seller(w)
    with pytest.raises(disb.DisbursementError, match="owner on this account"):
        disb.propose_refund(
            account=old, amount_cents=5000, forwarding_address="9 Far Road", actor=seller
        )
    batch = disb.propose_refund(
        account=old, amount_cents=5000, forwarding_address="9 Far Road", actor=w["manager"]
    )
    with pytest.raises(disb.DisbursementError, match="were an owner"):
        disb.decline(batch, note="No", actor=seller)
    with pytest.raises(disb.DisbursementError, match="were an owner"):
        disb.approve(batch, actor=seller)
    disb.approve(batch, actor=w["board"])
    for step in (
        lambda: disb.issue_checks(batch, first_number="500", issued_on=TODAY, actor=seller),
        lambda: disb.checks_pdf(batch, on=TODAY, actor=seller),
    ):
        with pytest.raises(disb.DisbursementError, match="were an owner"):
            step()
    (check,) = disb.issue_checks(batch, first_number="500", issued_on=TODAY, actor=w["board"])
    with pytest.raises(disb.DisbursementError, match="were an owner"):
        disb.void_check(check, reason="Lost", actor=seller)
    with pytest.raises(disb.DisbursementError, match="were on the deed"):
        disb.mark_cleared(check, on=TODAY, actor=seller)
    assert disb.check_status(check) == "issued"
    from audit.models import AuditEvent

    assert AuditEvent.objects.filter(action="policy.own_matter_refused").count() >= 6


def test_a_payment_to_a_board_member_is_processed_by_another_board_member(w):
    lot = _home(w, "board_owner")
    services.record_payment(
        lot=lot, amount_cents=700, received_on=days_ago(3), reference="9", actor=w["board"]
    )
    batch = disb.propose_refund(account=_account(lot), amount_cents=700, actor=w["manager"])
    disb.approve(batch, actor=w["board"])
    with pytest.raises(disb.DisbursementError, match="pays a board member"):
        disb.issue_checks(batch, first_number="600", issued_on=TODAY, actor=w["manager"])
    with pytest.raises(disb.DisbursementError, match="pays a board member"):
        disb.checks_pdf(batch, on=TODAY, actor=w["manager"])
    (check,) = disb.issue_checks(batch, first_number="600", issued_on=TODAY, actor=w["board"])
    with pytest.raises(disb.DisbursementError, match="pays a board member"):
        disb.mark_cleared(check, on=TODAY, actor=w["manager"])
    disb.mark_cleared(check, on=TODAY, actor=w["board_owner2"])
    assert disb.check_status(check) == "cleared"


def test_surplus_checks_to_board_members_are_left_for_another_board_member(w):
    homes = [*w["lots"]]
    for key in ("board_owner", "board_owner2"):
        homes.append(_home(w, key))
    batch = _surplus(w, total=1000, by_check=[lot.pk for lot in homes])
    disb.approve(batch, actor=w["board"])
    # The manager writes the owners' checks; the board members' wait.
    made = disb.issue_checks(batch, first_number="700", issued_on=TODAY, actor=w["manager"])
    assert {c.line.lot.label for c in made} == {lot.label for lot in w["lots"]}
    mine, left = disb.lines_for_actor(w["board_owner"], batch)
    assert [line.lot for line in mine] == [_home(w, "board_owner2")]
    assert [line.lot for line in left] == [_home(w, "board_owner")]
    (own,) = left
    with pytest.raises(disb.DisbursementError, match="you are or were on the deed"):
        disb.issue_checks(
            batch, first_number="800", issued_on=TODAY, line_ids=[own.pk], actor=w["board_owner"]
        )
    made = disb.issue_checks(batch, first_number="800", issued_on=TODAY, actor=w["board_owner"])
    assert [c.line_id for c in made] == [mine[0].pk]
    made = disb.issue_checks(batch, first_number="900", issued_on=TODAY, actor=w["board"])
    assert [c.line_id for c in made] == [own.pk]


def test_the_disbursement_page_hides_steps_on_checks_one_may_not_process(w, client):
    lot = _home(w, "board_owner")
    services.record_payment(
        lot=lot, amount_cents=700, received_on=days_ago(3), reference="9", actor=w["board"]
    )
    batch = disb.propose_refund(account=_account(lot), amount_cents=700, actor=w["manager"])
    disb.approve(batch, actor=w["board"])
    client.force_login(w["manager"])
    page = client.get(reverse("disbursement_detail", args=[batch.pk])).content.decode()
    assert 'data-testid="left-for-others"' in page and "pays a board member" in page
    assert "Mark as issued" not in page


def test_a_board_member_who_sold_their_home_gets_a_resign_reminder(w):
    from core.my_items import items_for

    _board_seller(w)
    titles = [i.title for i in items_for(w["board"])]
    name = w["board_owner"].person.display_name
    assert f"End {name}'s board seat" in titles
    # The board member without a home here (declarant-style test fixture)
    # also shows: an owner-elected seat needs a home.
    assert all("board seat" not in t for t in (i.title for i in items_for(w["owners"][1])))


# --- D67: surplus credited in proportion to what was paid ------------------------------------


def _declaration_rules(*, with_home=True, cash=False):
    """The defaults (share paid, credit stays with the home, credits only),
    or the variants a board may choose."""
    for key, value in (
        ("disbursement.surplus_by_share_paid", "true"),
        ("disbursement.surplus_stays_with_home", "true" if with_home else "false"),
        ("disbursement.surplus_cash_allowed", "true" if cash else "false"),
    ):
        community.set_rule(key=key, value=value, citation="Declaration §8.10 (test)", actor=SYSTEM)


_PERIOD = iter(range(1, 10_000))


def _fy_day():
    """A day in this fiscal year after the test homes' accounts opened."""
    return max(disb.fiscal_year_range(TODAY.year)[0], days_ago(200))


def _assessment(lot, cents, on=None):
    with system_access("t"):
        return LedgerEntry.objects.create(
            lot=lot,
            posted_on=on or _fy_day(),
            kind="assessment",
            period=f"2099-t{next(_PERIOD)}",
            amount_cents=cents,
            description="Assessment (test)",
        )


def _paid(w, lot, cents, ref):
    services.record_payment(
        lot=lot, amount_cents=cents, received_on=_fy_day(), reference=ref, actor=w["board"]
    )


def _sell(lot, on=None):
    with mock.patch("community.tasks.notify_board.defer"):
        community.transfer_lot(
            lot, new_owners=[PersonFactory()], transfer_date=on or days_ago(1), actor=SYSTEM
        )


def test_defaults_are_share_paid_stays_with_home_and_credits_only():
    from community.rules import rule

    assert rule("disbursement.surplus_by_share_paid") is True
    assert rule("disbursement.surplus_stays_with_home") is True
    assert rule("disbursement.surplus_cash_allowed") is False


def test_surplus_is_split_by_what_was_paid_that_year(w):
    _declaration_rules()
    a, b, c = w["lots"]
    for lot in (a, b, c):
        _assessment(lot, 4000)
    _paid(w, a, 4000, "1")  # paid in full
    _paid(w, b, 2000, "2")  # paid half; c paid nothing
    paid = {acct.lot_id: cents for acct, cents in disb.assessments_paid(TODAY.year)}
    assert paid[a.pk] == 4000 and paid[b.pk] == 2000 and c.pk not in paid
    lines = {p["lot"].pk: p for p in disb.surplus_split(600, year=TODAY.year)}
    assert lines[a.pk]["amount_cents"] == 400 and lines[b.pk]["amount_cents"] == 200
    assert c.pk not in lines
    assert all(p["method"] == "credit" for p in lines.values())


def test_credits_only_refuses_checks(w):
    _declaration_rules()
    lot = w["lots"][0]
    _assessment(lot, 4000)
    _paid(w, lot, 4000, "1")
    with pytest.raises(disb.DisbursementError, match="statement credits only"):
        disb.surplus_split(100, year=TODAY.year, by_check=[lot.pk])


def test_a_credit_that_stays_with_the_home_goes_to_the_buyer(w):
    _declaration_rules()
    lot = w["lots"][0]
    _assessment(lot, 4000)
    _paid(w, lot, 4000, "1")  # paid by the seller
    seller_account = _account(lot)
    _sell(lot)
    buyer_account = _account(lot)
    batch = disb.propose_surplus(
        total_cents=1000, year=TODAY.year, resolution_ref="Resolution 2026-20", actor=w["manager"]
    )
    (line,) = _lines(batch)
    assert line.account_id == buyer_account.pk and line.method == "credit"
    disb.approve(batch, actor=w["board"])
    assert disb.account_credit(buyer_account) == 1000
    assert disb.account_credit(seller_account) == 0


def test_a_credit_that_follows_the_owner_goes_to_the_sellers_old_account(w):
    _declaration_rules(with_home=False)
    lot = w["lots"][0]
    _assessment(lot, 4000)
    _paid(w, lot, 4000, "1")
    old = _account(lot)
    _sell(lot)
    batch = disb.propose_surplus(
        total_cents=1000, year=TODAY.year, resolution_ref="Resolution 2026-20", actor=w["manager"]
    )
    (line,) = _lines(batch)
    assert line.account_id == old.pk and line.method == "credit"
    disb.approve(batch, actor=w["board"])
    assert disb.account_credit(old) == 1000
    # The board can then refund it (Disbursements: refunds owed to former owners).
    assert any(r["account"].pk == old.pk for r in disb.dormant_credits())


def test_a_sellers_share_is_a_check_when_it_follows_the_owner_and_checks_are_allowed(w):
    _declaration_rules(with_home=False, cash=True)
    lot = w["lots"][0]
    _assessment(lot, 4000)
    _paid(w, lot, 4000, "1")
    _sell(lot)
    (line,) = disb.surplus_split(1000, year=TODAY.year)
    assert line["former"]
    # A check when the seller has a mailing address on file, else a credit.
    assert line["method"] == ("check" if line["mail_to"] else "credit")


def test_a_home_sold_between_proposal_and_approval(w):
    """Stays with the home: the credit goes to the buyer's account. Follows
    the owner: to the seller's old account."""
    batch = _surplus(w)  # the fixture's equal split; the credit stays with the home
    _sell(w["lots"][1], on=TODAY)
    disb.approve(batch, actor=w["board"])
    with system_access("t"):
        credit = LedgerEntry.objects.get(kind="credit", lot=w["lots"][1])
    assert credit.account_id == _account(w["lots"][1]).pk


def test_the_proposal_page_offers_no_checks_with_credits_only(w, client):
    _declaration_rules()
    client.force_login(w["manager"])
    page = client.get(reverse("disbursement_new") + "?kind=surplus").content.decode()
    assert 'data-testid="by-check"' not in page
    assert "statement credits only" in page and "stays with the home" in page
    assert "Declaration §8.10 (test)" in page


def test_the_wizard_has_a_surplus_step_with_every_disbursement_rule():
    from community import setup

    step = next(s for s in setup.STEPS if s.key == "surplus")
    keys = {spec.key for spec in setup.assign_rules()["surplus"]}
    assert {
        "disbursement.surplus_by_share_paid",
        "disbursement.surplus_stays_with_home",
        "disbursement.surplus_cash_allowed",
        "disbursement.second_approval_cents",
    } <= keys
    assert "disbursements" in step.features
