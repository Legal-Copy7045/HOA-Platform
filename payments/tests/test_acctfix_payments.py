# ruff: noqa: F811  (the scenario fixtures are imported, then used as arguments)
"""Fixes for the payment QA findings (D33-acctfix-5 and -6): further tests
beside the scenarios in test_payment_scenarios.py, using their fixtures."""

import datetime

import pytest
from django.core.exceptions import PermissionDenied
from django.urls import reverse

from community.models import Role
from community.tests.factories import make_member
from core.scoping import system_access
from ledger import accounting as acc
from ledger import allocation
from ledger import services as ledger
from ledger.bankfeeds import matching
from ledger.bankfeeds.base import TransactionInfo
from ledger.models import AccountingSetup, LedgerEntry, ReconciledItem
from ledger.tests.accounting_invariants import journal, journal_totals
from payments import services as pay_services
from payments import sync
from payments.models import AlertKind, StripePayout
from payments.tests.test_payment_scenarios import (  # noqa: F401  (fixtures)
    TODAY,
    _alerts,
    _bank_feed,
    _bt,
    _check,
    _clearing,
    _paid_and_paid_out,
    _payments,
    _return,
    _start,
    _stripe_balance,
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


# --- 5. Refunds: surcharge part in proportion, payment part on the account --------------------


def _credit_card_payment(client, w):
    checkout = _start(client, w, "credit")
    _pi, ch_id = w["fake"].pay(checkout.session_id, funding="credit")
    _return(client, checkout)
    (payment,) = _payments()
    assert payment.base_cents == 10_000 and payment.surcharge_cents == 300
    return payment, ch_id


def _refund(w, ch_id, refund_id, amount):
    w["fake"].refunds.setdefault(ch_id, []).append(
        {"id": refund_id, "amount": amount, "status": "succeeded"}
    )
    _bt(w["fake"], "refund", refund_id, -amount)
    sync.poll_recorded()


def _surcharge_income():
    gl = AccountingSetup.get().accounts()
    return -journal_totals(journal()).get(gl["card_surcharge"], 0)


def _record(client, w, refund_id):
    (alert,) = [a for a in _alerts(AlertKind.REFUND) if a.key == refund_id]
    client.force_login(w["board"])
    client.post(reverse("payment_alert_resolve", args=[alert.pk]), {"record_refund": "1"})
    (alert,) = [a for a in _alerts(AlertKind.REFUND) if a.key == refund_id]
    return alert


def test_partial_refund_returns_the_surcharge_in_proportion(client, w, books):
    payment, ch_id = _credit_card_payment(client, w)
    _refund(w, ch_id, "re_qa000010", 5_150)  # half of 103.00
    ((_ev, surcharge, base),) = pay_services.refund_portions(payment)
    assert (surcharge, base) == (150, 5_000)
    assert _surcharge_income() == 150
    alert = _record(client, w, "re_qa000010")
    assert alert.resolved_at is not None and "$50.00" in alert.resolution
    with system_access("t"):
        refund = LedgerEntry.objects.get(kind="refund")
    assert refund.amount_cents == 5_000 and refund.reference == payment.payment_intent_id
    assert ledger.balance(w["lot"].pk) == 10_000  # 50.00 left of the gate key + 50.00 back
    assert allocation.charge_status(w["c1"]) == "paid"  # the pond fee stays paid
    sync.sync_balance()
    assert _clearing() == _stripe_balance(w["fake"])
    _check(books, synced=True)


def test_two_partial_refunds_add_up_to_the_whole_surcharge(client, w, books):
    payment, ch_id = _credit_card_payment(client, w)
    _refund(w, ch_id, "re_qa000011", 3_333)
    _refund(w, ch_id, "re_qa000012", 6_967)
    parts = [(s, b) for _ev, s, b in pay_services.refund_portions(payment)]
    assert parts == [(97, 3_236), (203, 6_764)]  # round(33.33 x 3/103) = 0.97; then the rest
    assert sum(s for s, _b in parts) == 300 and sum(b for _s, b in parts) == 10_000
    for refund_id in ("re_qa000011", "re_qa000012"):
        _record(client, w, refund_id)
    assert _surcharge_income() == 0
    assert ledger.balance(w["lot"].pk) == 15_000  # as before the payment
    sync.sync_balance()
    assert _clearing() == _stripe_balance(w["fake"])
    _check(books, synced=True)


def test_full_refund_recorded_on_the_account_instead_of_a_reversal(client, w, books):
    payment, ch_id = _credit_card_payment(client, w)
    _refund(w, ch_id, "re_qa000013", 10_300)
    _record(client, w, "re_qa000013")
    assert _surcharge_income() == 0 and ledger.balance(w["lot"].pk) == 15_000
    with pytest.raises(ledger.LedgerError, match="reverse that refund entry first"):
        ledger.reverse_entry(payment.entry, reason="Refunded", actor=w["board"])
    sync.sync_balance()
    assert _clearing() == _stripe_balance(w["fake"])
    _check(books, synced=True)


def test_refund_after_the_payment_was_reversed_is_not_recorded_twice(client, w, books):
    payment, ch_id = _credit_card_payment(client, w)
    _refund(w, ch_id, "re_qa000014", 10_300)
    ledger.reverse_entry(payment.entry, reason="Refunded at Stripe", actor=w["board"])
    (alert,) = _alerts(AlertKind.REFUND)
    with pytest.raises(ledger.LedgerError, match="reversed"):
        pay_services.record_refund(alert, actor=w["board"])
    _record(client, w, "re_qa000014")  # the page shows the reason; the item stays open
    (alert,) = _alerts(AlertKind.REFUND)
    assert alert.resolved_at is None and ledger.balance(w["lot"].pk) == 15_000
    sync.sync_balance()
    assert _clearing() == _stripe_balance(w["fake"])
    _check(books, synced=True)


def test_only_the_board_records_a_refund(client, w, books):
    _payment, ch_id = _credit_card_payment(client, w)
    _refund(w, ch_id, "re_qa000015", 1_030)
    (alert,) = _alerts(AlertKind.REFUND)
    url = reverse("payment_alert_resolve", args=[alert.pk])
    for user in (w["owner"], w["owner_b"], make_member(roles=[Role.MANAGER])):
        client.force_login(user)
        assert client.post(url, {"record_refund": "1"}).status_code in (403, 404)
    with pytest.raises(PermissionDenied):
        pay_services.record_refund(alert, actor=w["owner"])
    with system_access("t"):
        assert not LedgerEntry.objects.filter(kind="refund").exists()
    (alert,) = _alerts(AlertKind.REFUND)
    assert alert.resolved_at is None
    _check(books)


@pytest.mark.parametrize(
    ("refunds", "expected"),
    [
        ([10_300], [300]),
        ([1, 1, 10_298], [0, 0, 300]),
        ([5_150, 5_150], [150, 150]),
        ([100, 10_200], [3, 297]),
        ([10_000], [291]),  # 100.00 of 103.00 refunded: 2.91 of the surcharge
    ],
)
def test_refund_surcharge_parts_never_exceed_the_surcharge(client, w, refunds, expected):
    payment, ch_id = _credit_card_payment(client, w)
    for i, amount in enumerate(refunds):
        _refund(w, ch_id, f"re_qa1000{i:02d}", amount)
    parts = pay_services.refund_portions(payment)
    assert [s for _e, s, _b in parts] == expected
    assert all(s + b == e.amount_cents for e, s, b in parts)
