"""Stripe test-mode integration (decision D32-payments). A fake stands in
for api.stripe.com: nothing here reaches the network."""

import datetime
import logging
import pathlib
import re
from unittest import mock

import pytest
from django.core import mail
from django.core.exceptions import PermissionDenied
from django.urls import reverse
from django.utils import timezone

from audit.models import AuditEvent
from community import services as community
from community.models import Account, Person, Role
from community.tests.factories import LotFactory, UserFactory, days_ago, enroll_totp, make_member
from core.append_only import AppendOnlyError
from core.models import FeatureSetting
from core.policy import SYSTEM
from core.scoping import system_access
from ledger import allocation
from ledger import services as ledger
from payments import checkout as flow
from payments import stripe_api, sync
from payments.models import (
    AlertKind,
    CheckoutState,
    PaymentAlert,
    PendingCheckout,
    StripePayment,
)
from payments.providers import config
from payments.tests.fake_stripe import FakeStripe

pytestmark = pytest.mark.django_db
KEY = "rk_test_" + "K" * 24


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
def w(fake):
    board = make_member(board=True)
    community.set_rule(
        key="payments.credit_card_surcharge_bp", value="300", citation="Res. 2027-09", actor=board
    )
    lot = LotFactory(street_address="1 Alpha Way")
    owner = make_member(lots=[lot])
    c1 = ledger.post_charge(
        lot=lot, kind="other_charge", amount_cents=10_000, description="Pond fee", actor=board
    )
    c2 = ledger.post_charge(
        lot=lot, kind="other_charge", amount_cents=5_000, description="Gate key", actor=board
    )
    other_lot = LotFactory(street_address="2 Bravo Way")
    owner_b = make_member(lots=[other_lot])
    ledger.post_charge(
        lot=other_lot, kind="other_charge", amount_cents=7_000, description="B fee", actor=board
    )
    return {
        "board": board,
        "lot": lot,
        "owner": owner,
        "c1": c1,
        "c2": c2,
        "other_lot": other_lot,
        "owner_b": owner_b,
        "fake": fake,
    }


def _start(client, w, method="credit", charges=None, user=None):
    client.force_login(user or w["owner"])
    data = {"method": method, "charge": [str(c.pk) for c in (charges or [w["c1"]])]}
    response = client.post(reverse("pay_checkout", args=[w["lot"].pk]), data)
    assert response.status_code == 302, response.content[:300]
    assert response["Location"].startswith("https://checkout.stripe.com/")
    with system_access("t"):
        return PendingCheckout.objects.latest("id")


def _return(client, checkout, session_id=None):
    return client.get(
        reverse("pay_return", args=[checkout.pk]),
        {"session_id": session_id or checkout.session_id},
    )


def _refresh(checkout):
    with system_access("t"):
        return PendingCheckout.objects.get(pk=checkout.pk)


# --- Mode safety ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "stage", "live_allowed", "available"),
    [
        ("rk_test_abc", "poc", False, True),
        ("sk_test_abc", "local", False, True),
        ("rk_test_abc", "production", False, False),  # test cards could "pay" real accounts
        ("sk_test_abc", "production", True, False),
        ("rk_live_abc", "poc", True, False),
        ("sk_live_abc", "production", False, False),
        ("rk_live_abc", "production", True, True),
        ("pk_test_abc", "poc", False, False),
        ("", "poc", False, False),
    ],
)
def test_keys_are_refused_outside_their_mode(settings, key, stage, live_allowed, available):
    settings.PAYMENTS_PROVIDER = "stripe"
    settings.STRIPE_SECRET_KEY = key
    settings.DEPLOYMENT_STAGE = stage
    settings.PAYMENTS_LIVE_ALLOWED = live_allowed
    assert config().available is available
    if not available:
        with pytest.raises(Exception, match=r"not|refused|need"):
            stripe_api.get("/v1/payouts")


