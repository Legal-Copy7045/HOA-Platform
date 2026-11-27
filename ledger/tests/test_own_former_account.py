"""Own matters on a former ownership account (D87, findings BUG-001 / SEC-001).

A board member who sold a home is no longer an owner of it, but the dormant
seller account (and an account a removed co-owner left) is still theirs:
nobody posts charges, credits or payments to it, reverses its entries, moves
money onto it or proposes its refund with board or staff powers. Another
board member does."""

from unittest import mock

import pytest

from audit.models import AuditEvent
from community import services as community
from community.models import Account, Ownership, Role
from community.tests.factories import LotFactory, PersonFactory, days_ago, make_member
from core import conflicts
from core.policy import SYSTEM
from core.scoping import system_access
from ledger import disbursements as disb
from ledger import services
from ledger.models import EntryKind, LedgerEntry

pytestmark = pytest.mark.django_db


@pytest.fixture
def w():
    lot = LotFactory()
    seller = make_member(board=True, lots=[lot])
    board2 = make_member(board=True)
    with system_access("t"):
        old = Account.objects.active().get(lot=lot)
    buyer = PersonFactory()
    with mock.patch("community.tasks.notify_board.defer"):
        community.transfer_lot(lot, new_owners=[buyer], transfer_date=days_ago(10), actor=SYSTEM)
    with system_access("t"):
        old.refresh_from_db()
        new = Account.objects.active().get(lot=lot)
    assert not old.is_active and new.pk != old.pk
    return {"lot": lot, "seller": seller, "board2": board2, "old": old, "new": new}


def _refusals():
    return AuditEvent.objects.filter(action=conflicts.AUDIT_ACTION).count()


def test_a_sellers_dormant_account_stays_their_own_matter(w):
    assert not conflicts.own_matter(w["seller"], w["lot"])  # no longer their home
    assert conflicts.own_matter(w["seller"], w["old"])
    assert not conflicts.own_matter(w["seller"], w["new"])
    assert not conflicts.own_matter(w["board2"], w["old"])
    entry = services.post_charge(
        lot=w["lot"],
        kind=EntryKind.OTHER_CHARGE,
        amount_cents=500,
        description="Fine",
        account_id=w["old"].pk,
        actor=w["board2"],
    )
    assert conflicts.own_matter(w["seller"], entry)  # its account is theirs


def test_seller_cannot_post_credit_or_charge_to_their_dormant_account(w):
    before = _refusals()
    with pytest.raises(services.LedgerError, match="your own"):
        services.post_credit(
            lot=w["lot"],
            amount_cents=1200,
            description="Waive",
            account_id=w["old"].pk,
            actor=w["seller"],
        )
    with pytest.raises(services.LedgerError, match="your own"):
        services.post_charge(
            lot=w["lot"],
            kind=EntryKind.OTHER_CHARGE,
            amount_cents=100,
            description="x",
            account_id=w["old"].pk,
            actor=w["seller"],
        )
    # A charge dated inside their era lands on their account by the date rule.
    with pytest.raises(services.LedgerError, match="your own"):
        services.post_charge(
            lot=w["lot"],
            kind=EntryKind.OTHER_CHARGE,
            amount_cents=100,
            description="x",
            posted_on=days_ago(30),
            actor=w["seller"],
        )
    assert _refusals() == before + 3
    with system_access("t"):
        assert not LedgerEntry.objects.filter(account=w["old"]).exists()
    # The buyer's account is not theirs; another board member does the rest.
    services.post_credit(lot=w["lot"], amount_cents=100, description="Goodwill", actor=w["seller"])
    services.post_credit(
        lot=w["lot"],
        amount_cents=100,
        description="Waive",
        account_id=w["old"].pk,
        actor=w["board2"],
    )


