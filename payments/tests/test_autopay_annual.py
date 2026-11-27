"""Automatic payments once a year and the board's review above the limit
(decision D47). A fake stands in for api.stripe.com."""

import datetime

import pytest
from django.core import mail
from django.urls import reverse
from django.utils import timezone

from audit.models import AuditEvent
from community import services as community
from community.models import Role
from community.tests.factories import days_ago, make_member
from core.models import FeatureSetting
from core.my_items import items_for
from core.policy import SYSTEM
from core.scoping import system_access
from ledger import allocation, statement_email
from ledger import services as ledger
from ledger.models import LedgerEntry
from payments import autopay, autopay_review
from payments.models import (
    AutopayAttempt,
    AutopayEventKind,
    AutopayReview,
    AutopayReviewDecision,
    CheckoutState,
)
from payments.tests.test_autopay import (
    MONTHLY,
    _account,
    _events,
    _fresh,
    _intent_posts,
    _payments,
    _setup,
)

pytestmark = pytest.mark.django_db

YEAR = 12 * MONTHLY  # 2,400.00


def _start(client, aw, method="card", *, schedule="annual", month=None, user=None, lot=None):
    user = user or aw["owner"]
    lot = lot or aw["lot"]
    _fresh(client, user)
    data = {"method": method, "schedule": schedule}
    if schedule == "annual":
        data["annual_month"] = str(month or aw["due"].month)
    response = client.post(reverse("autopay_start", args=[lot.pk]), data)
    assert response.status_code == 302, response.content[:300]
    assert response["Location"].startswith("https://checkout.stripe.com/")
    return _setup()


def _enrol(client, aw, method="card", **kwargs):
    setup = _start(client, aw, method, **kwargs)
    aw["fake"].complete_setup(setup.session_id)
    client.get(reverse("autopay_return", args=[setup.pk]), {"session_id": setup.session_id})
    return _setup(setup.pk)


def _limit(aw, cents):
    community.set_rule(
        key="payments.autopay_review_cents",
        value=str(cents),
        citation="Board resolution adopting automatic payments",
        actor=aw["board"],
    )


def _clear_other_charges(aw, extra=0):
    """Pay the two open other charges by check (plus ``extra`` credit), so
    the annual payment's credit is all for assessments."""
    ledger.record_payment(
        lot=aw["lot"],
        amount_cents=15_000 + extra,
        received_on=timezone.localdate(),
        reference="2001",
        actor=aw["board"],
    )


def _post(aw, year, month):
    ledger.post_monthly_assessments(year=year, month=month, actor=aw["board"])
    with system_access("t"):
        return LedgerEntry.objects.get(
            lot=aw["lot"], kind="assessment", period=f"{year:04d}-{month:02d}"
        )


