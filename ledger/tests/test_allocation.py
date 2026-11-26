"""Open-item accounting (D32-payments): each charge has its own open balance."""

import datetime
import random

import pytest
from django.core.management import call_command
from django.db import connection
from django.urls import reverse

from community import services as community
from community.models import Lot
from community.tests.factories import LotFactory, make_member
from core.append_only import AppendOnlyError
from core.policy import SYSTEM
from core.scoping import UnscopedQueryError, system_access
from ledger import allocation, services
from ledger.allocation import DASH
from ledger.models import ChargeNumber, LedgerEntry, PaymentApplication

pytestmark = pytest.mark.django_db
D = datetime.date


@pytest.fixture
def w():
    board = make_member(board=True)
    community.set_rule(
        key="dues.monthly_cents", value="4500", citation="Declaration §7.2 (demo)", actor=board
    )
    lot = LotFactory()
    return {"board": board, "lot": lot, "owner": make_member(lots=[lot])}


def _invariant(lot_id):
    opened = sum(c.open_cents for c in allocation.open_charges(lot_id))
    assert opened - allocation.unapplied_credit(lot_id) == services.balance(lot_id)


def _charge(w, cents=1000, desc="Pool key", on=None):
    return services.post_charge(
        lot=w["lot"],
        kind="other_charge",
        amount_cents=cents,
        description=desc,
        posted_on=on,
        actor=w["board"],
    )


def _pay(w, cents, on=None, **kw):
    return services.record_payment(
        lot=w["lot"],
        amount_cents=cents,
        received_on=on or D(2026, 6, 1),
        actor=w["board"],
        **kw,
    )


def test_charges_get_a_stable_number_and_dues_a_month_title(w):
    services.post_monthly_assessments(year=2026, month=3, actor=w["board"])
    first = _charge(w, on=D(2026, 4, 1))
    second = _charge(w, on=D(2026, 4, 2))
    views = allocation.open_charges(w["lot"].pk)
    assert [v.title for v in views] == [f"Dues {DASH} March 2026", "Pool key", "Pool key"]
    labels = [v.label for v in views]
    assert labels[0].startswith("Invoice 2026-") and len(set(labels)) == 3
    with system_access("t"):
        a, b = ChargeNumber.objects.get(entry=first), ChargeNumber.objects.get(entry=second)
    assert b.seq == a.seq + 1


def test_payments_pay_the_oldest_charge_first(w):
    old = _charge(w, 3000, "Old", D(2026, 1, 5))
    new = _charge(w, 2000, "New", D(2026, 2, 5))
    _pay(w, 4000)
    assert allocation.charge_status(old) == "paid"
    assert allocation.charge_status(new) == "partial"
    (view,) = allocation.open_charges(w["lot"].pk)
    assert view.entry.pk == new.pk and view.open_cents == 1000
    assert [p.amount_cents for p in view.payments] == [1000]
    _invariant(w["lot"].pk)


def test_rule_can_pay_dues_before_late_fees_and_other_charges(w):
    community.set_rule(
        key="ledger.assessments_before_fees",
        value="true",
        citation="Declaration §7.6 (demo)",
        actor=w["board"],
    )
    fee = _charge(w, 3000, "Old fine", D(2026, 1, 5))
    services.post_monthly_assessments(year=2026, month=3, actor=w["board"])
    _pay(w, 4500)
    assert allocation.charge_status(fee) == "open"
    assert [c.entry.pk for c in allocation.open_charges(w["lot"].pk)] == [fee.pk]


def test_a_payment_can_be_directed_to_chosen_charges(w):
    old = _charge(w, 3000, "Old", D(2026, 1, 5))
    new = _charge(w, 2000, "New", D(2026, 2, 5))
    _pay(w, 2500, apply_to=[new.pk])
    assert allocation.charge_status(new) == "paid"
    assert allocation.charge_status(old) == "partial"  # the rest followed the rule
    _invariant(w["lot"].pk)


def test_directing_to_another_homes_charge_is_refused(w):
    other = LotFactory()
    theirs = services.post_charge(
        lot=other, kind="other_charge", amount_cents=100, description="x", actor=w["board"]
    )
    with pytest.raises(services.LedgerError, match="open charges"):
        _pay(w, 100, apply_to=[theirs.pk])


