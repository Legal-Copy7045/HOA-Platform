"""Fixes for the accounting QA findings (D33-acctfix): further tests beside
the scenarios in test_accounting_scenarios.py."""

import datetime

import pytest
from django.utils import timezone

from community import services as community
from community.tests.factories import LotFactory, make_member
from core.policy import SYSTEM
from core.scoping import system_access
from ledger import allocation, services
from ledger.models import PaymentApplication

pytestmark = pytest.mark.django_db
D = datetime.date
TODAY = timezone.localdate()


@pytest.fixture
def w():
    board = make_member(board=True)
    for key, value in (
        ("dues.monthly_cents", "4500"),
        ("late_fee.cents", "2500"),
        ("late_fee.grace_days", "15"),
    ):
        community.set_rule(key=key, value=value, citation="Declaration §7.2 (test)", actor=SYSTEM)
    lot = LotFactory()
    return {"board": board, "lot": lot, "owner": make_member(lots=[lot])}


def _charge(w, cents, on, desc="Fine", lot=None):
    return services.post_charge(
        lot=lot or w["lot"],
        kind="other_charge",
        amount_cents=cents,
        description=desc,
        posted_on=on,
        actor=w["board"],
    )


def _pay(w, cents, on, lot=None, **kw):
    return services.record_payment(
        lot=lot or w["lot"], amount_cents=cents, received_on=on, actor=w["board"], **kw
    )


def _aging_consistent(lot_id, on):
    a = services.aging(lot_id, on)
    assert sum(a[b] for b in services.BUCKETS) - a["credit"] == services.balance(lot_id, on)
    return a


# --- 3. Views as of a past date ---------------------------------------------------------------


def test_applications_carry_the_date_of_the_entry_that_made_them(w):
    c1 = _charge(w, 3000, D(2026, 1, 5), "Old")
    c2 = _charge(w, 2000, D(2026, 2, 5), "New")
    p1 = _pay(w, 2000, D(2026, 2, 6), reference="1", apply_to=[c2.pk])
    _pay(w, 5000, D(2026, 2, 7), reference="2")
    reversal = services.reverse_entry(p1, reason="Check returned", actor=w["board"])
    with system_access("t"):
        dates = {
            (a.source_id, a.charge_id, a.amount_cents): a.effective_on
            for a in PaymentApplication.objects.filter(lot=w["lot"])
        }
    assert dates[(p1.pk, c2.pk, 2000)] == D(2026, 2, 6)
    assert dates[(p1.pk, c2.pk, -2000)] == reversal.posted_on == TODAY
    # Before the bounce: c2 paid by the check, c1 by the second payment, 20.00 credit.
    a = _aging_consistent(w["lot"].pk, D(2026, 2, 10))
    assert a["credit"] == 2000 and sum(a[b] for b in services.BUCKETS) == 0
    # Today: the credit went to c2 when the check bounced.
    a = _aging_consistent(w["lot"].pk, TODAY)
    assert a["credit"] == 0 and sum(a[b] for b in services.BUCKETS) == 0
    assert allocation.charge_status(c1) == allocation.charge_status(c2) == "paid"


def test_credit_applied_by_hand_takes_effect_on_the_day_it_is_applied(w):
    with allocation.suspended():
        payment = _pay(w, 1000, D(2026, 3, 1), reference="5")
    charge = _charge(w, 1000, D(2026, 3, 2))  # posting applies the earlier credit
    with system_access("t"):
        (row,) = PaymentApplication.objects.filter(source=payment)
    assert row.effective_on == D(2026, 3, 2)
    with allocation.suspended():
        later = _charge(w, 700, D(2026, 3, 3), "Later")
        credit = _pay(w, 700, D(2026, 3, 4), reference="6")
    allocation.apply(credit, [later.pk])
    with system_access("t"):
        (row,) = PaymentApplication.objects.filter(source=credit)
    assert row.effective_on == TODAY and allocation.charge_status(charge) == "paid"
    a = _aging_consistent(w["lot"].pk, D(2026, 3, 5))  # not applied yet on that day
    assert a["credit"] == 700 and sum(a[b] for b in services.BUCKETS) == 700