def test_no_payments_when_the_feature_is_off(client, w):
    FeatureSetting.objects.filter(key="online_payments").update(enabled=False)
    client.force_login(w["owner"])
    assert client.get(reverse("pay_online", args=[w["lot"].pk])).status_code == 404


# --- Checkout sessions -------------------------------------------------------------------


def test_credit_card_session_has_the_surcharge_line_and_funding_filter(client, w):
    checkout = _start(client, w, "credit")
    (call,) = w["fake"].posts()
    body = call["body"]
    assert call["path"] == "/v1/checkout/sessions"
    assert call["headers"]["Authorization"] == f"Bearer {KEY}"
    assert call["headers"]["Idempotency-Key"] == f"checkout-{checkout.pk}-{checkout.nonce[:24]}"
    assert call["headers"]["Stripe-Version"] == stripe_api.API_VERSION
    assert body["payment_method_types"] == ["card"]
    assert body["payment_method_options"]["card"]["restrictions"]["funding_types_blocked"] == [
        "debit",
        "prepaid",
    ]
    assert body["wallet_options"]["link"]["display"] == "never"
    base, surcharge = body["line_items"]
    assert base["price_data"]["unit_amount"] == "10000"
    assert surcharge["price_data"]["unit_amount"] == "300"
    assert "surcharge" in surcharge["price_data"]["product_data"]["name"]
    assert body["metadata"] == {
        "pending_checkout": str(checkout.pk),
        "nonce": checkout.nonce,
        "lot_id": str(w["lot"].pk),
        "account_id": str(checkout.account_id),
        "person_id": str(checkout.person_id),
    }
    assert body["payment_intent_data"]["metadata"] == body["metadata"]
    assert body["client_reference_id"] == f"pc{checkout.pk}"
    assert body["success_url"].endswith("?session_id={CHECKOUT_SESSION_ID}")
    assert len(checkout.nonce) == 64 and checkout.total_cents == 10_300
    assert checkout.charge_ids == [w["c1"].pk] and checkout.livemode is False


def test_debit_and_bank_sessions_have_no_fee(client, w):
    debit = _start(client, w, "debit")
    bank = _start(client, w, "bank")
    debit_body, bank_body = (c["body"] for c in w["fake"].posts())
    assert len(debit_body["line_items"]) == 1 and debit.total_cents == 10_000
    assert debit_body["payment_method_options"]["card"]["restrictions"][
        "funding_types_blocked"
    ] == ["credit"]
    assert bank_body["payment_method_types"] == ["us_bank_account"]
    assert bank_body["payment_method_options"]["us_bank_account"]["verification_method"] == (
        "automatic"
    )
    assert len(bank_body["line_items"]) == 1 and bank.surcharge_cents == 0
    keys = {c["headers"]["Idempotency-Key"] for c in w["fake"].posts()}
    assert len(keys) == 2


def test_pay_page_shows_each_fee_and_total_before_redirecting(client, w):
    client.force_login(w["owner"])
    page = client.post(
        reverse("pay_online", args=[w["lot"].pk]), {"charge": [w["c1"].pk, w["c2"].pk]}
    ).content.decode()
    assert "TEST MODE" in page
    assert "Bank account (no fee)" in page and "Debit card (no fee)" in page
    assert "Credit card (+3% surcharge)" in page and "$150.00" in page and "$154.50" in page
    assert (
        "A 3% surcharge applies to credit card payments; no surcharge on debit cards or bank "
        "payments. The surcharge is not greater than our cost of accepting credit cards." in page
    )


def test_typed_amount_is_limited_to_the_balance(client, w):
    client.force_login(w["owner"])
    page = client.post(reverse("pay_online", args=[w["lot"].pk]), {"amount": "500.00"})
    assert b"up to the balance" in page.content
    page = client.post(reverse("pay_online", args=[w["lot"].pk]), {"amount": "20.00"})
    assert b"$20.60" in page.content  # credit card total


# --- Recording what Stripe confirms ---------------------------------------------------------