def test_prepayment_stays_as_credit_and_draws_down_as_months_post(w):
    _pay(w, 4500 * 3, on=D(2025, 12, 20), reference="1001")
    assert allocation.unapplied_credit(w["lot"].pk) == 13500
    for month in (1, 2):
        services.post_monthly_assessments(year=2026, month=month, actor=w["board"])
    assert allocation.open_charges(w["lot"].pk) == []
    assert allocation.unapplied_credit(w["lot"].pk) == 4500
    services.post_monthly_assessments(year=2026, month=3, actor=w["board"])
    services.post_monthly_assessments(year=2026, month=4, actor=w["board"])
    (april,) = allocation.open_charges(w["lot"].pk)
    assert april.title == f"Dues {DASH} April 2026" and april.open_cents == 4500
    _invariant(w["lot"].pk)


def test_reversing_a_payment_reopens_the_charges_it_paid(w):
    charge = _charge(w, 3000)
    payment = _pay(w, 3000, reference="77")
    assert allocation.charge_status(charge) == "paid"
    services.reverse_entry(payment, reason="Check returned", actor=w["board"])
    assert allocation.charge_status(charge) == "open"
    assert allocation.applications_of(payment.pk) != []  # matched to its reversal
    _invariant(w["lot"].pk)


def test_reversing_a_charge_frees_the_money_for_other_charges(w):
    mistake = _charge(w, 3000, "Oops", D(2026, 1, 5))
    _pay(w, 3000)
    later = _charge(w, 2000, "Real", D(2026, 3, 5))
    services.reverse_entry(mistake, reason="Wrong lot", actor=w["board"])
    assert allocation.charge_status(later) == "paid"
    assert allocation.unapplied_credit(w["lot"].pk) == 1000
    views = allocation.charges(w["lot"].pk, open_only=False)
    assert {v.entry.pk: v.status for v in views}[mistake.pk] == "reversed"
    _invariant(w["lot"].pk)


def test_random_histories_keep_balances_identical(w):
    rng = random.Random(7)  # noqa: S311  # nosec B311 - test data
    entries = []
    for i in range(40):
        roll = rng.random()
        if roll < 0.4:
            entries.append(_charge(w, rng.randint(1, 9000), f"c{i}", D(2026, 1, 1 + i % 28)))
        elif roll < 0.75:
            entries.append(_pay(w, rng.randint(1, 9000), reference=f"r{i}"))
        elif roll < 0.85:
            services.post_credit(
                lot=w["lot"], amount_cents=rng.randint(1, 500), description="c", actor=w["board"]
            )
        elif entries:
            target = entries.pop(rng.randrange(len(entries)))
            services.reverse_entry(target, reason="r", actor=w["board"])
        _invariant(w["lot"].pk)
    book = allocation.load_book(w["lot"].pk)
    assert all(book.remaining(e) >= 0 for e in book.entries)


def test_aging_matches_the_open_charges(w):
    services.post_monthly_assessments(year=2026, month=1, actor=w["board"])
    services.post_monthly_assessments(year=2026, month=2, actor=w["board"])
    _pay(w, 4500, on=D(2026, 2, 3))
    a = services.aging(w["lot"].pk, as_of=D(2026, 4, 15))
    assert a["61_90"] == 4500 and sum(a[b] for b in services.BUCKETS) == services.balance(
        w["lot"].pk
    )


def test_applications_and_numbers_are_append_only(w):
    _charge(w)
    _pay(w, 500)
    with system_access("t"):
        row = PaymentApplication.objects.first()
        number = ChargeNumber.objects.first()
    with pytest.raises(AppendOnlyError):
        row.save()
    with pytest.raises(AppendOnlyError):
        row.delete()
    with pytest.raises(AppendOnlyError):
        number.delete()
    with connection.cursor() as cur, pytest.raises(Exception, match="append-only"):
        cur.execute("UPDATE ledger_paymentapplication SET amount_cents = 1")


def test_applications_are_scoped(w):
    _charge(w)
    _pay(w, 500)
    with pytest.raises(UnscopedQueryError):
        list(PaymentApplication.objects.all())
    other_owner = make_member(lots=[LotFactory()])
    assert not PaymentApplication.objects.for_user(other_owner).exists()
    assert PaymentApplication.objects.for_user(w["owner"]).exists()