def test_seller_cannot_record_a_backdated_payment_onto_their_account(w):
    with pytest.raises(services.LedgerError, match="your own"):
        services.record_payment(
            lot=w["lot"], amount_cents=900, received_on=days_ago(30), reference="41",
            actor=w["seller"],
        )  # fmt: skip
    with pytest.raises(services.LedgerError, match="your own"):
        services.record_payment(
            lot=w["lot"], amount_cents=900, received_on=days_ago(2), reference="42",
            account_id=w["old"].pk, actor=w["seller"],
        )  # fmt: skip
    with pytest.raises(services.LedgerError, match="your own"):
        services.record_payments(
            [services.BatchPayment(w["lot"], 900, days_ago(30), "43")], actor=w["seller"]
        )
    with system_access("t"):
        assert not LedgerEntry.objects.filter(lot=w["lot"], kind=EntryKind.PAYMENT).exists()
    # Today's payment is the buyer's: theirs to record.
    entry = services.record_payment(
        lot=w["lot"], amount_cents=900, received_on=days_ago(1), reference="44",
        actor=w["seller"],
    )  # fmt: skip
    assert entry.account_id == w["new"].pk


def test_seller_cannot_reverse_an_entry_on_their_dormant_account(w):
    late_fee = services.post_charge(
        lot=w["lot"],
        kind=EntryKind.OTHER_CHARGE,
        amount_cents=2500,
        description="Late fee for their last month",
        account_id=w["old"].pk,
        actor=w["board2"],
    )
    with pytest.raises(services.LedgerError, match="your own"):
        services.reverse_entry(late_fee, reason="waive", actor=w["seller"])
    services.reverse_entry(late_fee, reason="posted twice", actor=w["board2"])


def test_seller_cannot_move_a_buyers_payment_onto_their_account(w):
    pay = services.record_payment(
        lot=w["lot"], amount_cents=4500, received_on=days_ago(1), reference="55",
        actor=w["board2"],
    )  # fmt: skip
    assert pay.account_id == w["new"].pk
    before = _refusals()
    with pytest.raises(services.LedgerError, match="your own"):
        services.reassign_entry(pay, account_id=w["old"].pk, reason="settlement", actor=w["seller"])
    assert _refusals() == before + 1
    with system_access("t"):
        assert not LedgerEntry.objects.filter(moved_from=pay).exists()
    copy = services.reassign_entry(
        pay, account_id=w["old"].pk, reason="seller's settlement", actor=w["board2"]
    )
    assert copy.account_id == w["old"].pk


def test_seller_cannot_propose_their_own_refund(w):
    services.post_credit(
        lot=w["lot"], amount_cents=5000, description="Overpaid", account_id=w["old"].pk,
        actor=w["board2"],
    )  # fmt: skip
    with pytest.raises(disb.DisbursementError, match="owner on this account"):
        disb.propose_refund(
            account=w["old"], amount_cents=5000, forwarding_address="9 Far Road",
            actor=w["seller"],
        )  # fmt: skip
    manager = make_member(roles=[Role.MANAGER])
    batch = disb.propose_refund(
        account=w["old"], amount_cents=5000, forwarding_address="9 Far Road", actor=manager
    )
    with pytest.raises(disb.DisbursementError, match="were an owner"):
        disb.approve(batch, actor=w["seller"])


def test_a_removed_co_owner_keeps_the_open_account_as_their_own(w):
    lot = LotFactory()
    stays = make_member(lots=[lot])
    removed = make_member(board=True)
    community.record_ownership(
        person=removed.person, lot=lot, start_date=days_ago(300), actor=SYSTEM
    )
    with system_access("t"):
        ownership = Ownership.objects.get(person=removed.person, lot=lot)
        account = Account.objects.active().get(lot=lot)
    community.end_ownership(ownership, end_date=days_ago(5), actor=SYSTEM)
    assert stays  # the account carries on for the remaining owner
    assert not conflicts.own_matter(removed, lot)
    assert conflicts.own_matter(removed, account)
    with pytest.raises(services.LedgerError, match="your own"):
        services.post_credit(
            lot=lot, amount_cents=100, description="x", account_id=account.pk, actor=removed
        )
