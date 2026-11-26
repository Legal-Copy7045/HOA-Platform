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
from ledger.models import LedgerEntry, PaymentApplication

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


# --- 2. Late fees follow the month's own dues --------------------------------------------------


def _october(w):
    services.post_monthly_assessments(year=2026, month=10, actor=SYSTEM)
    with system_access("t"):
        return LedgerEntry.objects.get(lot=w["lot"], kind="assessment", period="2026-10")


def _late_lots(on):
    rows = services.late_fee_candidates(year=2026, month=10, on=on)
    return {r["lot"].pk: r["balance_cents"] for r in rows}


def test_late_fee_only_while_that_months_dues_are_open(w):
    dues = _october(w)
    _charge(w, 3000, D(2026, 9, 1), "Older fine")  # older and unpaid, but not October's dues
    _pay(w, 4500, D(2026, 10, 2), reference="10", apply_to=[dues.pk])
    assert w["lot"].pk not in _late_lots(D(2026, 10, 20))
    assert services.balance(w["lot"].pk) == 3000  # owes money, yet the dues were on time


def test_partly_paid_dues_are_late_for_what_is_left(w):
    _october(w)
    _pay(w, 4000, D(2026, 10, 3), reference="11")
    assert _late_lots(D(2026, 10, 16)) == {}  # still in the grace period
    assert _late_lots(D(2026, 10, 20)) == {w["lot"].pk: 500}


def test_prepaid_credit_covers_the_dues_so_no_late_fee(w):
    _pay(w, 9000, D(2026, 9, 20), reference="12")
    _october(w)
    assert _late_lots(D(2026, 10, 20)) == {}


def test_dues_paid_after_the_check_date_still_count_as_open_then(w):
    _october(w)
    _pay(w, 4500, D(2026, 10, 25), reference="13")
    assert _late_lots(D(2026, 10, 20)) == {w["lot"].pk: 4500}
    assert _late_lots(D(2026, 10, 26)) == {}


# --- 4. Delinquency notices state the past-due amount ------------------------------------------


def _notice_text(fn, *args, **kw):
    from unittest import mock

    from ledger import letters

    rendered = []
    with mock.patch.object(letters.pdf, "render", side_effect=lambda p, **k: rendered.append(p)):
        fn(*args, **kw)
    return "\n".join(t for _s, t in rendered[0])


def test_every_notice_path_states_only_the_past_due_amount(w):
    from ledger import letters
    from ledger.models import LetterKind

    _charge(w, 4500, TODAY - datetime.timedelta(days=40), "Old dues")
    _charge(w, 1000, TODAY, "Not yet due")
    _charge(w, 2000, TODAY + datetime.timedelta(days=3), "Due later")
    assert services.past_due(w["lot"].pk) == 4500 and services.balance(w["lot"].pk) == 7500
    for kind in (LetterKind.FIRST, LetterKind.SECOND, LetterKind.FINAL):
        text = _notice_text(letters.notice_pdf, w["lot"], kind)
        assert "delinquency of $45.00" in text and "$55.00" in text  # balance as of today
    text = _notice_text(services.generate_notice, w["lot"], "first", actor=w["board"])
    assert "delinquency of $45.00" in text


def test_statement_message_amount_placeholder_is_the_past_due_amount(w):
    from ledger import letters
    from ledger.models import LetterKind

    services.save_letter_template(
        LetterKind.STATEMENT,
        subject="",
        body="Past due: {amount}.",
        certified=False,
        actor=w["board"],
    )
    _charge(w, 4500, TODAY - datetime.timedelta(days=40), "Old dues")
    _charge(w, 1000, TODAY, "Not yet due")
    paras, balance = letters.statement_paragraphs(w["lot"])
    assert balance == 5500 and "Past due: $45.00." in "\n".join(t for _s, t in paras)


def test_past_due_follows_the_open_charges_not_the_netted_balance(w):
    """A payment the owner directed to a charge not yet due leaves the older
    charge open, so it is still past due."""
    old = _charge(w, 3000, D(2026, 9, 5), "Fine")
    later = _charge(w, 3000, TODAY + datetime.timedelta(days=5), "Due later")
    _pay(w, 3000, TODAY, reference="40", apply_to=[later.pk])
    assert services.balance(w["lot"].pk) == 3000
    assert allocation.charge_status(old) == "open" and services.past_due(w["lot"].pk) == 3000
    assert services.past_due(w["lot"].pk, D(2026, 9, 5)) == 0  # due that day: not yet late
    assert services.past_due(w["lot"].pk, D(2026, 9, 6)) == 3000