def test_paid_card_session_is_recorded_once_against_the_chosen_charge(
    client, w, django_capture_on_commit_callbacks
):
    checkout = _start(client, w, "credit", charges=[w["c2"]])
    w["fake"].pay(checkout.session_id, funding="credit")
    with django_capture_on_commit_callbacks(execute=True):
        page = _return(client, checkout)
    assert b"your payment was received" in page.content
    assert _refresh(checkout).state == CheckoutState.PAID
    assert allocation.charge_status(w["c2"]) == "paid"
    assert allocation.charge_status(w["c1"]) == "open"
    assert ledger.balance(w["lot"].pk) == 10_000  # surcharge is not an owner charge
    _return(client, checkout)  # replay: idempotent
    sync.poll_open_checkouts()
    with system_access("t"):
        assert StripePayment.objects.count() == 1
        payment = StripePayment.objects.get()
    assert payment.entry.reference == payment.payment_intent_id
    assert payment.charge_id.startswith("ch_") and payment.balance_transaction_id.startswith("txn")
    assert payment.fee_cents and payment.funding == "credit"
    assert AuditEvent.objects.filter(action="payments.recorded").count() == 1
    (receipt,) = [m for m in mail.outbox if "Payment received" in m.subject]
    assert "Credit card surcharge: $1.50" in receipt.body
    assert payment.payment_intent_id in receipt.body and "Gate key" in receipt.body


def test_unpaid_session_records_nothing(client, w):
    checkout = _start(client, w)
    page = _return(client, checkout)
    assert b"Waiting for Stripe" in page.content
    assert _refresh(checkout).state == CheckoutState.OPEN
    with system_access("t"):
        assert not StripePayment.objects.exists()


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("amount_total", 1, "amount"),
        ("currency", "eur", "currency"),
        ("livemode", True, "livemode"),
        ("client_reference_id", "pc999", "client_reference"),
        ("metadata", {"pending_checkout": "1", "nonce": "x"}, "metadata"),
    ],
)
def test_any_mismatch_records_nothing_and_tells_the_board(client, w, field, value, code):
    checkout = _start(client, w)
    w["fake"].pay(checkout.session_id)
    w["fake"].sessions[checkout.session_id][field] = value
    page = _return(client, checkout)
    assert b"could not confirm" in page.content
    assert _refresh(checkout).state == CheckoutState.MISMATCH
    with system_access("t"):
        assert not StripePayment.objects.exists()
        alert = PaymentAlert.objects.get(kind=AlertKind.MISMATCH)
    assert ledger.balance(w["lot"].pk) == 15_000
    event = AuditEvent.objects.get(action="payments.mismatch")
    assert event.metadata == {"checkout_id": checkout.pk, "code": code}
    client.force_login(w["board"])
    items = client.get(reverse("my_items")).content.decode()
    assert "did not match" in items
    assert alert.detail


def test_payment_intent_amount_or_capture_mismatch_is_refused(client, w):
    checkout = _start(client, w)
    _pi_id, ch_id = w["fake"].pay(checkout.session_id)
    w["fake"].charges[ch_id]["captured"] = False
    _return(client, checkout)
    assert _refresh(checkout).state == CheckoutState.MISMATCH
    second = _start(client, w)
    pi2, _ = w["fake"].pay(second.session_id)
    w["fake"].intents[pi2]["amount_received"] = 1
    _return(client, second)
    assert _refresh(second).state == CheckoutState.MISMATCH


def test_a_payment_intent_cannot_pay_two_checkouts(client, w):
    first = _start(client, w)
    pi_id, _ = w["fake"].pay(first.session_id)
    _return(client, first)
    second = _start(client, w, charges=[w["c2"]])
    w["fake"].sessions[second.session_id].update(
        status="complete", payment_status="paid", payment_intent=pi_id
    )
    _return(client, second)
    assert _refresh(second).state == CheckoutState.MISMATCH
    with system_access("t"):
        assert StripePayment.objects.count() == 1


