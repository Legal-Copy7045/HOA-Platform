"""Fixes for the accounting QA findings (D33-acctfix): further tests beside
the scenarios in test_accounting_scenarios.py."""

import datetime

import pytest
from django.urls import reverse
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


# --- 1. Money belongs to the ownership account it was posted under -----------------------------


def _ago(n):
    return TODAY - datetime.timedelta(days=n)


def _sell(w, on):
    from community.models import Person

    buyer = make_member()
    with system_access("t"):
        person = Person.objects.get(user=buyer)
    community.transfer_lot(w["lot"], new_owners=[person], transfer_date=on, actor=SYSTEM)
    return buyer


def _accounts(lot):
    from community.models import Account

    with system_access("t"):
        old, new = Account.objects.filter(lot=lot).order_by("opened_on")
    return old, new


def _seller_with_credit(w):
    _charge(w, 4500, _ago(60), "Dues")
    _pay(w, 13_500, _ago(50), reference="900")  # 90.00 left as credit
    buyer = _sell(w, _ago(20))
    return buyer, *_accounts(w["lot"])


def test_after_a_sale_each_account_keeps_its_own_money(client, w):
    from ledger.tests.accounting_invariants import assert_lot_books

    buyer, old, new = _seller_with_credit(w)
    fee = _charge(w, 3000, _ago(5), "Buyer's gate key")
    lot = w["lot"].pk
    assert fee.account_id == new.pk and allocation.charge_status(fee) == "open"
    assert services.balance(lot, account=old.pk) == -9000
    assert services.balance(lot, account=new.pk) == 3000
    assert services.balance(lot) == -6000  # the lot: the sum of its accounts
    assert allocation.unapplied_credit(lot, account=old.pk) == 9000
    assert allocation.unapplied_credit(lot, account=new.pk) == 0
    assert services.past_due(lot) == 3000 and services.amount_due(lot).amount_cents == 3000
    assert [r[0].pk for r in services.delinquent_lots(min_age_days=0)] == [lot]
    assert_lot_books()
    client.force_login(w["board"])
    page = client.get(reverse("lot_ledger", args=[lot])).content.decode()
    assert "Credit owed to former owner: <strong>$90.00</strong>" in page
    assert f"Account {old.number}" in page
    client.force_login(w["owner"])
    page = client.get(reverse("my_history")).content.decode()
    assert "Credit owed to you: <strong>$90.00</strong>" in page and "gate key" not in page
    client.force_login(buyer)
    page = client.get(reverse("my_account")).content.decode()
    assert "$90.00" not in page and "$30.00" in page and "gate key" in page


def test_board_refunds_the_former_owners_credit_on_the_old_account(client, w):
    _buyer, old, new = _seller_with_credit(w)
    client.force_login(w["board"])
    url = reverse("lot_ledger_action", args=[w["lot"].pk, "charge"])
    client.post(
        url,
        {
            "kind": "other_charge",
            "amount_cents": "90.00",
            "description": "Credit refunded, check 1201",
            "account_id": str(old.pk),
        },
    )
    assert services.balance(w["lot"].pk, account=old.pk) == 0
    assert services.balance(w["lot"].pk, account=new.pk) == 0
    page = client.get(reverse("lot_ledger", args=[w["lot"].pk])).content.decode()
    assert 'data-testid="former-accounts"' not in page  # nothing owed either way now
    client.force_login(w["owner"])
    page = client.get(reverse("my_history")).content.decode()
    assert "Credit refunded, check 1201" in page and "Credit owed to you" not in page


def test_entries_follow_their_date_and_reversals_their_entry(w):
    _charge(w, 4500, _ago(60), "Dues")
    buyer_day = _ago(20)
    _sell(w, buyer_day)
    old, new = _accounts(w["lot"])
    late_check = _pay(w, 4500, _ago(25), reference="901")  # dated before the sale
    assert late_check.account_id == old.pk
    assert services.balance(w["lot"].pk, account=old.pk) == 0
    bounce = services.reverse_entry(late_check, reason="Returned", actor=w["board"])
    assert bounce.posted_on == TODAY and bounce.account_id == old.pk
    assert services.balance(w["lot"].pk, account=new.pk) == 0
    on_closing = _pay(w, 1000, buyer_day, reference="902")
    assert on_closing.account_id == new.pk  # the sale date belongs to the buyers


def test_a_payment_pays_only_charges_of_its_own_account(w):
    _charge(w, 4500, _ago(60), "Seller dues")
    _sell(w, _ago(20))
    old, _new = _accounts(w["lot"])
    buyer_fee = _charge(w, 2000, _ago(5), "Buyer fee")
    with pytest.raises(services.LedgerError, match="open charges"):
        _pay(w, 2000, TODAY, reference="903", account_id=old.pk, apply_to=[buyer_fee.pk])
    settlement = _pay(w, 4500, TODAY, reference="904", account_id=old.pk)  # seller's arrears
    assert settlement.account_id == old.pk
    assert services.balance(w["lot"].pk, account=old.pk) == 0
    assert allocation.charge_status(buyer_fee) == "open"
    other = LotFactory()
    make_member(lots=[other])
    from community.models import Account

    with system_access("t"):
        foreign = Account.objects.get(lot=other)
    with pytest.raises(services.LedgerError, match="this home's accounts"):
        _pay(w, 100, TODAY, reference="905", account_id=foreign.pk)


