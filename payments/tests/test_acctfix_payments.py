# ruff: noqa: F811  (the scenario fixtures are imported, then used as arguments)
"""Fixes for the payment QA findings (D33-acctfix-5 and -6): further tests
beside the scenarios in test_payment_scenarios.py, using their fixtures."""

import datetime

import pytest
from django.urls import reverse

from ledger import accounting as acc
from ledger.bankfeeds import matching
from ledger.bankfeeds.base import TransactionInfo
from ledger.models import ReconciledItem
from payments.models import StripePayout
from payments.tests.test_payment_scenarios import (  # noqa: F401  (fixtures)
    TODAY,
    _bank_feed,
    _check,
    _paid_and_paid_out,
    books,
    fake,
    w,
)

pytestmark = pytest.mark.django_db


def _setup_cash(w):
    acc.save_setup(
        gl_accounts="",
        opening_cash_cents=100_000,
        opening_cash_on=TODAY - datetime.timedelta(days=30),
        actor=w["board"],
    )


# --- 6. Stripe payouts in the bank reconciliation ---------------------------------------------


def test_payout_is_a_book_deposit_on_its_arrival_date_and_never_counted_twice(client, w, books):
    _setup_cash(w)
    row = _paid_and_paid_out(client, w)
    assert acc.book_balance(TODAY - datetime.timedelta(days=1)) == 100_000
    assert acc.book_balance(TODAY) == 100_000 + row["amount_cents"]  # once: no deposit batch
    (item,) = acc.uncleared(TODAY)["payouts"]
    assert item.amount_cents == row["amount_cents"] and item.stripe_says_arrived
    assert acc.uncleared(TODAY)["deposits"] == []
    _check(books, synced=True)


def test_payout_matched_to_its_bank_line_then_cleared_by_the_statement(client, w, books):
    _setup_cash(w)
    row = _paid_and_paid_out(client, w)
    later = TODAY + datetime.timedelta(days=2)
    _bank_feed(w, [TransactionInfo("po", later, row["amount_cents"], "STRIPE TRANSFER")])
    (suggested,) = matching.suggestions()
    (cand,) = suggested["candidates"]
    assert cand.kind == "payout" and cand.amount_cents == row["amount_cents"]
    assert cand.days_apart == 2
    matching.confirm(suggested["line"], kind="payout", object_id=cand.object_id, actor=w["board"])
    ticked = matching.matched_for_statement(later)
    assert ticked["payout"] == {cand.object_id}
    rec = acc.reconcile(
        statement_date=later,
        statement_balance_cents=100_000 + row["amount_cents"],
        cleared_deposit_ids=[],
        cleared_payment_ids=[],
        cleared_payout_ids=ticked["payout"],
        actor=w["board"],
    )
    assert rec.difference_cents == 0 and rec.outstanding_deposits_cents == 0
    assert ReconciledItem.objects.get(reconciliation=rec).kind == "payout"
    assert acc.uncleared(later)["payouts"] == []  # cleared once, never again
    _check(books, synced=True)


def test_payout_not_yet_on_the_statement_stays_outstanding(client, w, books):
    _setup_cash(w)
    row = _paid_and_paid_out(client, w)
    rec = acc.reconcile(
        statement_date=TODAY,
        statement_balance_cents=100_000,  # the bank has not shown it yet
        cleared_deposit_ids=[],
        cleared_payment_ids=[],
        cleared_payout_ids=[],
        actor=w["board"],
    )
    assert rec.difference_cents == 0 and rec.outstanding_deposits_cents == row["amount_cents"]
    assert len(acc.uncleared(TODAY)["payouts"]) == 1


def test_payout_far_from_the_bank_line_date_is_not_suggested(client, w, books):
    row = _paid_and_paid_out(client, w)
    far = TODAY + datetime.timedelta(days=10)
    _bank_feed(w, [TransactionInfo("po", far, row["amount_cents"], "STRIPE TRANSFER")])
    (suggested,) = matching.suggestions()
    assert suggested["candidates"] == []


def test_reconcile_page_lists_the_payout_ticked_and_records_the_board_ticks(client, w, books):
    _setup_cash(w)
    row = _paid_and_paid_out(client, w)
    payout = StripePayout.objects.get()
    client.force_login(w["board"])
    page = client.get(reverse("reconcile"), {"date": TODAY.isoformat()}).content.decode()
    assert "Stripe payouts" in page and payout.payout_id in page
    assert f'name="payouts" value="{payout.pk}" checked' in page
    client.post(
        reverse("reconcile"),
        {
            "statement_date": TODAY.isoformat(),
            "statement_balance": f"{(100_000 + row['amount_cents']) / 100:.2f}",
            "payouts": [str(payout.pk)],
        },
    )
    rec = acc.BankReconciliation.objects.get()
    assert rec.difference_cents == 0 and rec.book_balance_cents == 100_000 + row["amount_cents"]
    client.force_login(w["owner"])
    assert client.get(reverse("reconcile"), {"date": TODAY.isoformat()}).status_code == 403
