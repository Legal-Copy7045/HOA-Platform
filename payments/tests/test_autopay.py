"""Automatic payment of the monthly assessment (decision D40-autopay). A fake
stands in for api.stripe.com: nothing here reaches the network."""

import datetime
import json
import re

import pytest
from django.core import mail
from django.urls import reverse
from django.utils import timezone

from audit.models import AuditEvent
from community import services as community
from community.models import Account, Person, Role
from community.tests.factories import UserFactory, days_ago, enroll_totp, make_member
from core.models import FeatureSetting
from core.policy import SYSTEM
from core.scoping import system_access
from ledger import allocation
from ledger import services as ledger
from ledger.models import LedgerEntry
from payments import autopay, sync
from payments.models import (
    AlertKind,
    AutopayAttempt,
    AutopayEvent,
    AutopayEventKind,
    AutopaySetup,
    CheckoutState,
    PaymentAlert,
    PendingCheckout,
    SetupState,
    StripeCustomer,
    StripePayment,
)
from payments.tests.test_stripe import KEY

pytestmark = pytest.mark.django_db

MONTHLY = 20_000
DUE_DAY = 5


def _next_due() -> datetime.date:
    today = timezone.localdate()
    year, month = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
    return datetime.date(year, month, DUE_DAY)


@pytest.fixture
def aw(w, monkeypatch):
    """The two-lot world of test_stripe, with dues, the feature on, and the
    calendar moved to next month's due day (enrolment always happens before
    it)."""
    board = w["board"]
    community.set_rule(key="dues.monthly_cents", value="20000", citation="Decl. 8.1", actor=board)
    community.set_rule(key="dues.due_day", value=str(DUE_DAY), citation="Decl. 8.2", actor=board)
    FeatureSetting.objects.create(key="autopay", enabled=True)
    due = _next_due()
    monkeypatch.setattr(autopay, "_today", lambda: due)
    return {**w, "due": due, "period": f"{due:%Y-%m}"}


def _fresh(client, user):
    """Signed in with a recent password check (D24-style money set-up)."""
    client.force_login(user)
    session = client.session
    session["account_authentication_methods_user"] = str(user.pk)
    session["account_authentication_methods"] = [
        {"method": "password", "at": timezone.now().timestamp(), "reauthenticated": True}
    ]
    session.save()


def _setup(pk=None):
    with system_access("t"):
        return AutopaySetup.objects.get(pk=pk) if pk else AutopaySetup.objects.latest("id")


def _start(client, aw, method="card", user=None, lot=None):
    user = user or aw["owner"]
    lot = lot or aw["lot"]
    _fresh(client, user)
    response = client.post(reverse("autopay_start", args=[lot.pk]), {"method": method})
    assert response.status_code == 302, response.content[:300]
    assert response["Location"].startswith("https://checkout.stripe.com/")
    return _setup()


def _enrol(client, aw, method="card", user=None, lot=None, capture=None, **saved):
    setup = _start(client, aw, method, user=user, lot=lot)
    aw["fake"].complete_setup(setup.session_id, **saved)
    response = client.get(
        reverse("autopay_return", args=[setup.pk]), {"session_id": setup.session_id}
    )
    assert response.status_code == 302 and response["Location"] == reverse("my_account")
    return _setup(setup.pk)


def _account(lot):
    with system_access("t"):
        return Account.objects.get(lot=lot, closed_on__isnull=True)


def _post_dues(aw):
    due = aw["due"]
    ledger.post_monthly_assessments(year=due.year, month=due.month, actor=aw["board"])
    with system_access("t"):
        return LedgerEntry.objects.get(lot=aw["lot"], kind="assessment", period=aw["period"])


def _intent_posts(fake):
    return [c for c in fake.posts() if c["path"] == "/v1/payment_intents"]


def _payments():
    with system_access("t"):
        return list(StripePayment.objects.all())


def _events(kind=None):
    with system_access("t"):
        qs = AutopayEvent.objects.all()
        return list(qs.filter(kind=kind) if kind else qs)


# --- Enrolment -----------------------------------------------------------------------------


