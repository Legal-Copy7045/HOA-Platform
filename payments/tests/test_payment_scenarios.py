"""Stripe payment scenarios end to end (independent QA, M5).

The owner pays through the real pages; Stripe is the in-memory fake
(payments/tests/fake_stripe.py). After each scenario the global accounting
invariants are checked (ledger/tests/accounting_invariants.py). Scenarios
that expose a bug are kept as ``xfail(strict=True)``.
"""

import datetime
from unittest import mock

import pytest
from django.urls import reverse
from django.utils import timezone

from audit.models import AuditEvent
from community import services as community
from community.tests.factories import LotFactory, make_member
from core.models import FeatureSetting
from core.scoping import system_access
from ledger import accounting as acc
from ledger import allocation
from ledger import services as ledger
from ledger.bankfeeds import matching
from ledger.bankfeeds import services as bank
from ledger.bankfeeds.base import TransactionInfo
from ledger.models import AccountingSetup, LedgerEntry
from ledger.tests.accounting_invariants import Books, journal, journal_totals
from payments import checkout as flow
from payments import stripe_api, sync
from payments.models import (
    AlertKind,
    CheckoutState,
    PaymentAlert,
    PendingCheckout,
    StripePayment,
)
from payments.tests.fake_stripe import FakeStripe

pytestmark = pytest.mark.django_db
KEY = "rk_test_" + "Q" * 24
TODAY = timezone.localdate()


@pytest.fixture
def fake(settings, monkeypatch):
    settings.PAYMENTS_PROVIDER = "stripe"
    settings.STRIPE_SECRET_KEY = KEY
    settings.DEPLOYMENT_STAGE = "poc"
    settings.PORTAL_BASE_URL = "https://portal.example.com"
    FeatureSetting.objects.create(key="online_payments", enabled=True)
    stripe = FakeStripe()
    monkeypatch.setattr(stripe_api, "transport", stripe)
    return stripe


@pytest.fixture
def books(app_role_connection):
    b = Books()
    b.app_role = app_role_connection
    return b


@pytest.fixture
def w(fake):
    board = make_member(board=True)
    community.set_rule(
        key="payments.credit_card_surcharge_bp", value="300", citation="Res. (QA)", actor=board
    )
    lot, other = LotFactory(), LotFactory()
    owner, owner_b = make_member(lots=[lot]), make_member(lots=[other])

    def charge(cents, desc, on_lot=lot):
        return ledger.post_charge(
            lot=on_lot, kind="other_charge", amount_cents=cents, description=desc, actor=board
        )

    c1, c2 = charge(10_000, "Pond fee"), charge(5_000, "Gate key")
    charge(7_000, "B fee", other)
    return {
        "board": board,
        "lot": lot,
        "other": other,
        "owner": owner,
        "owner_b": owner_b,
        "c1": c1,
        "c2": c2,
        "fake": fake,
    }


def _check(books, **kw):
    books.check(app_role_connection=books.app_role, **kw)


def _start(client, w, method="credit", charges=None, user=None, amount=None):
    client.force_login(user or w["owner"])
    data = {"method": method}
    if amount:
        data["amount"] = amount
    else:
        data["charge"] = [str(c.pk) for c in (charges or [w["c1"]])]
    response = client.post(reverse("pay_checkout", args=[w["lot"].pk]), data)
    assert response.status_code == 302, response.content[:300]
    with system_access("t"):
        return PendingCheckout.objects.latest("id")


def _return(client, checkout, **params):
    return client.get(
        reverse("pay_return", args=[checkout.pk]), {"session_id": checkout.session_id, **params}
    )


def _state(checkout):
    with system_access("t"):
        return PendingCheckout.objects.get(pk=checkout.pk).state


def _payments():
    with system_access("t"):
        return list(StripePayment.objects.select_related("entry"))


def _alerts(kind=None):
    with system_access("t"):
        qs = PaymentAlert.objects.all()
        return list(qs.filter(kind=kind) if kind else qs)


def _clearing(rows=None):
    gl = AccountingSetup.get().accounts()
    return journal_totals(rows or journal()).get(gl["stripe_clearing"], 0)


def _stripe_balance(fake):
    """What Stripe holds for the HOA: every balance line's net, less payouts."""
    return sum(b["net"] for b in fake.balance) - sum(p["amount"] for p in fake.payouts)


