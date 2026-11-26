"""Money belongs to the ownership period it is for (D34-fixmoney): fixes for
regression findings BUG-001, BUG-002, BUG-005 and BUG-010. Each test is the
finding's scenario; the global invariants are checked after each."""

import datetime

import pytest
from django.urls import reverse
from django.utils import timezone

from audit.models import AuditEvent
from community import services as community
from community.models import Account, Person
from community.tests.factories import LotFactory, make_member
from core.policy import SYSTEM
from core.scoping import system_access
from ledger import accounting as acc
from ledger import allocation, services
from ledger.accounts import CURRENT
from ledger.models import ChargeNumber, EntryKind, LedgerEntry, PaymentApplication
from ledger.tests.accounting_invariants import Books

pytestmark = pytest.mark.django_db
D = datetime.date
TODAY = timezone.localdate()
DUES = 4500


def ago(n):
    return TODAY - datetime.timedelta(days=n)


@pytest.fixture
def books(app_role_connection):
    b = Books()
    b.app_role = app_role_connection
    return b


def _check(books):
    books.check(app_role_connection=books.app_role)


@pytest.fixture
def w():
    board = make_member(board=True)
    for key, value in (
        ("dues.monthly_cents", str(DUES)),
        ("late_fee.cents", "2500"),
        ("late_fee.grace_days", "15"),
    ):
        community.set_rule(key=key, value=value, citation="Declaration §7.2 (QA)", actor=SYSTEM)
    lot = LotFactory()
    return {"board": board, "lot": lot, "owner": make_member(lots=[lot])}


def _person(user):
    with system_access("t"):
        return Person.objects.get(user=user)


def _sell(w, on, lot=None):
    buyer = make_member()
    community.transfer_lot(
        lot or w["lot"], new_owners=[_person(buyer)], transfer_date=on, actor=w["board"]
    )
    return buyer


def _accounts(lot):
    with system_access("t"):
        return list(Account.objects.filter(lot=lot).order_by("opened_on", "id"))


def _entries(lot, **kw):
    with system_access("t"):
        return list(LedgerEntry.objects.filter(lot=lot, **kw).order_by("id"))


def _charge(w, cents, on, desc="Fine", **kw):
    return services.post_charge(
        lot=w["lot"],
        kind=EntryKind.OTHER_CHARGE,
        amount_cents=cents,
        description=desc,
        posted_on=on,
        actor=w["board"],
        **kw,
    )


def _pay(w, cents, on, ref, **kw):
    return services.record_payment(
        lot=w["lot"], amount_cents=cents, received_on=on, reference=ref, actor=w["board"], **kw
    )


def _dues(year, month, lot):
    services.post_monthly_assessments(year=year, month=month, actor=SYSTEM)
    with system_access("t"):
        return LedgerEntry.objects.get(
            lot=lot, kind="assessment", period=f"{year}-{month:02d}", moved_from__isnull=True
        )


# --- BUG-001: a sale recorded later with an earlier date --------------------------------------


def test_backdated_sale_moves_the_buyers_dues_and_check_to_the_buyers_account(client, w, books):
    seller_dues = _charge(w, DUES, ago(40), "Dues before the sale")
    _pay(w, DUES, ago(38), "301", apply_to=[seller_dues.pk])
    buyers_dues = _charge(w, DUES, ago(15), "Dues after the sale")
    buyers_check = _pay(w, DUES, ago(10), "302")
    (seller_account,) = _accounts(w["lot"])
    assert buyers_dues.account_id == buyers_check.account_id == seller_account.pk
    buyer = _sell(w, ago(20))  # recorded today, effective 20 days ago
    seller_account, buyer_account = _accounts(w["lot"])
    copies = _entries(w["lot"], moved_from__isnull=False)
    assert [c.moved_from_id for c in copies] == [buyers_dues.pk, buyers_check.pk]
    for copy, original in zip(copies, (buyers_dues, buyers_check), strict=True):
        assert copy.account_id == buyer_account.pk
        assert (copy.posted_on, copy.kind, copy.amount_cents, copy.reference) == (
            original.posted_on,
            original.kind,
            original.amount_cents,
            original.reference,
        )
        (reversal,) = _entries(w["lot"], reverses=original)
        assert reversal.account_id == seller_account.pk and reversal.posted_on == original.posted_on
    # Nothing was edited: the originals still carry the seller's account.
    assert _entries(w["lot"], pk=buyers_dues.pk)[0].account_id == seller_account.pk
    # Balances per period: the seller paid up, the buyer's check paid the buyer's dues.
    assert services.balance(w["lot"].pk, account=seller_account.pk) == 0
    assert services.balance(w["lot"].pk, account=buyer_account.pk) == 0
    assert allocation.charge_status(copies[0]) == "paid"
    assert AuditEvent.objects.filter(action="ledger.entries_moved").count() == 1
    # The seller's history never shows the buyer's dues or check; the buyer's account does.
    client.force_login(w["owner"])
    page = client.get(reverse("my_history")).content.decode()
    assert "Dues before the sale" in page and "Dues after the sale" not in page
    assert "302" not in page and "Moved to account" not in page
    client.force_login(buyer)
    page = client.get(reverse("my_account")).content.decode()
    assert "Dues after the sale" in page and "Dues before the sale" not in page
    _check(books)


