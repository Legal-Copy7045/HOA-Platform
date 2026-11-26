"""Reconciliation check (c): Stripe payouts must reach the bank (bank feed)."""

import datetime

import pytest
from django.utils import timezone

from community.tests.factories import make_member
from ledger.bankfeeds import services as bank
from ledger.models import BankTransaction
from payments.models import StripePayout
from payments.sync import check_payouts_against_bank

pytestmark = pytest.mark.django_db
TODAY = timezone.localdate()


def _payout(pid, cents, days_ago, status="paid"):
    return StripePayout.objects.create(
        payout_id=pid,
        amount_cents=cents,
        gross_cents=cents,
        arrival_date=TODAY - datetime.timedelta(days=days_ago),
        status=status,
    )


def _deposit(conn, n, cents, days_ago):
    BankTransaction.objects.create(
        connection=conn,
        bank_id=f"b{n}",
        posted_on=TODAY - datetime.timedelta(days=days_ago),
        amount_cents=cents,
        source="ofx",
    )


@pytest.fixture
def conn():
    return bank.add_file_account(display_name="Operating", actor=make_member(board=True))


def test_no_bank_feed_means_nothing_to_compare():
    assert check_payouts_against_bank([_payout("po_1", 5000, 10)]) == []


def test_payouts_match_deposits_of_the_same_net_amount_once_each(conn):
    _deposit(conn, 1, 9700, 9)  # one day after arrival: fine
    _deposit(conn, 2, 4850, 0)  # keeps the feed current
    payouts = [_payout("po_1", 9700, 10), _payout("po_2", 9700, 8)]
    # Only one 97.00 deposit: the second payout is missing from the bank.
    assert check_payouts_against_bank(payouts) == [{"code": "payout_not_in_bank", "id": "po_2"}]


def test_wrong_amount_or_too_far_away_is_a_problem(conn):
    _deposit(conn, 1, 9699, 10)  # a cent short
    _deposit(conn, 2, 5000, 1)  # 9 days after the second payout's arrival
    _deposit(conn, 3, 1, 0)
    problems = check_payouts_against_bank([_payout("po_a", 9700, 10), _payout("po_b", 5000, 10)])
    assert {p["id"] for p in problems} == {"po_a", "po_b"}


def test_only_dates_the_feed_covers_and_paid_payouts_are_judged(conn):
    _deposit(conn, 1, 100, 5)  # the feed only runs to five days ago
    payouts = [
        _payout("po_recent", 9700, 3),  # feed not imported that far: not judged
        _payout("po_failed", 9700, 20, status="failed"),  # never reached the bank
    ]
    assert check_payouts_against_bank(payouts) == []