def _bt(fake, kind, source, amount, fee=0):
    line = {
        "id": fake._id("txn"),
        "type": kind,
        "source": source,
        "amount": amount,
        "fee": fee,
        "net": amount - fee,
        "created": int(timezone.now().timestamp()) - 2 * 86400,
        "livemode": False,
    }
    fake.balance.append(line)
    return line


# --- 6. Successful payments ------------------------------------------------------------------


def test_credit_card_payment_books_base_surcharge_fee_and_payout(client, w, books):
    checkout = _start(client, w, "credit")
    w["fake"].pay(checkout.session_id, funding="credit")
    assert b"your payment was received" in _return(client, checkout).content
    (payment,) = _payments()
    assert payment.entry.amount_cents == -10_000 and payment.surcharge_cents == 300
    assert ledger.balance(w["lot"].pk) == 5_000  # the surcharge never touches the owner
    assert allocation.charge_status(w["c1"]) == "paid"
    _check(books)
    sync.sync_balance()
    _check(books, synced=True)
    gl = AccountingSetup.get().accounts()
    rows = journal()
    totals = journal_totals(rows)
    fee = w["fake"].balance[0]["fee"]
    assert totals[gl["card_surcharge"]] == -300  # income (a credit)
    assert totals[gl["processing_fees"]] == fee
    assert _clearing(rows) == 10_300 - fee == _stripe_balance(w["fake"])
    w["fake"].payout(list(w["fake"].balance))
    sync.sync_balance()
    rows = journal()
    assert _clearing(rows) == 0 == _stripe_balance(w["fake"])
    assert sync.reconcile().ok
    _check(books, synced=True)


@pytest.mark.parametrize(
    ("method", "funding", "bank_"), [("debit", "debit", False), ("bank", "", True)]
)
def test_debit_and_bank_payments_carry_no_surcharge(client, w, books, method, funding, bank_):
    checkout = _start(client, w, method, charges=[w["c1"], w["c2"]])
    w["fake"].pay(checkout.session_id, funding=funding or "credit", bank=bank_)
    _return(client, checkout)
    (payment,) = _payments()
    assert payment.surcharge_cents == 0 and payment.entry.amount_cents == -15_000
    assert payment.funding == ("bank" if bank_ else "debit")
    assert ledger.balance(w["lot"].pk) == 0 and allocation.open_charges(w["lot"].pk) == []
    w["fake"].payout(list(w["fake"].balance))
    sync.sync_balance()
    gl = AccountingSetup.get().accounts()
    assert gl["card_surcharge"] not in journal_totals(journal())
    assert _clearing() == 0 and sync.reconcile().ok
    _check(books, synced=True)


# --- 7. Failures ---------------------------------------------------------------------------


def test_declined_cancelled_expired_and_abandoned_checkouts_record_nothing(client, w, books):
    declined = _start(client, w)  # a declined card leaves Stripe's page open
    assert b"Waiting for Stripe" in _return(client, declined).content
    cancelled = _start(client, w)
    assert _return(client, cancelled, cancelled="1").status_code == 302
    expired = _start(client, w)
    w["fake"].sessions[expired.session_id]["status"] = "expired"
    _return(client, expired)
    abandoned = _start(client, w)
    assert _state(declined) == _state(cancelled) == CheckoutState.OPEN
    assert _state(expired) == CheckoutState.EXPIRED
    sync.poll_open_checkouts(now=timezone.now() + datetime.timedelta(hours=25))
    for co in (declined, cancelled, abandoned):
        assert _state(co) == CheckoutState.EXPIRED
    assert _payments() == [] and ledger.balance(w["lot"].pk) == 15_000
    _check(books)


def test_bank_payment_processing_then_succeeded_days_later(client, w, books):
    checkout = _start(client, w, "bank")
    pi_id, _ = w["fake"].pay(checkout.session_id, bank=True, status="processing")
    assert b"processing" in _return(client, checkout).content
    assert _payments() == [] and ledger.balance(w["lot"].pk) == 15_000
    _check(books)
    w["fake"].settle_ach(pi_id)
    later = timezone.now() + datetime.timedelta(days=4)
    with mock.patch("django.utils.timezone.now", return_value=later):
        sync.poll_processing()
    (payment,) = _payments()
    assert payment.entry.posted_on == timezone.localdate(later)  # received when it cleared
    assert allocation.charge_status(w["c1"]) == "paid" and ledger.balance(w["lot"].pk) == 5_000
    sync.sync_balance()
    _check(books, synced=True)