def _shift(day: datetime.date, months: int) -> datetime.date:
    index = day.year * 12 + day.month - 1 + months
    return datetime.date(index // 12, index % 12 + 1, day.day)


def _attempts():
    with system_access("t"):
        return list(AutopayAttempt.objects.all())


def _reviews():
    with system_access("t"):
        return list(AutopayReview.objects.all())


def _when(day):
    return f"{day:%B} {day.day}, {day.year}"


# --- Setting it up ---------------------------------------------------------------------------


def test_annual_set_up_names_the_schedule_and_amount(client, aw):
    _fresh(client, aw["owner"])
    page = client.get(reverse("autopay", args=[aw["lot"].pk])).content.decode()
    assert "Once a year: about $2,400.00 once a year in the month you choose" in page
    assert "the credit simply runs out earlier or later" in page
    assert "Payments above $500.00 wait for the board" in page
    setup = _enrol(client, aw, "us_bank_account")
    month = f"{aw['due']:%B}"
    body = [c for c in aw["fake"].posts() if c["path"] == "/v1/checkout/sessions"][-1]["body"]
    message = body["custom_text"]["submit"]["message"]
    assert f"once a year in {month}" in message and "About $2,400.00" in message
    assert "for the next 12 months' assessments" in message
    assert "surcharge" not in message  # a bank account carries no fee
    assert f"once a year in {month}" in body["setup_intent_data"]["description"]
    assert setup.schedule == "annual" and setup.annual_month == aw["due"].month
    (event,) = _events()
    assert (event.schedule, event.annual_month) == ("annual", aw["due"].month)
    st = autopay.state(setup.account_id)
    assert st.schedule == autopay.ANNUAL and st.schedule_text == f"once a year in {month}"
    audit = AuditEvent.objects.get(action="payments.autopay_enrolled")
    assert audit.metadata["schedule"] == "annual"
    # A card's authorization names the surcharge on the whole amount.
    _start(client, aw, "card")
    body = aw["fake"].posts()[-1]["body"]
    assert "surcharge to the whole amount" in body["custom_text"]["submit"]["message"]


def test_a_month_is_required_for_the_annual_schedule(client, aw):
    _fresh(client, aw["owner"])
    for month in ("", "13", "x"):
        client.post(
            reverse("autopay_start", args=[aw["lot"].pk]),
            {"method": "card", "schedule": "annual", "annual_month": month},
        )
    client.post(
        reverse("autopay_start", args=[aw["lot"].pk]), {"method": "card", "schedule": "weekly"}
    )
    with system_access("t"):
        from payments.models import AutopaySetup

        assert not AutopaySetup.objects.exists()


# --- Charging once a year --------------------------------------------------------------------


def test_annual_charge_is_twelve_months_less_credit_with_the_card_surcharge(
    client, aw, django_capture_on_commit_callbacks
):
    _limit(aw, 1_000_000)
    _clear_other_charges(aw, extra=5_000)  # 50.00 credit already on the account
    setup = _enrol(client, aw, "card")
    dues = _post(aw, aw["due"].year, aw["due"].month)
    assert allocation.charge_status(dues) == "partial"  # the credit drew down
    with django_capture_on_commit_callbacks(execute=True):
        autopay.run()
        autopay.run()
        autopay.charge_due(aw["due"] + datetime.timedelta(days=2))
    (call,) = _intent_posts(aw["fake"])
    base = YEAR - 5_000
    surcharge = -(-base * 300 // 10_000)
    assert call["body"]["amount"] == str(base + surcharge)
    assert call["headers"]["Idempotency-Key"] == (
        f"autopay-{setup.account_id}-annual-{aw['due'].year}-1"
    )
    assert "twelve months from" in call["body"]["description"]
    (payment,) = _payments()
    assert payment.base_cents == base and payment.surcharge_cents == surcharge
    (attempt,) = _attempts()
    assert attempt.schedule == "annual" and attempt.period == f"{aw['due'].year}"
    assert attempt.covers_from == aw["period"] and attempt.charge_id == dues.pk
    assert allocation.charge_status(dues) == "paid"
    account = _account(aw["lot"])
    assert allocation.unapplied_credit(aw["lot"].pk, account=account.pk) == 11 * MONTHLY
    audit = AuditEvent.objects.get(action="payments.autopay_charge_started")
    assert audit.metadata["schedule"] == "annual"


def test_bank_and_debit_pay_no_surcharge_on_the_year(client, aw):
    _limit(aw, 1_000_000)
    _enrol(client, aw, "us_bank_account", user=aw["owner_b"], lot=aw["other_lot"])
    autopay.run()  # the month's assessment not posted yet: twelve months at the rule
    (call,) = _intent_posts(aw["fake"])
    assert call["body"]["amount"] == str(YEAR)
    (attempt,) = _attempts()
    assert attempt.charge_id is None and attempt.checkout.surcharge_cents == 0


def test_the_year_is_drawn_down_month_by_month(client, aw, monkeypatch):
    _limit(aw, 1_000_000)
    _clear_other_charges(aw)
    _enrol(client, aw, "us_bank_account")
    first = _post(aw, aw["due"].year, aw["due"].month)
    autopay.run()
    (payment,) = _payments()
    assert payment.base_cents == YEAR and allocation.charge_status(first) == "paid"
    account = _account(aw["lot"])
    last = _shift(aw["due"], 11)
    assert autopay.describe_for_statement(account) == (
        f"Your assessments are prepaid through {last:%B %Y} by your annual automatic payment."
    )
    for i in range(1, 12):
        day = _shift(aw["due"], i)
        charge = _post(aw, day.year, day.month)
        assert allocation.charge_status(charge) == "paid", day
        (view,) = [
            v for v in allocation.charges(aw["lot"].pk, open_only=False) if v.entry == charge
        ]
        assert [p.entry_id for p in view.payments] == [payment.entry_id]
        line = statement_email.status_line(account, year=day.year, month=day.month)
        assert "covered by your prepaid credit" in line, line
        # Monthly charging never happens for an annual payer.
        monkeypatch.setattr(autopay, "_today", lambda day=day: day)
        autopay.charge_due(day)
    assert len(_intent_posts(aw["fake"])) == 1
    assert allocation.unapplied_credit(aw["lot"].pk, account=account.pk) == 0
    # The thirteenth month is open: the next yearly payment covers it.
    day = _shift(aw["due"], 12)
    charge = _post(aw, day.year, day.month)
    assert allocation.charge_status(charge) == "open"
    monkeypatch.setattr(autopay, "_today", lambda: day)
    autopay.run()
    calls = _intent_posts(aw["fake"])
    assert len(calls) == 2 and calls[1]["body"]["amount"] == str(YEAR)
    assert calls[1]["headers"]["Idempotency-Key"].endswith(f"-annual-{day.year}-1")


def test_bridge_months_before_the_first_yearly_payment_are_paid_monthly(client, aw, monkeypatch):
    _limit(aw, 1_000_000)
    first_year = _shift(aw["due"], 2)
    _enrol(client, aw, "us_bank_account", month=first_year.month)
    account = _account(aw["lot"])
    _fresh(client, aw["owner"])
    page = client.get(reverse("autopay", args=[aw["lot"].pk])).content.decode()
    assert (
        f"Until your first yearly payment in {first_year:%B %Y}, each month is paid "
        "automatically as usual."
    ) in page
    text = autopay.describe_for_statement(account)
    assert text.startswith(f"This will be paid automatically on {_when(aw['due'])}")
    dues = _post(aw, aw["due"].year, aw["due"].month)
    autopay.run()
    autopay.run()
    (call,) = _intent_posts(aw["fake"])
    assert call["body"]["amount"] == str(MONTHLY)
    assert call["headers"]["Idempotency-Key"] == f"autopay-{account.pk}-{aw['period']}-1"
    assert allocation.charge_status(dues) == "paid"
    second = _shift(aw["due"], 1)
    monkeypatch.setattr(autopay, "_today", lambda: second)
    bridge = _post(aw, second.year, second.month)
    autopay.charge_due(second)
    assert len(_intent_posts(aw["fake"])) == 2 and allocation.charge_status(bridge) == "paid"
    monkeypatch.setattr(autopay, "_today", lambda: first_year)
    yearly = _post(aw, first_year.year, first_year.month)
    autopay.charge_due(first_year)
    autopay.charge_due(first_year + datetime.timedelta(days=1))
    calls = _intent_posts(aw["fake"])
    assert len(calls) == 3 and calls[2]["body"]["amount"] == str(YEAR)
    assert "-annual-" in calls[2]["headers"]["Idempotency-Key"]
    assert allocation.charge_status(yearly) == "paid"
    (annual,) = [a for a in _attempts() if a.schedule == "annual"]
    assert (annual.covers_from, annual.covers_through) == (
        f"{first_year:%Y-%m}",
        f"{_shift(first_year, 11):%Y-%m}",
    )


def test_a_bridge_month_the_credit_covers_is_not_charged(client, aw):
    _limit(aw, 1_000_000)
    _clear_other_charges(aw, extra=MONTHLY)
    _enrol(client, aw, "us_bank_account", month=_shift(aw["due"], 3).month)
    dues = _post(aw, aw["due"].year, aw["due"].month)
    autopay.run()
    assert not _intent_posts(aw["fake"]) and allocation.charge_status(dues) == "paid"


def test_an_annual_charge_never_includes_a_month_paid_monthly(client, aw):
    _enrol(client, aw, "us_bank_account", schedule="monthly")
    _post(aw, aw["due"].year, aw["due"].month)
    aw["fake"].outcomes[_events()[0].payment_method_id] = ["processing"]
    autopay.run()
    (attempt,) = _attempts()
    assert attempt.checkout.state == CheckoutState.PROCESSING
    account = _account(aw["lot"])
    _views, base = autopay.annual_amount(aw["lot"].pk, account.pk, aw["period"])
    assert base == YEAR - MONTHLY  # the month in flight is not charged again


def _on(monkeypatch, day):
    monkeypatch.setattr(autopay, "_today", lambda: day)
    return day


def _switch(client, aw, month):
    _fresh(client, aw["owner"])
    response = client.post(
        reverse("autopay_schedule", args=[aw["lot"].pk]),
        {"schedule": "annual", "annual_month": str(month), "authorize": "1"},
    )
    assert response.status_code == 302


def test_a_later_month_bridges_the_gap_after_the_prepaid_months(client, aw, monkeypatch):
    _limit(aw, 1_000_000)
    _clear_other_charges(aw)
    _enrol(client, aw, "us_bank_account", month=1)
    year = 2031
    day = _on(monkeypatch, datetime.date(year, 1, 5))
    _post(aw, year, 1)
    autopay.charge_due(day)
    (first,) = _intent_posts(aw["fake"])
    assert first["body"]["amount"] == str(YEAR)
    for month in range(2, 13):
        _post(aw, year, month)
    _switch(client, aw, 3)  # from January to March
    account = _account(aw["lot"])
    day = _on(monkeypatch, datetime.date(year, 3, 5))
    autopay.charge_due(day)  # this year's yearly payment is made: nothing more
    assert len(_intent_posts(aw["fake"])) == 1
    assert autopay.bridge_text(account, autopay.state(account.pk)) == [
        f"Your prepaid months end in December {year}. Until your next yearly payment in "
        f"March {year + 1}, each month is paid automatically as usual."
    ]
    charges = {}
    for month in (1, 2):
        day = _on(monkeypatch, datetime.date(year + 1, month, 5))
        charges[month] = _post(aw, year + 1, month)
        autopay.charge_due(day)
        autopay.charge_due(day + datetime.timedelta(days=1))
    calls = _intent_posts(aw["fake"])
    assert [c["body"]["amount"] for c in calls] == [str(YEAR), str(MONTHLY), str(MONTHLY)]
    assert calls[1]["headers"]["Idempotency-Key"].endswith(f"-{year + 1}-01-1")
    assert all(allocation.charge_status(c) == "paid" for c in charges.values())
    day = _on(monkeypatch, datetime.date(year + 1, 3, 5))
    march = _post(aw, year + 1, 3)
    autopay.charge_due(day)
    calls = _intent_posts(aw["fake"])
    assert len(calls) == 4 and calls[3]["body"]["amount"] == str(YEAR)
    assert calls[3]["headers"]["Idempotency-Key"].endswith(f"-annual-{year + 1}-1")
    assert allocation.charge_status(march) == "paid"
    latest = next(a for a in _attempts() if a.schedule == "annual" and a.period == f"{year + 1}")
    assert (latest.covers_from, latest.covers_through) == (f"{year + 1}-03", f"{year + 2}-02")


def test_an_earlier_month_buys_twelve_months_from_the_end_of_the_prepaid_months(
    client, aw, monkeypatch
):
    _limit(aw, 1_000_000)
    _clear_other_charges(aw)
    _enrol(client, aw, "us_bank_account", month=12)
    year = 2031
    day = _on(monkeypatch, datetime.date(year, 12, 5))
    _post(aw, year, 12)
    autopay.charge_due(day)
    assert len(_intent_posts(aw["fake"])) == 1
    _switch(client, aw, 1)  # from December to January
    account = _account(aw["lot"])
    _on(monkeypatch, datetime.date(year, 12, 20))
    assert autopay.bridge_text(account, autopay.state(account.pk)) == [
        f"Your next yearly payment, in January {year + 1}, pays for the 12 months from "
        f"December {year + 1}, the month after your prepaid months end."
    ]
    _fresh(client, aw["owner"])
    page = client.get(reverse("autopay", args=[aw["lot"].pk])).content.decode()
    assert f"pays for the 12 months from December {year + 1}" in page
    day = _on(monkeypatch, datetime.date(year + 1, 1, 5))
    january = _post(aw, year + 1, 1)
    autopay.charge_due(day)
    calls = _intent_posts(aw["fake"])
    assert len(calls) == 2 and calls[1]["body"]["amount"] == str(YEAR)
    assert allocation.charge_status(january) == "paid"  # from the first year's credit
    latest = next(a for a in _attempts() if a.period == f"{year + 1}")
    assert (latest.covers_from, latest.covers_through) == (f"{year + 1}-01", f"{year + 2}-11")
    assert allocation.unapplied_credit(aw["lot"].pk, account=account.pk) == 22 * MONTHLY


def test_back_to_monthly_uses_the_credit_and_never_charges_a_month_twice(client, aw, monkeypatch):
    _limit(aw, 1_000_000)
    _clear_other_charges(aw)
    _enrol(client, aw, "us_bank_account", month=12)
    aw["fake"].outcomes[_events()[0].payment_method_id] = ["processing"]
    year = 2031
    day = _on(monkeypatch, datetime.date(year, 12, 5))
    autopay.charge_due(day)
    (annual,) = _attempts()
    assert annual.checkout.state == CheckoutState.PROCESSING
    _fresh(client, aw["owner"])
    client.post(
        reverse("autopay_schedule", args=[aw["lot"].pk]),
        {"schedule": "monthly", "authorize": "1"},
    )
    account = _account(aw["lot"])
    assert autopay.state(account.pk).schedule == autopay.MONTHLY
    # The yearly payment is still settling: its months are not charged monthly.
    day = _on(monkeypatch, datetime.date(year + 1, 1, 5))
    january = _post(aw, year + 1, 1)
    autopay.charge_due(day)
    assert len(_intent_posts(aw["fake"])) == 1
    aw["fake"].settle_ach(annual.checkout.payment_intent_id)
    autopay.poll_attempts(timezone.now() + datetime.timedelta(hours=7))
    assert allocation.charge_status(january) == "paid"  # the credit, first
    day = _on(monkeypatch, datetime.date(year + 1, 2, 5))
    february = _post(aw, year + 1, 2)
    autopay.charge_due(day)
    assert len(_intent_posts(aw["fake"])) == 1 and allocation.charge_status(february) == "paid"
    # After the year (and its credit) runs out, monthly charges carry on.
    for month in range(3, 13):
        _post(aw, year + 1, month)
    assert allocation.unapplied_credit(aw["lot"].pk, account=account.pk) == 0
    day = _on(monkeypatch, datetime.date(year + 2, 1, 5))
    last = _post(aw, year + 2, 1)
    autopay.charge_due(day)
    calls = _intent_posts(aw["fake"])
    assert len(calls) == 2 and calls[1]["body"]["amount"] == str(MONTHLY)
    assert allocation.charge_status(last) == "paid"


def test_switching_schedule_is_audited_and_takes_effect_from_the_next_charge(client, aw):
    _limit(aw, 1_000_000)
    from payments.tests.test_autopay import _enrol as enrol_monthly

    enrol_monthly(client, aw, "us_bank_account")
    url = reverse("autopay_schedule", args=[aw["lot"].pk])
    _fresh(client, aw["owner"])
    month = aw["due"].month
    # The authorization box is required; the same schedule is refused.
    client.post(url, {"schedule": "annual", "annual_month": str(month)})
    client.post(url, {"schedule": "monthly", "authorize": "1"})
    assert not _events(AutopayEventKind.SCHEDULE)
    response = client.post(
        url, {"schedule": "annual", "annual_month": str(month), "authorize": "1"}
    )
    assert response.status_code == 302
    (event,) = _events(AutopayEventKind.SCHEDULE)
    assert event.user_id == aw["owner"].pk and event.annual_month == month
    audit = AuditEvent.objects.get(action="payments.autopay_schedule_changed")
    assert audit.metadata == {
        "lot_id": aw["lot"].pk,
        "account_id": event.account_id,
        "schedule": "annual",
        "month": month,
    }
    assert autopay.state(event.account_id).schedule == autopay.ANNUAL
    page = client.get(reverse("autopay", args=[aw["lot"].pk])).content.decode()
    assert f"Paying once a year in {aw['due']:%B}" in page
    _post(aw, aw["due"].year, aw["due"].month)
    autopay.run()
    (call,) = _intent_posts(aw["fake"])
    assert "-annual-" in call["headers"]["Idempotency-Key"]
    # Back to monthly: the credit pays the coming months, nothing more is charged.
    client.post(url, {"schedule": "monthly", "authorize": "1"})
    assert autopay.state(event.account_id).schedule == autopay.MONTHLY
    autopay.run()
    assert len(_intent_posts(aw["fake"])) == 1


def test_only_payers_may_change_the_schedule(client, aw):
    from payments.tests.test_autopay import _enrol as enrol_monthly

    setup_b = enrol_monthly(client, aw, "card", user=aw["owner_b"], lot=aw["other_lot"])
    data = {"schedule": "annual", "annual_month": "1", "authorize": "1"}
    for user in (aw["owner"], aw["board"], make_member(roles=[Role.MANAGER])):
        _fresh(client, user)
        response = client.post(reverse("autopay_schedule", args=[aw["other_lot"].pk]), data)
        assert response.status_code in (403, 404), user
    assert autopay.state(setup_b.account_id).schedule == autopay.MONTHLY


# --- Board review above the limit ------------------------------------------------------------


def test_a_monthly_charge_above_the_limit_waits_then_is_charged_once(
    client, aw, django_capture_on_commit_callbacks
):
    community.set_rule(
        key="dues.monthly_cents", value="60000", citation="Decl. 8.1", actor=aw["board"]
    )
    from payments.tests.test_autopay import _enrol as enrol_monthly

    enrol_monthly(client, aw, "us_bank_account")
    _post(aw, aw["due"].year, aw["due"].month)
    autopay.run()
    autopay.run()
    assert not _intent_posts(aw["fake"])
    (review,) = _reviews()
    assert (review.schedule, review.total_cents, review.limit_cents) == ("monthly", 60_000, 50_000)
    assert AuditEvent.objects.filter(action="payments.autopay_review_requested").count() == 1
    (item,) = [i for i in items_for(aw["board"]) if i.title.startswith("Approve automatic")]
    assert item.title == "Approve automatic payment of $600.00 for 1 Alpha Way"
    _fresh(client, aw["owner"])
    page = client.get(reverse("autopay", args=[aw["lot"].pk])).content.decode()
    assert "Your automatic payment of $600.00 for the" in page
    assert "is waiting for the board" in page
    client.force_login(aw["board"])
    page = client.get(reverse("online_payments")).content.decode()
    assert "Approve automatic payment of $600.00 for 1 Alpha Way" in page
    response = client.post(reverse("autopay_review", args=[review.pk]), {"decision": "approve"})
    assert response.status_code == 302
    assert autopay_review.status(review) == autopay_review.APPROVED
    assert not [i for i in items_for(aw["board"]) if i.title.startswith("Approve automatic")]
    # A second approval is refused.
    client.post(reverse("autopay_review", args=[review.pk]), {"decision": "approve"})
    with system_access("t"):
        assert AutopayReviewDecision.objects.count() == 1
    later = timezone.now() + datetime.timedelta(days=9)  # past the due-day window
    with django_capture_on_commit_callbacks(execute=True):
        autopay.run(later)
        autopay.run(later + datetime.timedelta(hours=1))
    (call,) = _intent_posts(aw["fake"])
    assert call["body"]["amount"] == "60000"
    (payment,) = _payments()
    assert payment.base_cents == 60_000
    assert AuditEvent.objects.get(action="payments.autopay_review_approved").metadata == {
        "lot_id": aw["lot"].pk,
        "account_id": review.account_id,
        "review_id": review.pk,
        "amount_cents": 60_000,
    }


def test_an_annual_charge_waits_and_a_decline_emails_the_payer(
    client, aw, django_capture_on_commit_callbacks
):
    setup = _enrol(client, aw, "card")
    autopay.run()
    assert not _intent_posts(aw["fake"])
    (review,) = _reviews()
    total = YEAR + YEAR * 3 // 100  # surcharge on the whole year
    assert (review.schedule, review.total_cents) == ("annual", total)
    account = _account(aw["lot"])
    assert autopay.describe_for_statement(account) == (
        "Your annual automatic payment of $2,472.00 is waiting for the board's review."
    )
    _fresh(client, aw["owner"])
    page = client.get(reverse("autopay", args=[aw["lot"].pk])).content.decode()
    assert "Your annual payment of $2,472.00 is waiting for the board" in page
    client.force_login(aw["board"])
    url = reverse("autopay_review", args=[review.pk])
    client.post(url, {"decision": "decline", "reason": "  "})
    assert autopay_review.status(review) == autopay_review.WAITING
    mail.outbox.clear()
    with django_capture_on_commit_callbacks(execute=True):
        client.post(url, {"decision": "decline", "reason": "Please talk to the treasurer first."})
    assert autopay_review.status(review) == autopay_review.DECLINED
    (email,) = [m for m in mail.outbox if "not approved" in m.subject]
    assert aw["owner"].email in email.to
    assert "$2,472.00" in email.body and "1 Alpha Way" in email.body
    assert "Please talk to the treasurer first." in email.body
    assert "monthly payments do not start" in email.body
    assert "https://portal.example.com" + reverse("my_account") in email.body
    autopay.run()
    autopay.run(timezone.now() + datetime.timedelta(days=2))
    assert not _intent_posts(aw["fake"]) and not _attempts()
    assert autopay.state(setup.account_id).status == autopay.ON
    audit = AuditEvent.objects.get(action="payments.autopay_review_declined")
    assert "reason" not in audit.metadata and audit.metadata["review_id"] == review.pk


def test_a_review_expires_after_14_days(client, aw, django_capture_on_commit_callbacks):
    _enrol(client, aw, "us_bank_account")
    autopay.run()
    (review,) = _reviews()
    mail.outbox.clear()
    with django_capture_on_commit_callbacks(execute=True):
        autopay.run(timezone.now() + datetime.timedelta(days=13))
        assert autopay_review.status(review) == autopay_review.WAITING
        autopay.run(timezone.now() + datetime.timedelta(days=14, minutes=1))
    assert autopay_review.status(review) == autopay_review.EXPIRED
    (email,) = [m for m in mail.outbox if "was not charged" in m.subject]
    assert "not decided within 14 days" in email.body and aw["owner"].email in email.to
    client.force_login(aw["board"])
    client.post(reverse("autopay_review", args=[review.pk]), {"decision": "approve"})
    assert autopay_review.status(review) == autopay_review.EXPIRED
    autopay.run()
    assert not _intent_posts(aw["fake"])
    assert AuditEvent.objects.get(action="payments.autopay_review_expired").metadata["code"] == (
        "not_decided"
    )


def test_no_self_approval_and_the_manager_cannot_approve(client, aw):
    co_owner_on_board = make_member(board=True, lots=[aw["lot"]])
    _enrol(client, aw, "us_bank_account")
    autopay.run()
    (review,) = _reviews()
    url = reverse("autopay_review", args=[review.pk])
    assert not [i for i in items_for(co_owner_on_board) if "Approve automatic" in i.title]
    client.force_login(co_owner_on_board)
    page = client.get(reverse("online_payments")).content.decode()
    # D64: said before any button, in the board-note style; the service refuses too.
    assert "You pay for this home. This is about your own home, so another board member" in page
    assert 'data-testid="own-home-note"' in page
    assert client.post(url, {"decision": "approve"}).status_code == 403
    for user in (make_member(roles=[Role.MANAGER]), aw["owner"], aw["owner_b"]):
        client.force_login(user)
        assert client.post(url, {"decision": "approve"}).status_code in (403, 404), user
    assert autopay_review.status(review) == autopay_review.WAITING
    client.force_login(aw["board"])
    client.post(url, {"decision": "approve"})
    assert autopay_review.status(review) == autopay_review.APPROVED


def test_the_review_url_is_off_with_the_feature(client, aw):
    _enrol(client, aw, "us_bank_account")
    autopay.run()
    (review,) = _reviews()
    FeatureSetting.objects.filter(key="autopay").update(enabled=False)
    client.force_login(aw["board"])
    response = client.post(reverse("autopay_review", args=[review.pk]), {"decision": "approve"})
    assert response.status_code == 404
    _fresh(client, aw["owner"])
    response = client.post(
        reverse("autopay_schedule", args=[aw["lot"].pk]),
        {"schedule": "monthly", "authorize": "1"},
    )
    assert response.status_code == 404
    assert autopay_review.status(review) == autopay_review.WAITING


def test_switching_schedule_closes_a_waiting_review(client, aw):
    _enrol(client, aw, "us_bank_account")
    autopay.run()
    (review,) = _reviews()
    _fresh(client, aw["owner"])
    client.post(
        reverse("autopay_schedule", args=[aw["lot"].pk]),
        {"schedule": "monthly", "authorize": "1"},
    )
    assert autopay_review.status(review) == autopay_review.CLOSED
    assert not [i for i in items_for(aw["board"]) if "Approve automatic" in i.title]


# --- A sale ----------------------------------------------------------------------------------


def test_a_sale_ends_annual_autopay_and_the_credit_stays_with_the_seller(
    client, aw, django_capture_on_commit_callbacks
):
    _limit(aw, 1_000_000)
    _clear_other_charges(aw)
    setup = _enrol(client, aw, "us_bank_account")
    autopay.run()  # before the month's assessment is posted: all of it is credit
    (payment,) = _payments()
    assert payment.checkout.state == CheckoutState.PAID
    seller = _account(aw["lot"])
    credit = allocation.unapplied_credit(aw["lot"].pk, account=seller.pk)
    assert credit == YEAR
    buyer = community.record_person(display_name="Buyer", actor=SYSTEM)
    with django_capture_on_commit_callbacks(execute=True):
        community.transfer_lot(
            aw["lot"], new_owners=[buyer], transfer_date=days_ago(0), actor=SYSTEM
        )
    (cancelled,) = _events(AutopayEventKind.CANCELLED)
    assert cancelled.code == "ownership_ended" and cancelled.account_id == setup.account_id
    buyer_account = _account(aw["lot"])
    assert buyer_account.pk != seller.pk
    charge = _post(aw, aw["due"].year, aw["due"].month)
    assert charge.account_id == buyer_account.pk
    assert allocation.charge_status(charge) == "open"  # the seller's credit never pays it
    assert allocation.unapplied_credit(aw["lot"].pk, account=seller.pk) == credit
    assert allocation.unapplied_credit(aw["lot"].pk, account=buyer_account.pk) == 0
    assert autopay.state(buyer_account.pk).status == autopay.OFF