def test_forged_or_foreign_session_ids_are_refused(client, w):
    checkout = _start(client, w)
    w["fake"].pay(checkout.session_id)
    assert _return(client, checkout, session_id="cs_test_forged1234").status_code == 404
    client.force_login(w["owner_b"])
    assert _return(client, checkout).status_code == 404
    co_owner = make_member(lots=[w["lot"]])  # same lot, different person
    client.force_login(co_owner)
    assert _return(client, checkout).status_code == 404
    assert _refresh(checkout).state == CheckoutState.OPEN


# --- Bank payments (ACH) -------------------------------------------------------------------


def test_bank_payment_processes_then_succeeds(client, w):
    checkout = _start(client, w, "bank")
    pi_id, _ = w["fake"].pay(checkout.session_id, bank=True, status="processing")
    page = _return(client, checkout)
    assert b"bank payment is processing" in page.content
    assert _refresh(checkout).state == CheckoutState.PROCESSING
    assert ledger.balance(w["lot"].pk) == 15_000
    sync.poll_processing()
    assert _refresh(checkout).state == CheckoutState.PROCESSING
    w["fake"].settle_ach(pi_id)
    sync.poll_processing()
    assert _refresh(checkout).state == CheckoutState.PAID
    assert ledger.balance(w["lot"].pk) == 5_000


def test_bank_payment_that_fails_records_nothing(client, w):
    checkout = _start(client, w, "bank")
    pi_id, _ = w["fake"].pay(checkout.session_id, bank=True, status="processing")
    _return(client, checkout)
    w["fake"].settle_ach(pi_id, ok=False)
    sync.poll_processing()
    assert _refresh(checkout).state == CheckoutState.FAILED
    assert ledger.balance(w["lot"].pk) == 15_000


def test_returned_bank_payment_is_reversed_and_the_board_told(client, w):
    checkout = _start(client, w, "bank")
    _pi_id, ch_id = w["fake"].pay(checkout.session_id, bank=True)
    _return(client, checkout)
    assert ledger.balance(w["lot"].pk) == 5_000
    w["fake"].charges[ch_id]["status"] = "failed"
    sync.poll_recorded()
    sync.poll_recorded()  # once only
    assert ledger.balance(w["lot"].pk) == 15_000
    assert allocation.charge_status(w["c1"]) == "open"
    with system_access("t"):
        assert PaymentAlert.objects.filter(kind=AlertKind.RETURNED).count() == 1


def test_refunds_and_disputes_become_board_items_never_edits(client, w):
    checkout = _start(client, w, "debit")
    _pi_id, ch_id = w["fake"].pay(checkout.session_id, funding="debit")
    _return(client, checkout)
    w["fake"].refunds[ch_id] = [{"id": "re_test123456", "amount": 2_500, "status": "succeeded"}]
    w["fake"].disputes[ch_id] = [{"id": "dp_test123456", "amount": 10_000}]
    sync.poll_recorded()
    sync.poll_recorded()
    with system_access("t"):
        kinds = sorted(PaymentAlert.objects.values_list("kind", flat=True))
    assert kinds == [AlertKind.DISPUTE, AlertKind.REFUND]
    assert ledger.balance(w["lot"].pk) == 5_000  # nothing changed by itself


# --- Surcharge safety net ------------------------------------------------------------------


def test_debit_card_on_the_surcharge_session_asks_the_board_to_refund_it(client, w):
    checkout = _start(client, w, "credit")
    pi_id, ch_id = w["fake"].pay(checkout.session_id, funding="debit")
    _return(client, checkout)
    with system_access("t"):
        alert = PaymentAlert.objects.get(kind=AlertKind.SURCHARGE_REFUND)
    assert alert.amount_cents == 300 and alert.stripe_ref == pi_id
    assert "Refund $3.00 surcharge to lot" in alert.detail and "(debit card)" in alert.detail
    assert AuditEvent.objects.filter(action="payments.surcharge_refund_due").exists()
    assert not [c for c in w["fake"].posts() if "refund" in c["path"]]
    w["fake"].refunds[ch_id] = [{"id": "re_test999999", "amount": 300, "status": "succeeded"}]
    sync.poll_recorded()
    alert.refresh_from_db()
    assert alert.resolved_at is not None
    with system_access("t"):
        assert not PaymentAlert.objects.filter(kind=AlertKind.REFUND).exists()