def test_card_enrolment_end_to_end(client, aw):
    _fresh(client, aw["owner"])
    page = client.get(reverse("autopay", args=[aw["lot"].pk])).content.decode()
    assert "<h1>Pay assessments automatically</h1>" in page
    assert "Bank account (no fee)" in page and "credit cards add the 3% surcharge" in page
    assert "$6.00 each month" in page and "TEST MODE" in page
    setup = _enrol(client, aw, "card")
    fake = aw["fake"]
    customer_call, session_call = fake.posts()
    # Only internal ids go to Stripe: no name, email or address.
    assert customer_call["path"] == "/v1/customers"
    assert customer_call["body"] == {
        "metadata": {"account_id": str(setup.account_id), "lot_id": str(aw["lot"].pk)}
    }
    body = session_call["body"]
    assert body["mode"] == "setup" and body["customer"] == setup.customer_id
    assert body["payment_method_types"] == ["card"] and "line_items" not in body
    assert body["client_reference_id"] == f"as{setup.pk}"
    assert body["metadata"]["nonce"] == setup.nonce
    assert body["setup_intent_data"]["metadata"] == body["metadata"]
    assert body["wallet_options"]["link"]["display"] == "never"
    assert setup.state == SetupState.DONE
    (event,) = _events()
    assert event.kind == AutopayEventKind.ENROLLED and event.method_type == "card"
    assert (event.brand, event.last4, event.funding) == ("visa", "4242", "credit")
    assert event.user_id == aw["owner"].pk and event.payment_method_id.startswith("pm_")
    st = autopay.state(setup.account_id)
    assert st.status == autopay.ON and st.label == "Visa ending 4242"
    audit = AuditEvent.objects.get(action="payments.autopay_enrolled")
    assert audit.metadata["method"] == "card" and audit.metadata["replaced"] is False
    my_account = client.get(reverse("my_account")).content.decode()
    assert "This will be paid automatically on" in my_account and "Visa ending 4242" in my_account
    with system_access("t"):
        assert StripeCustomer.objects.count() == 1


def test_bank_enrolment_waits_for_verification_and_keeps_the_mandate(client, aw):
    setup = _start(client, aw, "us_bank_account")
    body = aw["fake"].posts()[-1]["body"]
    assert body["payment_method_types"] == ["us_bank_account"]
    assert body["payment_method_options"]["us_bank_account"]["verification_method"] == "automatic"
    seti_id, _pm = aw["fake"].complete_setup(setup.session_id, status="requires_action")
    client.get(reverse("autopay_return", args=[setup.pk]), {"session_id": setup.session_id})
    assert _setup(setup.pk).state == SetupState.WAITING
    assert autopay.state(setup.account_id).status == autopay.OFF
    aw["fake"].setup_intents[seti_id]["status"] = "succeeded"  # micro-deposits verified
    autopay.poll_setups()
    assert _setup(setup.pk).state == SetupState.DONE
    (event,) = _events()
    assert event.mandate_id.startswith("mandate_") and event.bank_name == "STRIPE TEST BANK"
    assert autopay.state(setup.account_id).label == "STRIPE TEST BANK account ending 4242"


def test_set_up_needs_a_fresh_password_check(client, aw):
    client.force_login(aw["owner"])
    response = client.get(reverse("autopay", args=[aw["lot"].pk]))
    assert response.status_code == 302 and "reauthenticate" in response["Location"]
    response = client.post(reverse("autopay_start", args=[aw["lot"].pk]), {"method": "card"})
    assert response.status_code == 302 and "reauthenticate" in response["Location"]
    assert not aw["fake"].posts()


def test_a_tampered_return_is_refused(client, aw):
    setup_b = _start(client, aw, "card", user=aw["owner_b"], lot=aw["other_lot"])
    setup_a = _start(client, aw, "card")
    aw["fake"].complete_setup(setup_b.session_id)
    # Another person's set-up, or another set-up's session id: 404, nothing saved.
    response = client.get(
        reverse("autopay_return", args=[setup_b.pk]), {"session_id": setup_b.session_id}
    )
    assert response.status_code == 404
    response = client.get(
        reverse("autopay_return", args=[setup_a.pk]), {"session_id": setup_b.session_id}
    )
    assert response.status_code == 404
    assert not _events()
    # Stripe's record changed (nonce): nothing saved, audited.
    aw["fake"].complete_setup(setup_a.session_id)
    aw["fake"].sessions[setup_a.session_id]["metadata"]["nonce"] = "0" * 64
    client.get(reverse("autopay_return", args=[setup_a.pk]), {"session_id": setup_a.session_id})
    assert _setup(setup_a.pk).state == SetupState.MISMATCH
    assert not _events()
    assert AuditEvent.objects.get(action="payments.autopay_setup_mismatch").metadata == {
        "setup_id": setup_a.pk,
        "code": "metadata",
    }