def test_bank_payment_returned_after_recording_is_reversed_and_the_board_told(client, w, books):
    checkout = _start(client, w, "bank", charges=[w["c2"]])
    _pi, ch_id = w["fake"].pay(checkout.session_id, bank=True)
    _return(client, checkout)
    assert allocation.charge_status(w["c2"]) == "paid"
    w["fake"].charges[ch_id]["status"] = "failed"
    _bt(w["fake"], "payment_failure_refund", ch_id, -5_000)
    sync.poll_recorded()
    sync.poll_recorded()
    (payment,) = _payments()
    with system_access("t"):
        reversal = LedgerEntry.objects.get(reverses=payment.entry)
    assert reversal.amount_cents == 5_000
    assert allocation.charge_status(w["c2"]) == "open" and ledger.balance(w["lot"].pk) == 15_000
    assert len(_alerts(AlertKind.RETURNED)) == 1
    client.force_login(w["board"])
    assert "was returned" in client.get(reverse("my_items")).content.decode()
    sync.sync_balance()
    assert _clearing() == _stripe_balance(w["fake"])
    _check(books, synced=True)


def test_refund_and_dispute_are_board_items_only(client, w, books):
    checkout = _start(client, w, "credit")
    _pi, ch_id = w["fake"].pay(checkout.session_id, funding="credit")
    _return(client, checkout)
    w["fake"].refunds[ch_id] = [{"id": "re_qa000001", "amount": 2_000, "status": "succeeded"}]
    w["fake"].disputes[ch_id] = [{"id": "dp_qa000001", "amount": 10_300}]
    sync.poll_recorded()
    sync.poll_recorded()
    assert sorted(a.kind for a in _alerts()) == [AlertKind.DISPUTE, AlertKind.REFUND]
    assert ledger.balance(w["lot"].pk) == 5_000 and allocation.charge_status(w["c1"]) == "paid"
    client.force_login(w["owner"])
    assert "re_qa000001" not in client.get(reverse("my_account")).content.decode()
    _check(books)


def test_debit_card_that_paid_a_surcharge_gives_a_refund_item_closed_by_the_refund(
    client, w, books
):
    checkout = _start(client, w, "credit")
    _pi, ch_id = w["fake"].pay(checkout.session_id, funding="debit")
    _return(client, checkout)
    (alert,) = _alerts(AlertKind.SURCHARGE_REFUND)
    assert alert.amount_cents == 300 and alert.resolved_at is None
    w["fake"].refunds[ch_id] = [{"id": "re_qa000002", "amount": 300, "status": "succeeded"}]
    _bt(w["fake"], "refund", "re_qa000002", -300)
    sync.poll_recorded()
    (alert,) = _alerts(AlertKind.SURCHARGE_REFUND)
    assert alert.resolved_at is not None and not _alerts(AlertKind.REFUND)
    sync.sync_balance()
    gl = AccountingSetup.get().accounts()
    assert journal_totals(journal()).get(gl["card_surcharge"], 0) == 0  # income then refunded
    assert _clearing() == _stripe_balance(w["fake"])
    assert ledger.balance(w["lot"].pk) == 5_000
    _check(books, synced=True)


def test_credit_card_on_a_no_fee_session_is_absorbed(client, w, books):
    checkout = _start(client, w, "debit")
    w["fake"].pay(checkout.session_id, funding="credit")
    _return(client, checkout)
    (payment,) = _payments()
    assert payment.surcharge_cents == 0 and payment.funding == "credit"
    assert _alerts() == []
    _check(books)


def test_full_refund_of_a_surcharged_payment_keeps_stripe_clearing_true(client, w, books):
    checkout = _start(client, w, "credit")
    _pi, ch_id = w["fake"].pay(checkout.session_id, funding="credit")
    _return(client, checkout)
    w["fake"].refunds[ch_id] = [{"id": "re_qa000003", "amount": 10_300, "status": "succeeded"}]
    _bt(w["fake"], "refund", "re_qa000003", -10_300)
    sync.poll_recorded()
    (alert,) = _alerts(AlertKind.REFUND)
    (payment,) = _payments()
    ledger.reverse_entry(payment.entry, reason="Refunded at Stripe", actor=w["board"])
    sync.sync_balance()
    _check(books, synced=True)
    assert alert.amount_cents == 10_300
    assert _clearing() == _stripe_balance(w["fake"])