def test_statement_resale_certificate_and_letters_use_the_current_account(w):
    from ledger import letters

    _charge(w, 4500, _ago(60), "Seller dues")  # left unpaid by the seller
    _sell(w, _ago(20))
    _charge(w, 1500, _ago(10), "Buyer fine")
    paras, balance = letters.statement_paragraphs(w["lot"])
    text = "\n".join(t for _s, t in paras)
    assert balance == 1500 and "Buyer fine" in text and "Seller dues" not in text
    assert services.past_due(w["lot"].pk) == 6000
    assert letters._past_due(w["lot"]) == 1500
    resale = services.request_resale(lot=w["lot"], requester_name="Agent", actor=w["board"])
    pdf = services.resale_certificate(resale)
    assert b"15.00 unpaid" in pdf and b"60.00" not in pdf


def test_board_forms_offer_the_account_only_after_a_sale(client, w):
    client.force_login(w["board"])
    page = client.get(reverse("lot_ledger", args=[w["lot"].pk])).content.decode()
    assert 'name="account_id"' not in page
    _sell(w, _ago(20))
    old, _new = _accounts(w["lot"])
    page = client.get(reverse("lot_ledger", args=[w["lot"].pk])).content.decode()
    assert page.count('name="account_id"') == 3 and f"{old.number} · former owner" in page
    client.post(
        reverse("lot_ledger_action", args=[w["lot"].pk, "credit"]),
        {"amount_cents": "5.00", "description": "Goodwill", "account_id": str(old.pk)},
    )
    assert services.balance(w["lot"].pk, account=old.pk) == -500


def test_buyer_and_seller_never_see_each_others_account(client, w):
    buyer, _old, _new = _seller_with_credit(w)
    _charge(w, 3000, _ago(5), "Buyer's gate key")
    client.force_login(buyer)
    assert "$90.00" not in client.get(reverse("my_account")).content.decode()
    history = client.get(reverse("my_history"))
    assert history.status_code == 403 or "$90.00" not in history.content.decode()
    assert client.get(reverse("my_statement_pdf", args=[w["lot"].pk])).status_code == 200
    client.force_login(w["owner"])
    assert client.get(reverse("my_account")).status_code == 403
    assert client.get(reverse("my_statement_pdf", args=[w["lot"].pk])).status_code in (403, 404)
    for name in ("lot_ledger",):
        assert client.get(reverse(name, args=[w["lot"].pk])).status_code in (403, 404)


def test_migration_assigns_accounts_and_separates_money_applied_across_them(w):
    """Migration 0015 on history from before D33-acctfix-1: entries without
    an account, and a seller's credit already applied to the buyer's dues."""
    import importlib

    from django.apps import apps
    from django.db import connection

    from ledger.models import LedgerEntry, PaymentApplication
    from ledger.tests.accounting_invariants import assert_lot_books

    migration = importlib.import_module("ledger.migrations.0015_entry_accounts")
    _sell(w, _ago(20))
    old, new = _accounts(w["lot"])
    lot = w["lot"]
    with system_access("t"):
        charge, pay, buyer_dues = LedgerEntry.objects.bulk_create(
            [
                LedgerEntry(
                    lot=lot,
                    posted_on=_ago(60),
                    kind="other_charge",
                    amount_cents=4500,
                    description="Dues",
                ),
                LedgerEntry(
                    lot=lot,
                    posted_on=_ago(50),
                    kind="payment",
                    amount_cents=-9000,
                    description="Payment (check)",
                ),
                LedgerEntry(
                    lot=lot,
                    posted_on=_ago(5),
                    kind="assessment",
                    period="2099-01",
                    amount_cents=4500,
                    description="Buyer dues",
                ),
            ]
        )
        PaymentApplication.objects.bulk_create(
            [
                PaymentApplication(
                    lot=lot,
                    source=pay,
                    charge=charge,
                    amount_cents=4500,
                    kind="auto",
                    effective_on=_ago(50),
                ),
                PaymentApplication(
                    lot=lot,
                    source=pay,
                    charge=buyer_dues,
                    amount_cents=4500,
                    kind="auto",
                    effective_on=_ago(5),
                ),
            ]
        )
        reversal = LedgerEntry.objects.create(
            lot=lot,
            posted_on=TODAY,
            kind="reversal",
            amount_cents=-4500,
            reverses=charge,
            description="Reversal",
            account=None,
        )
    assert reversal.account_id is None  # its original had no account yet

    class Editor:
        pass

    editor = Editor()
    editor.connection = connection
    with system_access("t"):  # live models stand in for the historical ones
        migration.assign_accounts(apps, editor)
    with system_access("t"):
        got = dict(LedgerEntry.objects.filter(lot=lot).values_list("pk", "account_id"))
    assert got[charge.pk] == got[pay.pk] == got[reversal.pk] == old.pk
    assert got[buyer_dues.pk] == new.pk
    assert allocation.charge_status(buyer_dues) == "open"
    assert allocation.unapplied_credit(lot.pk, account=old.pk) == 9000
    assert_lot_books()