def test_manual_online_payments_are_refused(w, client):
    for method in ("online", "Stripe", " ONLINE "):
        with pytest.raises(services.LedgerError, match="automatically from Stripe"):
            _pay(w, 500, method=method)
    with pytest.raises(PermissionError):
        services.record_online_payment(
            lot=w["lot"],
            amount_cents=500,
            received_on=D(2026, 6, 1),
            payment_intent_id="pi_test123456",
            apply_to=[],
            actor=w["board"],
        )
    client.force_login(w["board"])
    client.post(
        reverse("lot_ledger_action", args=[w["lot"].pk, "payment"]),
        {"amount_cents": "5.00", "received_on": "2026-06-01", "method": "online"},
    )
    assert services.balance(w["lot"].pk) == 0


def test_rebuilding_history_is_idempotent_and_keeps_balances(w):
    with allocation.suspended():
        services.post_monthly_assessments(year=2026, month=1, actor=w["board"])
        fee = _charge(w, 3000, "Fine", D(2026, 1, 20))
        payment = _pay(w, 6000, reference="9")
        services.post_monthly_assessments(year=2026, month=2, actor=w["board"])
        services.reverse_entry(fee, reason="Waived", actor=w["board"])
        services.reverse_entry(payment, reason="Returned", actor=w["board"])
        _pay(w, 2000, reference="10")
    before = services.balance(w["lot"].pk)
    with system_access("t"):
        assert not PaymentApplication.objects.exists()
    qs = lambda m: m.objects.unscoped("test")  # noqa: E731
    first = allocation.backfill(LedgerEntry, PaymentApplication, ChargeNumber, qs=qs)
    assert first["applications"] > 0 and first["numbers"] == 3
    again = allocation.backfill(LedgerEntry, PaymentApplication, ChargeNumber, qs=qs)
    assert again == {"applications": 0, "numbers": 0}
    assert services.balance(w["lot"].pk) == before
    _invariant(w["lot"].pk)


def test_seeded_community_rebuilds_to_the_same_balances(settings):
    settings.SEED_DEMO_ALLOWED = True
    with allocation.suspended():
        call_command("seed_demo", verbosity=0)
    with system_access("t"):
        lots = list(Lot.objects.values_list("pk", flat=True))
    before = {pk: services.balance(pk) for pk in lots}
    qs = lambda m: m.objects.unscoped("test")  # noqa: E731
    allocation.backfill(LedgerEntry, PaymentApplication, ChargeNumber, qs=qs)
    assert allocation.backfill(LedgerEntry, PaymentApplication, ChargeNumber, qs=qs) == {
        "applications": 0,
        "numbers": 0,
    }
    for pk in lots:
        assert services.balance(pk) == before[pk]
        _invariant(pk)
        a = services.aging(pk)
        total = sum(a[b] for b in services.BUCKETS) - a["credit"]
        assert total == before[pk]


def test_owner_sees_open_charges_with_payments_applied(client, w):
    charge = _charge(w, 3000, "Gate remote")
    _pay(w, 1000, reference="55")
    client.force_login(w["owner"])
    page = client.get(reverse("my_account")).content.decode()
    assert "Open charges" in page and "Gate remote" in page
    number = allocation.open_charges(w["lot"].pk)[0].number
    assert f"Invoice {number}" in page and "Partly paid" in page
    other = make_member(lots=[LotFactory()])
    client.force_login(other)
    assert "Gate remote" not in client.get(reverse("my_account")).content.decode()
    assert charge.pk


def test_board_records_a_payment_against_a_chosen_charge(client, w):
    old = _charge(w, 3000, "Old", D(2026, 1, 5))
    new = _charge(w, 2000, "New", D(2026, 2, 5))
    client.force_login(w["board"])
    page = client.get(reverse("lot_ledger", args=[w["lot"].pk])).content.decode()
    assert "Apply payment to" in page
    client.post(
        reverse("lot_ledger_action", args=[w["lot"].pk, "payment"]),
        {
            "amount_cents": "20.00",
            "received_on": "2026-06-01",
            "method": "check",
            "reference": "123",
            "apply_to": [str(new.pk)],
        },
    )
    assert allocation.charge_status(new) == "paid"
    assert allocation.charge_status(old) == "open"


def test_statement_and_resale_certificate_list_open_charges(w):
    from ledger import letters

    _charge(w, 3000, "Gate remote")
    paras, _bal = letters.statement_paragraphs(w["lot"])
    text = "\n".join(t for _s, t in paras)
    assert "What this amount is made of" in text and "Gate remote" in text
    resale = services.request_resale(lot=w["lot"], requester_name="Agent", actor=w["board"])
    pdf = services.resale_certificate(resale)
    assert b"OPEN CHARGES" in pdf
    assert b"Section 5407" in pdf  # legal wording unchanged
    assert SYSTEM