def test_a_payment_method_of_another_customer_is_refused(client, aw):
    setup = _start(client, aw, "card")
    _seti, pm_id = aw["fake"].complete_setup(setup.session_id)
    aw["fake"].payment_methods[pm_id]["customer"] = "cus_test_someoneelse"
    client.get(reverse("autopay_return", args=[setup.pk]), {"session_id": setup.session_id})
    assert _setup(setup.pk).state == SetupState.MISMATCH and not _events()


def _contact(lot, kind):
    user = UserFactory()
    person = community.record_person(display_name=f"{kind} person", actor=SYSTEM)
    with system_access("t"):
        Person.objects.filter(pk=person.pk).update(user=user)
    enroll_totp(user)
    community.add_account_contact(_account(lot), person=person, kind=kind, actor=SYSTEM)
    return user


def test_only_owners_and_authorized_contacts_may_set_it_up(client, aw):
    authorized = _contact(aw["lot"], "authorized")
    _enrol(client, aw, "card", user=authorized)
    for user in (
        _contact(aw["lot"], "view_only"),
        _contact(aw["lot"], "tenant"),
        make_member(roles=[Role.MANAGER]),
        aw["board"],
        aw["owner_b"],
    ):
        _fresh(client, user)
        for name, method in (
            ("autopay", "get"),
            ("autopay_start", "post"),
            ("autopay_cancel", "post"),
        ):
            response = getattr(client, method)(
                reverse(name, args=[aw["lot"].pk]), {"method": "card"}
            )
            assert response.status_code in (403, 404), (user, name)
    assert autopay.state(_account(aw["lot"]).pk).status == autopay.ON  # nothing cancelled


def test_a_former_owner_cannot_set_it_up(client, aw):
    buyer = community.record_person(display_name="Buyer", actor=SYSTEM)
    community.transfer_lot(aw["lot"], new_owners=[buyer], transfer_date=days_ago(0), actor=SYSTEM)
    _fresh(client, aw["owner"])
    assert client.get(reverse("autopay", args=[aw["lot"].pk])).status_code in (403, 404)


def test_owner_a_reaches_nothing_of_owner_b(client, aw):
    setup_b = _enrol(client, aw, "card", user=aw["owner_b"], lot=aw["other_lot"])
    _fresh(client, aw["owner"])
    lot_b = aw["other_lot"].pk
    assert client.get(reverse("autopay", args=[lot_b])).status_code == 404
    assert client.post(reverse("autopay_start", args=[lot_b]), {"method": "card"}).status_code == (
        404
    )
    assert client.post(reverse("autopay_cancel", args=[lot_b])).status_code == 404
    assert client.get(reverse("autopay_return", args=[setup_b.pk])).status_code == 404
    assert autopay.state(setup_b.account_id).status == autopay.ON


def test_feature_off_means_404_and_no_charges(client, aw, django_capture_on_commit_callbacks):
    _enrol(client, aw, "card")
    _post_dues(aw)
    FeatureSetting.objects.filter(key="autopay").update(enabled=False)
    _fresh(client, aw["owner"])
    lot = aw["lot"].pk
    for name, method in (
        ("autopay", "get"),
        ("autopay_start", "post"),
        ("autopay_cancel", "post"),
    ):
        assert getattr(client, method)(reverse(name, args=[lot])).status_code == 404
    assert client.get(reverse("autopay_return", args=[_setup().pk])).status_code == 404
    assert autopay.run() == 0 and not _intent_posts(aw["fake"])
    assert autopay.describe_for_statement(_account(aw["lot"])) is None
    # Online payments off switches it off too.
    FeatureSetting.objects.filter(key="autopay").update(enabled=True)
    FeatureSetting.objects.filter(key="online_payments").update(enabled=False)
    assert client.get(reverse("autopay", args=[lot])).status_code == 404
    assert autopay.run() == 0 and not _intent_posts(aw["fake"])


