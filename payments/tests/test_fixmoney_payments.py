# ruff: noqa: F811  (the scenario fixtures are imported, then used as arguments)
"""Online payments and the dashboard's balance to-do (D34-fixmoney): fixes
for regression findings BUG-003 (= SEC-001 = R2-001), BUG-004, BUG-006,
BUG-008, BUG-009 and R2-004, each as its scenario, with the global
accounting invariants checked."""

import datetime
from unittest import mock

import pytest
from django.urls import reverse
from django.utils import timezone

from audit.models import AuditEvent
from community import services as community
from community.models import Account, Person
from community.tests.factories import make_member
from core import my_items
from core.scoping import system_access
from ledger import allocation
from ledger import services as ledger
from ledger.models import LedgerEntry
from payments import checkout as flow
from payments import stripe_api, sync
from payments.models import AlertKind, CheckoutState, PendingCheckout, StripeEvent
from payments.tests.test_payment_scenarios import (  # noqa: F401  (fixtures)
    TODAY,
    _alerts,
    _bt,
    _check,
    _payments,
    _return,
    _start,
    _state,
    books,
    fake,
    w,
)

pytestmark = pytest.mark.django_db


def ago(n):
    return TODAY - datetime.timedelta(days=n)


def _person(user):
    with system_access("t"):
        return Person.objects.get(user=user)


def _accounts(lot):
    with system_access("t"):
        return list(Account.objects.filter(lot=lot).order_by("opened_on", "id"))


# --- BUG-003 / SEC-001 / R2-001: a payment that settles after the sale ------------------------


def test_bank_payment_settling_after_the_sale_stays_on_the_payers_account(client, w, books):
    s1, s2 = _charge(w, 6_000, ago(10), "Seller fee 1"), _charge(w, 3_000, ago(9), "Seller fee 2")
    checkout = _start(client, w, "bank", charges=[s1, s2])
    pi_id, _ = w["fake"].pay(checkout.session_id, bank=True, status="processing")
    _return(client, checkout)
    (seller_account,) = _accounts(w["lot"])
    buyer = make_member()
    # The sale closes today (the fixture's two charges of today go to the buyer).
    community.transfer_lot(
        w["lot"], new_owners=[_person(buyer)], transfer_date=TODAY, actor=w["board"]
    )
    _seller, buyer_account = _accounts(w["lot"])
    buyer_dues = _charge(w, 4_500, TODAY, "Dues")
    assert buyer_dues.account_id == buyer_account.pk
    w["fake"].settle_ach(pi_id)
    sync.poll_processing()
    (payment,) = _payments()
    assert payment.entry.account_id == seller_account.pk
    # The seller's chosen charges are paid; nothing of the buyer's is.
    assert allocation.charge_status(s1) == allocation.charge_status(s2) == "paid"
    assert allocation.charge_status(buyer_dues) == "open"
    assert ledger.balance(w["lot"].pk, account=seller_account.pk) == 0
    assert ledger.balance(w["lot"].pk, account=buyer_account.pk) == 15_000 + 4_500
    assert allocation.unapplied_credit(w["lot"].pk, account=buyer_account.pk) == 0
    (alert,) = _alerts(AlertKind.FORMER_OWNER)
    assert alert.lot_id == w["lot"].pk and alert.amount_cents == 9_000
    # The receipt is the seller's: the buyer never sees it.
    client.force_login(buyer)
    page = client.get(reverse("my_account")).content.decode()
    assert payment.payment_intent_id not in page
    receipt = reverse("payment_receipt", args=[payment.pk])
    assert client.get(receipt).status_code == 404
    client.force_login(w["board"])
    assert client.get(receipt).status_code == 200
    sync.sync_balance()
    _check(books, synced=True)


def test_online_payment_is_dated_when_stripe_says_it_succeeded(client, w, books):
    checkout = _start(client, w, "bank")
    pi_id, ch_id = w["fake"].pay(checkout.session_id, bank=True, status="processing")
    _return(client, checkout)
    with system_access("t"):
        PendingCheckout.objects.filter(pk=checkout.pk).update(
            created_at=timezone.now() - datetime.timedelta(days=6)
        )
    w["fake"].settle_ach(pi_id)
    three_days_ago = int((timezone.now() - datetime.timedelta(days=3)).timestamp())
    w["fake"].charges[ch_id]["balance_transaction"]["created"] = three_days_ago
    sync.poll_processing()
    (payment,) = _payments()
    assert payment.entry.posted_on == timezone.localdate(
        datetime.datetime.fromtimestamp(three_days_ago, tz=datetime.UTC)
    )
    # Never in the future, never before the checkout started.
    assert flow._succeeded_on(checkout, {"created": 4_102_444_800}) == TODAY
    with system_access("t"):
        fresh = PendingCheckout.objects.get(pk=checkout.pk)
    assert flow._succeeded_on(fresh, {"created": 1_000_000_000}) == timezone.localdate(
        fresh.created_at
    )
    _check(books)