def test_credit_card_on_a_no_fee_session_is_absorbed_and_only_counted(client, w, caplog):
    checkout = _start(client, w, "debit")
    w["fake"].pay(checkout.session_id, funding="credit")
    with caplog.at_level(logging.INFO, logger="payments"):
        _return(client, checkout)
    assert "payments.credit_card_on_no_fee_session" in caplog.text
    with system_access("t"):
        assert not PaymentAlert.objects.exists()


def test_the_portal_has_no_way_to_write_refunds():
    """The Stripe key is restricted (refunds read-only); the code never posts
    anything but a new Checkout Session."""
    root = pathlib.Path(__file__).resolve().parents[1]
    code = "\n".join(p.read_text() for p in root.glob("*.py"))
    assert re.findall(r'post\(\s*"(/v1/[^"]+)"', code) == ["/v1/checkout/sessions"]
    assert "stripe_api.post(" not in code


# --- Who may pay ---------------------------------------------------------------------------


def _contact(lot, kind):
    user = UserFactory()
    person = community.record_person(display_name=f"{kind} person", actor=SYSTEM)
    with system_access("t"):
        Person.objects.filter(pk=person.pk).update(user=user)
        account = Account.objects.get(lot=lot, closed_on__isnull=True)
    person.refresh_from_db()
    enroll_totp(user)
    community.add_account_contact(account, person=person, kind=kind, actor=SYSTEM)
    return user


def test_only_owners_and_authorized_contacts_may_pay(client, w):
    authorized = _contact(w["lot"], "authorized")
    _start(client, w, user=authorized)
    for user in (
        _contact(w["lot"], "view_only"),
        _contact(w["lot"], "tenant"),
        make_member(roles=[Role.MANAGER]),
        w["owner_b"],
    ):
        client.force_login(user)
        for name, method in (("pay_online", "get"), ("pay_checkout", "post")):
            response = getattr(client, method)(
                reverse(name, args=[w["lot"].pk]), {"method": "bank", "charge": w["c1"].pk}
            )
            assert response.status_code in (403, 404), (user, name)
    with pytest.raises(PermissionDenied):
        flow.start(w["owner_b"], w["lot"], method="bank", charge_ids=[w["c1"].pk])


def test_former_owner_cannot_pay(client, w):
    seller = w["owner"]
    buyer = community.record_person(display_name="Buyer", actor=SYSTEM)
    community.transfer_lot(w["lot"], new_owners=[buyer], transfer_date=days_ago(0), actor=SYSTEM)
    client.force_login(seller)
    response = client.get(reverse("pay_online", args=[w["lot"].pk]))
    assert response.status_code in (403, 404)


def test_owner_cannot_pick_another_homes_charge(client, w):
    with system_access("t"):
        other_charge = ledger.LedgerEntry.objects.filter(lot=w["other_lot"]).first()
    client.force_login(w["owner"])
    page = client.post(
        reverse("pay_checkout", args=[w["lot"].pk]),
        {"method": "bank", "charge": other_charge.pk},
    )
    assert page.status_code == 200 and b"Choose charges from your open charges" in page.content
    assert not w["fake"].posts()


def test_receipts_are_only_for_that_account(client, w):
    checkout = _start(client, w)
    w["fake"].pay(checkout.session_id)
    _return(client, checkout)
    with system_access("t"):
        payment = StripePayment.objects.get()
    url = reverse("payment_receipt", args=[payment.pk])
    assert url in client.get(reverse("my_account")).content.decode()
    response = client.get(url)
    assert response.status_code == 200 and response["Content-Type"] == "application/pdf"
    body = (
        b"".join(response.streaming_content)
        if hasattr(response, "streaming_content")
        else response.content
    )
    assert b"PAYMENT RECEIPT" in body
    client.force_login(w["owner_b"])
    assert client.get(url).status_code == 404
    client.force_login(w["board"])
    assert client.get(url).status_code == 200
    page = client.get(reverse("online_payments")).content.decode()
    assert "TEST MODE" in page and payment.payment_intent_id in page


