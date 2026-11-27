"""Payment processing failure alerts for the sysadmins, and the board's My
item for a failed automatic payment (D74). The Stripe fake stands in for
api.stripe.com: nothing reaches the network."""

import pytest
from django.core import mail

from community.models import Role
from community.tests.factories import make_member
from core import health
from core.my_items import items_for
from core.ops_timeline import bump, tally
from payments import autopay, failure_alerts, stripe_api
from payments.models import AutopayEventKind
from payments.tests.test_autopay import _enrol, _events, _post_dues

pytestmark = pytest.mark.django_db


def _figures(**kw):
    base = {
        "one_off_declined": 0,
        "autopay_declined": 0,
        "autopay_technical": 0,
        "mismatched": 0,
        "stripe_errors": 0,
        "stripe_code": "",
    }
    return {**base, **kw}


def test_one_decline_is_normal_a_burst_is_not():
    assert failure_alerts.problems(_figures(autopay_declined=1)) == []
    (burst,) = failure_alerts.problems(_figures(one_off_declined=2, autopay_declined=1))
    assert burst.startswith("3 payments were declined in the last day, more than usual")


def test_system_problems_are_named_plainly_with_counts_only():
    found = failure_alerts.problems(
        _figures(stripe_errors=7, stripe_code="http_401", mismatched=1, autopay_technical=2)
    )
    assert found[0].startswith("Stripe did not answer or refused the portal's key 7 times")
    assert "http_401" in found[0] and "check_stripe" in found[0]
    assert found[1].startswith("1 online payment in the last day did not match")
    assert found[2].startswith("2 automatic payments in the last day could not be made")


def test_hidden_until_stripe_is_set_up(settings):
    settings.STRIPE_SECRET_KEY = ""  # nosec B105 (no key: not set up)
    assert failure_alerts.health() is None


def test_stripe_trouble_is_counted_by_the_client(aw):
    aw["fake"].fail_next = (503, {"error": {"type": "api_error"}})
    with pytest.raises(stripe_api.StripeError):
        stripe_api.retrieve_payment_intent("pi_test_0000000001")
    assert tally("stripe", hours=1)[0] == 1
    # A decline is the payer's, not Stripe's trouble.
    aw["fake"].fail_next = (402, {"error": {"code": "card_declined"}})
    with pytest.raises(stripe_api.StripeError):
        stripe_api.retrieve_payment_intent("pi_test_0000000001")
    assert tally("stripe", hours=1)[0] == 1


def test_stripe_not_answering_fails_the_check_and_reaches_the_alert_email(aw):
    admin = make_member(roles=[Role.SYSADMIN])
    for _ in range(failure_alerts.STRIPE_ERRORS_ALERT):
        bump("stripe", "network")
    ok, detail = health._payment_failures()
    assert not ok and "Stripe did not answer" in detail
    results = [r for r in health.run_checks() if r.key == "payment_failures"]
    health.alert(results)
    (email,) = [m for m in mail.outbox if admin.email in m.to]
    assert "PROBLEM  Payment processing" in email.body
    assert "Counts, last day:" in email.body
    assert aw["lot"].label not in email.body and aw["owner"].email not in email.body


def test_a_failed_autopay_is_counted_and_goes_to_the_boards_my_items(
    client, aw, django_capture_on_commit_callbacks
):
    _enrol(client, aw, "card")
    _post_dues(aw)
    aw["fake"].outcomes[_events()[0].payment_method_id] = ["authentication_required"]
    with django_capture_on_commit_callbacks(execute=True):
        autopay.run()
    (failure,) = _events(AutopayEventKind.FAILED)
    f = failure_alerts.figures()
    assert f["autopay_declined"] == 1 and f["autopay_technical"] == 0
    ok, detail = failure_alerts.health()
    assert ok and detail.startswith("1 payment declined by the payer's bank")
    title = f"Automatic payment didn't go through for {aw['lot'].label}"
    (item,) = [i for i in items_for(aw["board"]) if i.title == title]
    assert "paused" in item.detail and item.side == "board"
    # Not on the payer's own board list (they have their own home item).
    assert title not in [i.title for i in items_for(aw["owner"])]
    # Once a payment method is set up again and the charge is paid, it goes.
    assert autopay.state(failure.account_id).status == autopay.PAUSED


def test_no_board_item_once_the_failure_is_resolved(
    client, aw, django_capture_on_commit_callbacks, monkeypatch
):
    _enrol(client, aw, "card")
    _post_dues(aw)
    aw["fake"].outcomes[_events()[0].payment_method_id] = ["decline:insufficient_funds"]
    with django_capture_on_commit_callbacks(execute=True):
        autopay.run()
    title = f"Automatic payment didn't go through for {aw['lot'].label}"
    assert title in [i.title for i in items_for(aw["board"])]  # still on, the charge open
    monkeypatch.setattr(autopay, "_failure_open", lambda *a: False)
    assert title not in [i.title for i in items_for(aw["board"])]
    # And older than the window: gone too (a window ending tomorrow holds nothing).
    monkeypatch.undo()
    monkeypatch.setattr(autopay, "BOARD_FAILURE_DAYS", -1)
    assert title not in [i.title for i in items_for(aw["board"])]