def test_moving_a_deposited_check_is_neither_a_new_deposit_nor_a_returned_item(w, books):
    check = _pay(w, DUES, ago(10), "303")
    acc.create_deposit(entry_ids=[check.pk], deposited_on=ago(9), actor=w["board"])
    _sell(w, ago(20))
    (copy,) = _entries(w["lot"], moved_from=check)
    assert copy.pk not in {e.pk for e in acc.undeposited_payments()}
    assert acc._returned_items(ago(100), TODAY) == []
    assert acc.uncleared(TODAY)["returned"] == []
    # If the moved check then bounces, the bank does take it back.
    bounce = services.reverse_entry(copy, reason="Returned by the bank", actor=w["board"])
    assert [e.pk for e in acc._returned_items(ago(100), TODAY)] == [bounce.pk]
    _check(books)


def test_dues_moved_to_the_buyer_keep_one_assessment_a_month_and_bring_their_late_fee(w, books):
    dues = _dues(2026, 10, w["lot"])
    assert services.post_late_fees(year=2026, month=10, actor=w["board"], on=D(2026, 10, 20)) == 1
    (fee,) = _entries(w["lot"], kind="late_fee")
    assert fee.account_id == dues.account_id
    _sell(w, D(2026, 9, 25))  # the deed is dated before October's dues
    _seller, buyer_account = _accounts(w["lot"])
    copies = {c.kind: c for c in _entries(w["lot"], moved_from__isnull=False)}
    assert set(copies) == {"assessment", "late_fee"}
    assert {c.account_id for c in copies.values()} == {buyer_account.pk}
    assert copies["assessment"].period == copies["late_fee"].period == "2026-10"
    # Posting October again still adds nothing; the month's fee is still once.
    assert services.post_monthly_assessments(year=2026, month=10, actor=SYSTEM) == 0
    assert services.post_late_fees(year=2026, month=10, actor=w["board"], on=D(2026, 10, 25)) == 0
    assert services.balance(w["lot"].pk, account=buyer_account.pk) == DUES + 2500
    with system_access("t"):
        assert ChargeNumber.objects.filter(entry=copies["assessment"]).exists()
    _check(books)


def test_an_account_the_board_chose_by_hand_is_kept_when_a_later_sale_is_backdated(w, books):
    _sell(w, ago(100))
    first, _second = _accounts(w["lot"])
    refund_check = _charge(w, 1200, ago(5), "Refund of credit", account_id=first.pk)
    _sell(w, ago(10))
    assert _entries(w["lot"], moved_from__isnull=False) == []
    assert _entries(w["lot"], reverses=refund_check) == []
    _check(books)


def test_ownership_recorded_with_its_true_start_moves_entries_from_the_gap(w, books):
    lot = LotFactory()
    w2 = {**w, "lot": lot}
    first_owner = make_member()
    community.record_ownership(
        person=_person(first_owner), lot=lot, start_date=ago(200), actor=w["board"]
    )
    community.end_ownership(_person_ownership(first_owner, lot), end_date=ago(50), actor=w["board"])
    gap_charge = _charge(w2, 900, ago(30), "Gap fee")
    assert gap_charge.account_id is None  # dated after the last owner left
    newcomer = make_member()
    community.record_ownership(person=_person(newcomer), lot=lot, start_date=ago(40), actor=SYSTEM)
    (copy,) = _entries(lot, moved_from=gap_charge)
    assert copy.account_id == _accounts(lot)[1].pk
    _check(books)


def _person_ownership(user, lot):
    from community.models import Ownership

    with system_access("t"):
        return Ownership.objects.get(person__user=user, lot=lot)


# --- BUG-002: a late fee belongs with the dues it penalises -------------------------------------


def test_late_fee_for_the_sellers_unpaid_dues_stays_on_the_sellers_account(client, w, books):
    dues = _dues(2026, 10, w["lot"])
    buyer = _sell(w, D(2026, 10, 12))
    seller_account, buyer_account = _accounts(w["lot"])
    assert dues.account_id == seller_account.pk
    assert services.post_late_fees(year=2026, month=10, actor=w["board"], on=D(2026, 10, 20)) == 1
    (fee,) = _entries(w["lot"], kind="late_fee")
    assert fee.account_id == seller_account.pk
    assert services.balance(w["lot"].pk, account=buyer_account.pk) == 0
    assert services.past_due(w["lot"].pk, D(2026, 11, 1), account=buyer_account.pk) == 0
    client.force_login(buyer)
    assert "Late fee" not in client.get(reverse("my_account")).content.decode()
    _check(books)


# --- BUG-005: re-applications made by a reversal before migration 0012 -------------------------