# --- 8. Tampering ----------------------------------------------------------------------------


def test_forged_and_foreign_session_returns_record_nothing(client, w, books):
    checkout = _start(client, w)
    w["fake"].pay(checkout.session_id)
    assert _return(client, checkout, session_id="cs_test_forged0001").status_code == 404
    for intruder in (w["owner_b"], make_member(lots=[w["lot"]]), w["board"]):
        client.force_login(intruder)
        assert _return(client, checkout).status_code in (403, 404)
    assert _state(checkout) == CheckoutState.OPEN and _payments() == []
    client.force_login(w["owner"])
    _return(client, checkout)
    _return(client, checkout)  # replayed return: still one payment
    sync.poll_open_checkouts()
    assert len(_payments()) == 1
    assert AuditEvent.objects.filter(action="payments.recorded").count() == 1
    _check(books)


@pytest.mark.parametrize(
    ("where", "field", "value", "code"),
    [
        ("session", "amount_total", 10_000, "amount"),
        ("session", "currency", "cad", "currency"),
        ("session", "livemode", True, "livemode"),
        ("intent", "amount", 1, "amount"),
        ("intent", "currency", "eur", "currency"),
        ("intent", "livemode", True, "livemode"),
        ("charge", "amount", 1, "charge"),
        ("charge", "livemode", True, "charge"),
    ],
)
def test_any_mismatch_records_nothing(client, w, books, where, field, value, code):
    checkout = _start(client, w)
    pi_id, ch_id = w["fake"].pay(checkout.session_id)
    target = {
        "session": w["fake"].sessions[checkout.session_id],
        "intent": w["fake"].intents[pi_id],
        "charge": w["fake"].charges[ch_id],
    }[where]
    target[field] = value
    _return(client, checkout)
    sync.poll_open_checkouts()
    assert _state(checkout) == CheckoutState.MISMATCH and _payments() == []
    assert ledger.balance(w["lot"].pk) == 15_000
    assert AuditEvent.objects.get(action="payments.mismatch").metadata["code"] == code
    assert len(_alerts(AlertKind.MISMATCH)) == 1
    _check(books)


def test_double_clicked_checkout_records_only_what_was_paid(client, w, books):
    first = _start(client, w)
    second = _start(client, w)
    assert first.nonce != second.nonce and first.session_id != second.session_id
    w["fake"].pay(second.session_id)
    _return(client, second)
    _return(client, first)
    sync.poll_open_checkouts(now=timezone.now() + datetime.timedelta(hours=25))
    assert _state(first) == CheckoutState.EXPIRED and len(_payments()) == 1
    _check(books)
    # Both paid (two tabs): two real payments; the second is credit, never over-applied.
    third, fourth = _start(client, w, charges=[w["c2"]]), _start(client, w, charges=[w["c2"]])
    for co in (third, fourth):
        w["fake"].pay(co.session_id)
        _return(client, co)
    assert len(_payments()) == 3
    assert (
        ledger.balance(w["lot"].pk) == -5_000 and allocation.unapplied_credit(w["lot"].pk) == 5_000
    )
    _check(books)


def test_charge_paid_by_check_while_checkout_open_leaves_the_online_money_as_credit(
    client, w, books
):
    checkout = _start(client, w, "debit", charges=[w["c2"]])
    ledger.record_payment(
        lot=w["lot"],
        amount_cents=5_000,
        received_on=TODAY,
        reference="881",
        actor=w["board"],
        apply_to=[w["c2"].pk],
    )
    w["fake"].pay(checkout.session_id, funding="debit")
    _return(client, checkout)
    (payment,) = _payments()
    assert allocation.applications_of(payment.entry.pk) == [(w["c1"].pk, 5_000)]
    assert ledger.balance(w["lot"].pk) == 5_000
    _check(books)