def test_test_keys_are_still_refused_in_production(client, aw, settings):
    _enrol(client, aw, "card")
    _post_dues(aw)
    settings.DEPLOYMENT_STAGE = "production"
    assert autopay.autopay_enabled() is False
    assert autopay.run() == 0 and not _intent_posts(aw["fake"])


# --- Charging ------------------------------------------------------------------------------


def test_the_month_is_charged_once_with_the_card_surcharge(
    client, aw, django_capture_on_commit_callbacks
):
    setup = _enrol(client, aw, "card")
    dues = _post_dues(aw)
    with django_capture_on_commit_callbacks(execute=True):
        autopay.run()
        autopay.run()
        autopay.charge_due(aw["due"] + datetime.timedelta(days=1))
    (call,) = _intent_posts(aw["fake"])
    body = call["body"]
    assert body["amount"] == "20600" and body["currency"] == "usd"  # 20,000 + 3%
    assert body["off_session"] == "true" and body["confirm"] == "true"
    assert body["customer"] == setup.customer_id and body["payment_method_types"] == ["card"]
    assert call["headers"]["Idempotency-Key"] == (f"autopay-{setup.account_id}-{aw['period']}-1")
    (payment,) = _payments()
    assert payment.base_cents == MONTHLY and payment.surcharge_cents == 600
    assert payment.method == "credit" and payment.funding == "credit"
    assert allocation.charge_status(dues) == "paid"
    # Only the month's assessment: the other open charges stay open.
    assert allocation.charge_status(aw["c1"]) == "open"
    assert allocation.charge_status(aw["c2"]) == "open"
    assert any("Payment received" in m.subject for m in mail.outbox)
    with system_access("t"):
        attempt = AutopayAttempt.objects.get()
    assert attempt.checkout.state == CheckoutState.PAID and attempt.charge_id == dues.pk
    assert AuditEvent.objects.filter(action="payments.recorded").count() == 1


def test_debit_cards_and_bank_accounts_carry_no_fee(client, aw):
    _enrol(client, aw, "card", funding="debit")
    _enrol(client, aw, "us_bank_account", user=aw["owner_b"], lot=aw["other_lot"])
    _post_dues(aw)
    autopay.run()
    bodies = {c["body"]["payment_method_types"][0]: c["body"] for c in _intent_posts(aw["fake"])}
    assert bodies["card"]["amount"] == "20000" and "mandate" not in bodies["card"]
    assert bodies["us_bank_account"]["amount"] == "20000"
    assert bodies["us_bank_account"]["mandate"].startswith("mandate_")
    with system_access("t"):
        methods = sorted(PendingCheckout.objects.values_list("method", "surcharge_cents"))
    assert methods == [("bank", 0), ("debit", 0)]


def test_bank_payment_processing_then_succeeded_is_recorded_once(client, aw):
    setup = _enrol(client, aw, "us_bank_account")
    _post_dues(aw)
    aw["fake"].outcomes[_events()[0].payment_method_id] = ["processing"]
    autopay.run()
    with system_access("t"):
        attempt = AutopayAttempt.objects.get()
    assert attempt.checkout.state == CheckoutState.PROCESSING and not _payments()
    later = timezone.now() + datetime.timedelta(hours=7)
    autopay.poll_attempts(later)
    sync.poll_processing()  # the generic poller leaves automatic payments alone
    assert not _payments()
    aw["fake"].settle_ach(_pi(attempt))
    autopay.poll_attempts(later + datetime.timedelta(hours=7))
    autopay.poll_attempts(later + datetime.timedelta(hours=14))
    sync.poll_processing()
    (payment,) = _payments()
    assert payment.method == "bank" and payment.surcharge_cents == 0
    assert payment.entry.account_id == setup.account_id
    assert len(_intent_posts(aw["fake"])) == 1


def _pi(attempt):
    with system_access("t"):
        return PendingCheckout.objects.get(pk=attempt.checkout_id).payment_intent_id