# --- Logs ----------------------------------------------------------------------------------


def test_errors_log_status_and_code_only(client, w, caplog):
    w["fake"].fail_next = (
        402,
        {"error": {"code": "card_declined", "message": "secret_body customer@example.com"}},
    )
    with caplog.at_level(logging.DEBUG):
        client.force_login(w["owner"])
        page = client.post(
            reverse("pay_checkout", args=[w["lot"].pk]),
            {"method": "credit", "charge": w["c1"].pk},
        )
    assert b"could not start" in page.content
    text = caplog.text + repr([r.__dict__ for r in caplog.records])
    assert "card_declined" in text
    for secret in (KEY, "secret_body", "customer@example.com", "nonce"):
        assert secret not in text


def test_server_errors_retry_posts_with_the_same_key(w):
    calls = []

    def flaky(method, url, data, headers):
        calls.append(headers.get("Idempotency-Key"))
        if len(calls) < 3:
            return 503, b"{}"
        return w["fake"](method, url, data, headers)

    with mock.patch.object(stripe_api, "transport", flaky):
        stripe_api.create_checkout_session(
            {"mode": "payment", "line_items": []}, idempotency_key="k-1"
        )
    assert calls == ["k-1", "k-1", "k-1"]


def test_only_api_stripe_com_with_safe_ids(w):
    with pytest.raises(stripe_api.StripeError):
        stripe_api.retrieve_charge("../v1/refunds")
    with pytest.raises(stripe_api.StripeError):
        stripe_api._transport("GET", "https://evil.example.com/v1/x", None, {})


# --- Append-only ---------------------------------------------------------------------------


def test_stripe_records_are_append_only(client, w):
    checkout = _start(client, w)
    w["fake"].pay(checkout.session_id)
    _return(client, checkout)
    with system_access("t"):
        payment = StripePayment.objects.get()
    with pytest.raises(AppendOnlyError):
        payment.save()
    with pytest.raises(AppendOnlyError):
        payment.delete()
    with pytest.raises(PermissionError):
        _refresh(checkout).delete()


# --- Payouts and the three-way reconciliation -----------------------------------------------


def _paid(client, w, method="credit", funding="credit"):
    checkout = _start(client, w, method)
    w["fake"].pay(checkout.session_id, funding=funding)
    _return(client, checkout)
    return checkout


def test_payouts_are_synced_and_offered_for_bank_matching(client, w):
    _paid(client, w)
    po_id = w["fake"].payout(list(w["fake"].balance))
    sync.sync_balance()
    sync.sync_balance()
    today = timezone.localdate()
    (row,) = sync.payouts_for_matching(today - datetime.timedelta(days=3), today)
    assert row["payout_id"] == po_id and row["adds_up"]
    assert row["gross_cents"] == 10_300
    assert row["amount_cents"] == 10_300 - row["fees_cents"]
    run = sync.reconcile()
    assert run.ok, run.problems
    from core.health import run_checks

    (health,) = [r for r in run_checks() if r.key == "payments"]
    assert health.ok


def test_reconciliation_catches_money_that_never_reached_stripe(
    client, w, django_capture_on_commit_callbacks
):
    _paid(client, w)
    w["fake"].balance.clear()  # Stripe has no such charge (a bug or a forged record)
    sync.sync_balance()
    with django_capture_on_commit_callbacks(execute=True):
        run = sync.reconcile(now=timezone.now() + datetime.timedelta(days=2))
    assert not run.ok
    assert {"code": "ledger_without_stripe", "id": _payment_pi()} in run.problems
    from core.health import run_checks

    (health,) = [r for r in run_checks() if r.key == "payments"]
    assert not health.ok
    assert any("do not match Stripe" in m.subject for m in mail.outbox)
    client.force_login(w["board"])  # no treasurer recorded: the whole board sees it
    assert "Stripe records do not match" in client.get(reverse("my_items")).content.decode()