def test_test_key_in_production_refuses_everything(client, w, books, settings):
    checkout = _start(client, w)
    w["fake"].pay(checkout.session_id)
    settings.DEPLOYMENT_STAGE = "production"
    client.force_login(w["owner"])
    assert client.get(reverse("pay_online", args=[w["lot"].pk])).status_code == 404
    assert _return(client, checkout).status_code == 404
    assert sync.poll_open_checkouts() == 0 and sync.poll_processing() == 0
    with pytest.raises(flow.PaymentError):
        flow.start(w["owner"], w["lot"], method="bank", charge_ids=[w["c1"].pk])
    assert _payments() == [] and ledger.balance(w["lot"].pk) == 15_000
    _check(books)


def test_manual_online_payment_is_refused_on_every_board_path(client, w, books):
    for method in ("online", "Stripe", " ONLINE "):
        with pytest.raises(ledger.LedgerError, match="automatically from Stripe"):
            ledger.record_payment(
                lot=w["lot"], amount_cents=500, received_on=TODAY, method=method, actor=w["board"]
            )
    client.force_login(w["board"])
    client.post(
        reverse("lot_ledger_action", args=[w["lot"].pk, "payment"]),
        {"amount_cents": "100.00", "received_on": str(TODAY), "method": "online"},
    )
    with pytest.raises(PermissionError):
        ledger.record_online_payment(
            lot=w["lot"],
            amount_cents=500,
            received_on=TODAY,
            payment_intent_id="pi_x",
            apply_to=[],
            actor=w["board"],
        )
    assert ledger.balance(w["lot"].pk) == 15_000
    _check(books)


# --- 10. Payouts and the bank ----------------------------------------------------------------


def _bank_feed(w, lines):
    FeatureSetting.objects.get_or_create(key="bank_feeds", defaults={"enabled": True})
    conn = bank.add_file_account(display_name="Operating", actor=w["board"])
    bank.store_lines(conn, lines, source="csv", actor=w["board"])
    return conn


def _paid_and_paid_out(client, w):
    checkout = _start(client, w, "credit")
    w["fake"].pay(checkout.session_id, funding="credit")
    _return(client, checkout)
    w["fake"].payout(list(w["fake"].balance))
    sync.sync_balance()
    (row,) = sync.payouts_for_matching(TODAY - datetime.timedelta(days=3), TODAY)
    return row


def test_payout_matches_the_bank_deposit_and_reconciles(client, w, books):
    row = _paid_and_paid_out(client, w)
    assert row["adds_up"] and row["gross_cents"] == 10_300
    _bank_feed(w, [TransactionInfo("po", TODAY, row["amount_cents"], "STRIPE TRANSFER")])
    run = sync.reconcile()
    assert run.ok, run.problems
    _check(books, synced=True)


def test_payout_missing_from_the_bank_or_not_adding_up_fails_reconciliation(client, w, books):
    checkout = _start(client, w, "credit")
    w["fake"].pay(checkout.session_id, funding="credit")
    _return(client, checkout)
    po_id = w["fake"].payout(list(w["fake"].balance), amount=1)  # does not add up
    sync.sync_balance()
    future = TODAY + datetime.timedelta(days=sync.PAYOUT_BANK_DAYS)
    _bank_feed(w, [TransactionInfo("other", future, 5_000, "SOMETHING ELSE")])
    run = sync.reconcile()
    codes = {(p["code"], p["id"]) for p in run.problems}
    assert ("payout_does_not_add_up", po_id) in codes
    assert ("payout_not_in_bank", po_id) in codes
    assert not run.ok and _alerts(AlertKind.RECONCILIATION)
    _check(books, synced=True)


def test_bank_reconciliation_counts_stripe_payouts(client, w, books):
    acc.save_setup(
        gl_accounts="",
        opening_cash_cents=100_000,
        opening_cash_on=TODAY - datetime.timedelta(days=30),
        actor=w["board"],
    )
    row = _paid_and_paid_out(client, w)
    _bank_feed(w, [TransactionInfo("po", TODAY, row["amount_cents"], "STRIPE TRANSFER")])
    _check(books, synced=True)
    (suggested,) = matching.suggestions()
    assert suggested["candidates"], "the payout's bank line has nothing to match"
    rec = acc.reconcile(
        statement_date=TODAY,
        statement_balance_cents=100_000 + row["amount_cents"],
        cleared_deposit_ids=[],
        cleared_payment_ids=[],
        actor=w["board"],
    )
    assert rec.difference_cents == 0