def test_soft_decline_is_retried_once_then_paused(client, aw, django_capture_on_commit_callbacks):
    _enrol(client, aw, "card")
    dues = _post_dues(aw)
    pm = _events()[0].payment_method_id
    aw["fake"].outcomes[pm] = ["decline:insufficient_funds", "decline:insufficient_funds"]
    with django_capture_on_commit_callbacks(execute=True):
        autopay.run()
    (failure,) = _events(AutopayEventKind.FAILED)
    assert failure.code == "insufficient_funds" and not _payments()
    assert autopay.state(failure.account_id).status == autopay.ON
    (email,) = [m for m in mail.outbox if "didn't go through" in m.subject]
    assert "We will try once more" in email.body and "$200.00" in email.body
    assert aw["owner"].email in email.to
    # The owner's to-do.
    from core.my_items import items_for

    (item,) = [i for i in items_for(aw["owner"]) if "automatic payment" in i.title]
    assert "Pay now or update the payment method" in item.detail and item.action_label
    assert "try again" in autopay.describe_for_statement(_account(aw["lot"]))
    # Not before three days.
    autopay.retry_due(timezone.now() + datetime.timedelta(days=2))
    assert len(_intent_posts(aw["fake"])) == 1
    mail.outbox.clear()
    with django_capture_on_commit_callbacks(execute=True):
        autopay.retry_due(timezone.now() + datetime.timedelta(days=3, hours=1))
        autopay.retry_due(timezone.now() + datetime.timedelta(days=3, hours=2))
    assert len(_intent_posts(aw["fake"])) == 2
    assert len(_events(AutopayEventKind.FAILED)) == 2
    (paused,) = _events(AutopayEventKind.PAUSED)
    assert autopay.state(paused.account_id).status == autopay.PAUSED
    (email,) = [m for m in mail.outbox if "didn't go through" in m.subject]
    assert "paused" in email.body
    assert "paused" in autopay.describe_for_statement(_account(aw["lot"]))
    assert allocation.charge_status(dues) == "open"
    # Paused: no third try, and the next run charges nothing.
    autopay.retry_due(timezone.now() + datetime.timedelta(days=4))
    autopay.run()
    assert len(_intent_posts(aw["fake"])) == 2
    # Setting up a payment method again turns it back on.
    _enrol(client, aw, "card")
    assert autopay.state(paused.account_id).status == autopay.ON
    assert _events(AutopayEventKind.REPLACED)


def test_authentication_required_pauses_without_a_retry(
    client, aw, django_capture_on_commit_callbacks
):
    _enrol(client, aw, "card")
    _post_dues(aw)
    aw["fake"].outcomes[_events()[0].payment_method_id] = ["authentication_required"]
    with django_capture_on_commit_callbacks(execute=True):
        autopay.run()
    (failure,) = _events(AutopayEventKind.FAILED)
    assert failure.code == "authentication_required"
    assert autopay.state(failure.account_id).status == autopay.PAUSED
    autopay.retry_due(timezone.now() + datetime.timedelta(days=4))
    assert len(_intent_posts(aw["fake"])) == 1
    assert AuditEvent.objects.filter(action="payments.autopay_paused").count() == 1


def test_a_failed_bank_debit_is_recorded_as_a_failure(client, aw):
    _enrol(client, aw, "us_bank_account")
    _post_dues(aw)
    aw["fake"].outcomes[_events()[0].payment_method_id] = ["processing"]
    autopay.run()
    with system_access("t"):
        attempt = AutopayAttempt.objects.get()
    aw["fake"].settle_ach(_pi(attempt), ok=False, code="insufficient_funds")
    autopay.poll_attempts(timezone.now() + datetime.timedelta(hours=7))
    (failure,) = _events(AutopayEventKind.FAILED)
    assert failure.code == "insufficient_funds" and not _payments()
    with system_access("t"):
        assert PendingCheckout.objects.get(pk=attempt.checkout_id).state == CheckoutState.FAILED


def test_a_returned_bank_payment_pauses_autopay(client, aw):
    _enrol(client, aw, "us_bank_account")
    _post_dues(aw)
    autopay.run()
    (payment,) = _payments()
    aw["fake"].charges[payment.charge_id]["status"] = "failed"  # returned later
    sync.poll_recorded()
    autopay.poll_attempts()
    (failure,) = _events(AutopayEventKind.FAILED)
    assert failure.code == "returned"
    assert autopay.state(failure.account_id).status == autopay.PAUSED