def test_reapplication_by_a_reversal_is_dated_by_the_reversal_after_the_correction(w, books):
    """Before 0012: payment P paid charge A; charge B was open; A was
    reversed on Mar 5 and P re-applied to B. 0012 dated P->B Feb 1."""
    with allocation.suspended():
        a = _charge(w, 10_000, D(2026, 3, 1) - datetime.timedelta(days=63), "A")  # Jan 1
        p = _pay(w, 10_000, D(2026, 1, 10), "401")
        b = _charge(w, 10_000, D(2026, 2, 1), "B")
        with system_access("t"):
            rev = LedgerEntry.objects.create(
                lot=w["lot"],
                posted_on=D(2026, 3, 5),
                kind=EntryKind.REVERSAL,
                amount_cents=-10_000,
                reverses=a,
                description="Reversal",
            )
    # Rebuild the rows as migrations 0009 and 0012 left them.
    book = allocation.LotBook()
    rows = []
    for e in (a, p, b, rev):
        rows += book.post(allocation._Entry.of(e), kind=allocation.MIGRATION)
    dates = {e.pk: e.posted_on for e in (a, p, b, rev)}
    with system_access("t"):
        for s, c, cents, _k in rows:
            # 0012: an undo row by its reversal, any other by its later entry.
            on = dates[rev.pk] if cents < 0 else max(dates[s], dates[c])
            PaymentApplication.objects.create(
                lot=w["lot"],
                source_id=s,
                charge_id=c,
                amount_cents=cents,
                kind="migration",
                effective_on=on,
            )
    feb15 = services.aging(w["lot"].pk, D(2026, 2, 15))
    assert feb15["credit"] == 0 and sum(feb15[k] for k in services.BUCKETS) == 0  # the bug
    assert services.balance(w["lot"].pk, D(2026, 2, 15)) == 10_000
    with system_access("t"):
        fixed = allocation.redate_reversal_reapplications(LedgerEntry, PaymentApplication)
    assert fixed == 1
    feb15 = services.aging(w["lot"].pk, D(2026, 2, 15))
    assert sum(feb15[k] for k in services.BUCKETS) - feb15["credit"] == 10_000
    assert services.past_due(w["lot"].pk, D(2026, 2, 15)) == 10_000  # B open, A paid by P
    state = allocation.state_as_of(w["lot"].pk, D(2026, 2, 15))
    assert state.open[a.pk] == 0 and state.open[b.pk] == 10_000
    assert allocation.charge_status(b) == "paid"  # today: B paid by P, totals unchanged
    _check(books)


def test_correction_leaves_live_history_alone(w, books):
    a = _charge(w, 10_000, D(2026, 1, 1), "A")
    _pay(w, 10_000, D(2026, 1, 10), "402")
    _charge(w, 10_000, D(2026, 2, 1), "B")
    services.reverse_entry(a, reason="Posted by mistake", actor=w["board"])
    with system_access("t"):
        before = PaymentApplication.objects.count()
        assert allocation.redate_reversal_reapplications(LedgerEntry, PaymentApplication) == 0
        assert PaymentApplication.objects.count() == before
    _check(books)


# --- BUG-010: two ownership periods of the same lot ---------------------------------------------


def test_former_owner_with_two_periods_sees_each_periods_own_account(client, w, books):
    lot = LotFactory()
    twice = make_member()
    me = _person(twice)
    community.record_ownership(person=me, lot=lot, start_date=D(2015, 1, 1), actor=w["board"])
    _sell(w, D(2018, 1, 1), lot=lot)
    services.post_charge(
        lot=lot,
        kind="other_charge",
        amount_cents=1111,
        description="First era fee",
        posted_on=D(2016, 5, 1),
        actor=w["board"],
    )
    community.transfer_lot(lot, new_owners=[me], transfer_date=D(2021, 1, 1), actor=w["board"])
    services.post_charge(
        lot=lot,
        kind="other_charge",
        amount_cents=2222,
        description="Second era fee",
        posted_on=D(2022, 5, 1),
        actor=w["board"],
    )
    _sell(w, D(2025, 1, 1), lot=lot)
    first, _x, second, _y = _accounts(lot)
    client.force_login(twice)
    response = client.get(reverse("my_history"))
    periods = sorted(response.context["periods"], key=lambda p: p["start"])
    assert [p["account"].pk for p in periods] == [first.pk, second.pk]
    assert [e.description for e in periods[0]["statement"].entries] == ["First era fee"]
    assert [e.description for e in periods[1]["statement"].entries] == ["Second era fee"]
    assert periods[0]["sold_balance"] == 1111 and periods[1]["sold_balance"] == 2222
    _check(books)


def test_current_account_reader_still_matches_after_a_move(w, books):
    _charge(w, DUES, ago(15), "Dues after the sale")
    _sell(w, ago(20))
    assert services.balance(w["lot"].pk, account=CURRENT) == DUES
    statement = services.owner_statement(w["lot"].pk, ago(20))
    assert [e.description for e in statement.entries] == ["Dues after the sale"]
    _check(books)