# --- BUG-004: a returned bank payment that was partly refunded ---------------------------------


def _bank_payment(client, w, charges):
    checkout = _start(client, w, "bank", charges=charges)
    _pi, ch_id = w["fake"].pay(checkout.session_id, bank=True)
    _return(client, checkout)
    return ch_id


def test_return_of_a_partly_refunded_payment_becomes_a_board_item_and_polling_goes_on(
    client, w, books
):
    first = _bank_payment(client, w, [w["c1"]])
    second = _bank_payment(client, w, [w["c2"]])
    w["fake"].refunds[first] = [{"id": "re_fm000001", "amount": 2_000, "status": "succeeded"}]
    sync.poll_recorded()
    (refund_alert,) = _alerts(AlertKind.REFUND)
    client.force_login(w["board"])
    client.post(reverse("payment_alert_resolve", args=[refund_alert.pk]), {"record_refund": "1"})
    with system_access("t"):
        assert LedgerEntry.objects.filter(kind="refund").count() == 1
    for ch in (first, second):
        w["fake"].charges[ch]["status"] = "failed"
    assert sync.poll_recorded() == 2  # no crash; both payments looked at
    alerts = {a.key: a for a in _alerts(AlertKind.RETURNED)}
    assert "recorded as refunded" in alerts[first].detail
    assert "was reversed" in alerts[second].detail
    p1, p2 = sorted(_payments(), key=lambda p: p.pk)
    with system_access("t"):
        assert not LedgerEntry.objects.filter(reverses=p1.entry).exists()
        assert LedgerEntry.objects.filter(reverses=p2.entry).exists()
        assert StripeEvent.objects.filter(kind="returned").count() == 2
    assert AuditEvent.objects.filter(action="payments.return_needs_board").count() == 1
    assert AuditEvent.objects.filter(action="payments.returned").count() == 1
    sync.poll_recorded()  # seen once: nothing doubles
    assert len(_alerts(AlertKind.RETURNED)) == 2
    assert AuditEvent.objects.filter(action="payments.returned").count() == 1
    _check(books)


def test_a_return_is_marked_seen_only_with_its_reversal(client, w, books):
    ch = _bank_payment(client, w, [w["c2"]])
    w["fake"].charges[ch]["status"] = "failed"
    with mock.patch("ledger.services.reverse_entry", side_effect=RuntimeError("database away")):
        sync.poll_recorded()
    with system_access("t"):
        assert not StripeEvent.objects.filter(kind="returned").exists()
    assert _alerts(AlertKind.RETURNED) == []
    sync.poll_recorded()  # the next run does it
    (payment,) = _payments()
    with system_access("t"):
        assert LedgerEntry.objects.filter(reverses=payment.entry).exists()
    assert allocation.charge_status(w["c2"]) == "open"
    (alert,) = _alerts(AlertKind.RETURNED)
    assert "was reversed" in alert.detail
    _check(books)


# --- R2-004: never expire a checkout Stripe could not be asked about ---------------------------


def test_open_checkout_is_not_expired_while_stripe_cannot_be_asked(client, w, books):
    checkout = _start(client, w)
    later = timezone.now() + datetime.timedelta(hours=25)
    w["fake"].fail_next = (429, {"error": {"code": "rate_limit"}})
    sync.poll_open_checkouts(now=later)
    assert _state(checkout) == CheckoutState.OPEN  # unanswered: asked again later
    w["fake"].pay(checkout.session_id, funding="credit")  # it had been paid after all
    sync.poll_open_checkouts(now=later)
    assert _state(checkout) == CheckoutState.PAID
    assert len(_payments()) == 1
    _check(books)


def test_open_checkout_unanswered_for_days_becomes_a_board_item(client, w):
    checkout = _start(client, w)
    much_later = timezone.now() + datetime.timedelta(days=5)
    with mock.patch.object(stripe_api, "transport", side_effect=OSError("down")):
        sync.poll_open_checkouts(now=much_later)
    assert _state(checkout) == CheckoutState.OPEN
    (alert,) = _alerts(AlertKind.MISMATCH)
    assert alert.key == f"unanswered-{checkout.pk}"
    sync.poll_open_checkouts(now=much_later)  # Stripe answers: past its time, not paid
    assert _state(checkout) == CheckoutState.EXPIRED