def test_credit_on_the_account_is_used_first(client, aw):
    _enrol(client, aw, "card")
    ledger.record_payment(
        lot=aw["lot"],
        amount_cents=100_000,
        received_on=timezone.localdate(),
        reference="1001",
        actor=aw["board"],
    )
    dues = _post_dues(aw)
    assert allocation.charge_status(dues) == "paid"  # the prepayment drew down
    autopay.run()
    assert not _intent_posts(aw["fake"])
    text = autopay.describe_for_statement(_account(aw["lot"]))
    assert "credit on account covers" in text


def test_partial_credit_lowers_the_charge(client, aw):
    _enrol(client, aw, "us_bank_account")
    ledger.record_payment(
        lot=aw["lot"],
        amount_cents=15_000 + 5_000,  # the two open charges, then 50.00 credit
        received_on=timezone.localdate(),
        reference="1002",
        actor=aw["board"],
    )
    _post_dues(aw)
    autopay.run()
    (call,) = _intent_posts(aw["fake"])
    assert call["body"]["amount"] == "15000"


def test_an_amount_over_the_ceiling_is_held_for_the_board(client, aw):
    community.set_rule(
        key="payments.autopay_ceiling_percent",
        value="50",
        citation="Res. 2027-12",
        actor=aw["board"],
    )
    _enrol(client, aw, "card")
    _post_dues(aw)
    autopay.run()
    autopay.run()
    assert not _intent_posts(aw["fake"])
    with system_access("t"):
        (alert,) = PaymentAlert.objects.filter(kind=AlertKind.AUTOPAY)
    assert alert.amount_cents == MONTHLY and "ceiling" in alert.detail
    assert AuditEvent.objects.filter(action="payments.autopay_held").count() == 1


def test_set_up_after_the_due_date_starts_next_month(client, aw, monkeypatch):
    _enrol(client, aw, "card")
    last_month = timezone.localdate().replace(day=1) - datetime.timedelta(days=1)
    due = last_month.replace(day=DUE_DAY)
    ledger.post_monthly_assessments(year=due.year, month=due.month, actor=aw["board"])
    monkeypatch.setattr(autopay, "_today", lambda: due)
    assert autopay.charge_due(due) == 0 and not _intent_posts(aw["fake"])


def test_an_unanswered_charge_is_found_not_sent_twice(client, aw):
    _enrol(client, aw, "card")
    _post_dues(aw)
    aw["fake"].drop_responses = 3  # Stripe made it, but every answer was lost
    autopay.run()
    with system_access("t"):
        attempt = AutopayAttempt.objects.get()
    assert attempt.checkout.state == CheckoutState.OPEN and not attempt.checkout.payment_intent_id
    autopay.poll_attempts(timezone.now() + datetime.timedelta(minutes=11))
    (payment,) = _payments()
    assert len(aw["fake"].intents) == 1 and payment.base_cents == MONTHLY


# --- Cancelling, sales, replacing -----------------------------------------------------------


def test_cancel_is_audited_and_detaches_the_card(client, aw, django_capture_on_commit_callbacks):
    _enrol(client, aw, "card")
    pm = _events()[0].payment_method_id
    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(reverse("autopay_cancel", args=[aw["lot"].pk]))
    assert response.status_code == 302
    (cancelled,) = _events(AutopayEventKind.CANCELLED)
    assert cancelled.code == "owner" and cancelled.user_id == aw["owner"].pk
    assert autopay.state(cancelled.account_id).status == autopay.OFF
    assert any(c["path"] == f"/v1/payment_methods/{pm}/detach" for c in aw["fake"].posts())
    assert aw["fake"].payment_methods[pm]["customer"] is None
    assert AuditEvent.objects.get(action="payments.autopay_cancelled").metadata["code"] == "owner"
    _post_dues(aw)
    autopay.run()
    assert not _intent_posts(aw["fake"])
    assert autopay.describe_for_statement(_account(aw["lot"])) is None


def test_a_new_payment_method_replaces_and_detaches_the_old(
    client, aw, django_capture_on_commit_callbacks
):
    _enrol(client, aw, "card")
    old = _events()[0].payment_method_id
    with django_capture_on_commit_callbacks(execute=True):
        _enrol(client, aw, "us_bank_account")
    replaced = _events(AutopayEventKind.REPLACED)[0]
    assert replaced.payment_method_id != old
    assert aw["fake"].payment_methods[old]["customer"] is None
    assert autopay.state(replaced.account_id).label.endswith("account ending 4242")
    with system_access("t"):
        assert StripeCustomer.objects.count() == 1  # one customer per account