def _payment_pi():
    with system_access("t"):
        return StripePayment.objects.get().payment_intent_id


def test_reconciliation_catches_stripe_charges_missing_from_the_ledger(w):
    w["fake"].balance.append(
        {
            "id": "txn_test_orphan1",
            "type": "charge",
            "source": "ch_test_orphan1",
            "amount": 999,
            "fee": 59,
            "net": 940,
            "created": int(timezone.now().timestamp()) - 3 * 86400,
            "livemode": False,
        }
    )
    sync.sync_balance()
    run = sync.reconcile()
    assert {"code": "stripe_without_ledger", "id": "ch_test_orphan1"} in run.problems


def test_reconciliation_catches_a_payout_that_does_not_add_up(client, w):
    _paid(client, w)
    w["fake"].payout(list(w["fake"].balance), amount=1)
    sync.sync_balance()
    run = sync.reconcile()
    assert any(p["code"] == "payout_does_not_add_up" for p in run.problems)


def test_bank_hook_problems_fail_the_reconciliation(client, w):
    _paid(client, w)
    sync.sync_balance()
    with mock.patch.object(
        sync, "check_payouts_against_bank", return_value=[{"code": "bank", "id": "po_x"}]
    ):
        assert not sync.reconcile().ok


def test_accounting_export_balances_with_surcharge_fees_and_payouts(client, w):
    from ledger.accounting import journal_rows

    _paid(client, w)
    w["fake"].payout(list(w["fake"].balance))
    sync.sync_balance()
    today = timezone.localdate()
    rows = journal_rows(today - datetime.timedelta(days=10), today + datetime.timedelta(days=1))
    debits = sum(int(r[4].replace(".", "")) for r in rows if r[4])
    credits = sum(int(r[5].replace(".", "")) for r in rows if r[5])
    assert debits == credits
    accounts = {r[3] for r in rows}
    assert "4300 Credit card surcharge income" in accounts
    assert "6150 Payment processing fees" in accounts and "1050 Stripe clearing" in accounts
    from ledger.accounting import undeposited_payments

    assert undeposited_payments() == []  # online money arrives by payout, not deposit


def test_health_is_quiet_when_payments_are_not_set_up(settings):
    settings.PAYMENTS_PROVIDER = "none"
    assert sync.health() == (True, "Not set up")


def test_checkout_starts_are_rate_limited(client, w, monkeypatch):
    monkeypatch.setattr(flow, "MAX_STARTS_PER_HOUR", 2)
    _start(client, w)
    _start(client, w)
    page = client.post(
        reverse("pay_checkout", args=[w["lot"].pk]), {"method": "bank", "charge": w["c1"].pk}
    )
    assert b"Too many payment attempts" in page.content
    assert len(w["fake"].posts()) == 2


def test_board_closes_an_item_with_a_note(client, w):
    checkout = _start(client, w, "credit")
    w["fake"].pay(checkout.session_id, funding="debit")
    _return(client, checkout)
    with system_access("t"):
        alert = PaymentAlert.objects.get()
    url = reverse("payment_alert_resolve", args=[alert.pk])
    client.force_login(w["owner"])
    assert client.post(url, {"note": "x"}).status_code == 403
    manager = make_member(roles=[Role.MANAGER])
    client.force_login(manager)
    assert client.post(url, {"note": "x"}).status_code == 403
    client.force_login(w["board"])
    client.post(url, {"note": "Refunded in Stripe"})
    alert.refresh_from_db()
    assert alert.resolved_by == w["board"] and alert.resolution == "Refunded in Stripe"
    assert AuditEvent.objects.filter(action="payments.alert_resolved").exists()