# --- BUG-009: no empty metadata value ----------------------------------------------------------


def test_checkout_metadata_never_sends_an_empty_value_and_verifies_without_it(client, w):
    checkout = _start(client, w)
    sent = w["fake"].sessions[checkout.session_id]["metadata"]
    assert all(sent.values()) and sent["account_id"] == str(checkout.account_id)
    with system_access("t"):
        checkout = PendingCheckout.objects.get(pk=checkout.pk)
    checkout.account_id = None  # a lot whose owner has no account row
    meta = flow._metadata(checkout)
    assert "account_id" not in meta and all(meta.values())
    session = {
        **w["fake"].sessions[checkout.session_id],
        "metadata": {**meta},  # Stripe dropped the key
    }
    flow._verify_session(checkout, session)
    flow._verify_session(checkout, {**session, "metadata": {**meta, "account_id": ""}})
    with pytest.raises(flow.Mismatch):
        flow._verify_session(checkout, {**session, "metadata": {**meta, "account_id": "9"}})


# --- BUG-006 and BUG-008: the dashboard's balance to-do ----------------------------------------


def _balance_item(user):
    return next(i for i in my_items.items_for(user) if i.nav == "my_account")


def _charge(w, cents, on, desc):
    return ledger.post_charge(
        lot=w["lot"],
        kind="other_charge",
        amount_cents=cents,
        description=desc,
        posted_on=on,
        actor=w["board"],
    )


def _clear(w):
    """Credit the fixture's two charges away: a clean account to start from."""
    for c in (w["c1"], w["c2"]):
        ledger.post_credit(
            lot=w["lot"], amount_cents=c.amount_cents, description="Clear", actor=w["board"]
        )
    assert ledger.balance(w["lot"].pk) == 0


def test_due_since_is_the_oldest_open_charge_like_my_account(client, w):
    _clear(w)
    january = _charge(w, 10_000, ago(60), "January")
    february = _charge(w, 10_000, ago(30), "February")
    ledger.record_payment(
        lot=w["lot"],
        amount_cents=10_000,
        received_on=ago(25),
        reference="611",
        apply_to=[february.pk],
        actor=w["board"],
    )
    assert allocation.charge_status(january) == "open"
    item = _balance_item(w["owner"])
    assert item.due == ledger.amount_due(w["lot"].pk).due_on == ago(60)
    # A bounced check reopens what it paid: still due since that charge, not today.
    check = ledger.record_payment(
        lot=w["lot"], amount_cents=10_000, received_on=ago(20), reference="612", actor=w["board"]
    )
    assert allocation.charge_status(january) == "paid"
    ledger.reverse_entry(check, reason="Returned", actor=w["board"])
    assert _balance_item(w["owner"]).due == ago(60)


def _spouse_joins(w, on):
    spouse = make_member()
    community.transfer_lot(
        w["lot"],
        new_owners=[_person(w["owner"]), _person(spouse)],
        transfer_date=on,
        actor=w["board"],
    )
    return spouse


def test_pay_button_amount_is_what_the_payment_page_quotes(client, w):
    _clear(w)
    _charge(w, 30_000, ago(10), "Before the spouse joined")
    spouse = _spouse_joins(w, ago(5))
    (account,) = _accounts(w["lot"])  # the household's account carries on
    _charge(w, 15_000, ago(2), "After the spouse joined")
    item = _balance_item(spouse)
    assert item.title.startswith("$450.00 due")  # the account's whole balance
    q = flow.quote_all(spouse, w["lot"])
    assert item.action_label == f"Pay ${q.base_cents / 100:,.2f}" == "Pay $150.00"
    assert item.action_url.endswith("?all=1")
    client.force_login(spouse)
    page = client.get(item.action_url).content.decode()
    assert "$150.00" in page
    # The owner from before: every charge of the account, the same figure on both.
    owner_item = _balance_item(w["owner"])
    assert owner_item.action_label == "Pay $450.00"
    assert flow.quote_all(w["owner"], w["lot"]).base_cents == 45_000
    assert account.pk == ledger.accounts.current_account_id(w["lot"].pk)


def test_pay_button_opens_the_amount_page_when_nothing_is_quoted(client, w):
    _clear(w)
    _charge(w, 30_000, ago(10), "Before the spouse joined")
    spouse = _spouse_joins(w, ago(5))
    item = _balance_item(spouse)  # only charges from before the spouse's time are open
    assert item.title.startswith("$300.00 due")
    assert item.action_label == "Pay online" and item.action_url.endswith("?choose=1")
    client.force_login(spouse)
    assert client.get(item.action_url).status_code == 200