def test_a_sale_cancels_autopay_and_the_buyer_does_not_inherit_it(
    client, aw, django_capture_on_commit_callbacks
):
    setup = _enrol(client, aw, "card")
    pm = _events()[0].payment_method_id
    buyer = community.record_person(display_name="Buyer", actor=SYSTEM)
    with django_capture_on_commit_callbacks(execute=True):
        community.transfer_lot(
            aw["lot"], new_owners=[buyer], transfer_date=days_ago(0), actor=SYSTEM
        )
    (cancelled,) = _events(AutopayEventKind.CANCELLED)
    assert cancelled.code == "ownership_ended" and cancelled.account_id == setup.account_id
    assert aw["fake"].payment_methods[pm]["customer"] is None
    new_account = _account(aw["lot"])
    assert new_account.pk != setup.account_id
    assert autopay.state(new_account.pk).status == autopay.OFF
    _post_dues(aw)
    autopay.run()
    assert not _intent_posts(aw["fake"])


# --- What people see ---------------------------------------------------------------------


def test_statement_sentence(client, aw):
    assert autopay.describe_for_statement(_account(aw["lot"])) is None
    _enrol(client, aw, "card")
    account = _account(aw["lot"])
    when = f"{aw['due']:%B} {aw['due'].day}, {aw['due'].year}"
    assert autopay.describe_for_statement(account) == (
        f"This will be paid automatically on {when} from your Visa ending 4242 "
        "(card surcharge $6.00 included)."
    )
    assert autopay.describe_for_statement(account.pk) == autopay.describe_for_statement(account)
    _enrol(client, aw, "us_bank_account", last4="6789")
    assert autopay.describe_for_statement(account) == (
        f"This will be paid automatically on {when} from your STRIPE TEST BANK account ending 6789."
    )


def test_board_sees_statuses_without_card_details(client, aw):
    _enrol(client, aw, "card")
    _post_dues(aw)
    aw["fake"].outcomes[_events()[0].payment_method_id] = ["decline:expired_card"]
    autopay.run()
    client.force_login(aw["board"])
    page = client.get(reverse("online_payments")).content.decode()
    assert "Automatic payments" in page and "Paused" in page and "expired_card" in page
    assert "4242" not in page
    client.force_login(make_member(roles=[Role.MANAGER]))
    page = client.get(reverse("online_payments")).content.decode()
    assert 'data-testid="autopay-board"' not in page


def test_audit_metadata_holds_no_card_details_or_keys(
    client, aw, django_capture_on_commit_callbacks
):
    _enrol(client, aw, "card")
    _post_dues(aw)
    aw["fake"].outcomes[_events()[0].payment_method_id] = ["decline:insufficient_funds"]
    with django_capture_on_commit_callbacks(execute=True):
        autopay.run()
        client.post(reverse("autopay_cancel", args=[aw["lot"].pk]))
    rows = AuditEvent.objects.filter(action__startswith="payments.autopay")
    assert {r.action for r in rows} >= {
        "payments.autopay_setup_started",
        "payments.autopay_enrolled",
        "payments.autopay_charge_started",
        "payments.autopay_failed",
        "payments.autopay_cancelled",
    }
    dumped = json.dumps([r.metadata for r in rows])
    assert "4242" not in dumped and "visa" not in dumped.lower()
    assert KEY not in dumped and not re.search(r"(pm|cus|seti|mandate)_test", dumped)


def test_a_bank_debit_stuck_for_weeks_goes_to_the_board(client, aw):
    _enrol(client, aw, "us_bank_account")
    _post_dues(aw)
    aw["fake"].outcomes[_events()[0].payment_method_id] = ["processing"]
    autopay.run()
    autopay.poll_attempts(timezone.now() + datetime.timedelta(days=15))
    with system_access("t"):
        (alert,) = PaymentAlert.objects.filter(kind=AlertKind.MISMATCH)
    assert alert.key.startswith("stuck-") and "Automatic payment" in alert.detail
    assert not _payments()
