# ruff: noqa: F811  (the scenario fixtures are imported, then used as arguments)
"""Entry moves after a backdated ownership change (fixres): BUG-001 (the
two-step path), BUG-002 (a moved payment keeps its charges), BUG-003 /
R2-003 (the board is told, and moved copies are marked), BUG-007 (the lot is
locked first). Each test is the finding's scenario; the global accounting
invariants are checked after each."""

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from audit.models import AuditEvent
from community import services as community
from community.models import Ownership
from community.tests.factories import LotFactory, make_member
from core import my_items
from core.scoping import system_access
from ledger import allocation, services
from ledger.models import EntryMoveNotice
from ledger.tests.test_fixmoney import (  # noqa: F401  (fixtures)
    DUES,
    _accounts,
    _charge,
    _check,
    _entries,
    _pay,
    _person,
    _sell,
    ago,
    books,
    w,
)

pytestmark = pytest.mark.django_db


def _sellers_ownership(w):
    with system_access("t"):
        return Ownership.objects.get(lot=w["lot"], end_date__isnull=True)


# --- BUG-001: end with a past date, then add the buyer in a separate step ---------------------


def _post_buyers_month(w):
    dues = _charge(w, DUES, ago(15), "December dues")
    check = _pay(w, DUES, ago(10), "5005")
    (seller_account,) = _accounts(w["lot"])
    assert dues.account_id == check.account_id == seller_account.pk
    return dues, check


@pytest.mark.parametrize("second_step", ["record", "transfer"])
def test_ending_with_a_past_date_then_adding_the_buyer_moves_the_buyers_entries(
    client, w, books, second_step
):
    dues, check = _post_buyers_month(w)
    community.end_ownership(_sellers_ownership(w), end_date=ago(20), actor=w["board"])
    # Step 1 alone moves nothing: no account covers those dates yet.
    assert _entries(w["lot"], moved_from__isnull=False) == []
    buyer = make_member()
    if second_step == "record":
        community.record_ownership(
            person=_person(buyer), lot=w["lot"], start_date=ago(20), actor=w["board"]
        )
    else:
        community.transfer_lot(
            w["lot"], new_owners=[_person(buyer)], transfer_date=ago(20), actor=w["board"]
        )
    seller_account, buyer_account = _accounts(w["lot"])
    copies = _entries(w["lot"], moved_from__isnull=False)
    assert [c.moved_from_id for c in copies] == [dues.pk, check.pk]
    assert {c.account_id for c in copies} == {buyer_account.pk}
    assert services.balance(w["lot"].pk, account=seller_account.pk) == 0
    assert services.balance(w["lot"].pk, account=buyer_account.pk) == 0
    client.force_login(w["owner"])
    assert "December dues" not in client.get(reverse("my_history")).content.decode()
    client.force_login(buyer)
    assert "December dues" in client.get(reverse("my_account")).content.decode()
    _check(books)


def test_entry_posted_by_hand_on_the_closed_account_stays_when_the_buyer_is_added(w, books):
    community.end_ownership(_sellers_ownership(w), end_date=ago(20), actor=w["board"])
    (seller_account,) = _accounts(w["lot"])
    # After the closing was recorded, the board chose the seller's account.
    refund = _charge(w, 1200, ago(5), "Refund of credit", account_id=seller_account.pk)
    community.record_ownership(
        person=_person(make_member()), lot=w["lot"], start_date=ago(20), actor=w["board"]
    )
    assert _entries(w["lot"], reverses=refund) == []
    assert _entries(w["lot"], moved_from__isnull=False) == []
    _check(books)


# --- BUG-002: a moved payment keeps the charges it was directed to ----------------------------


def test_moved_check_stays_on_the_charge_it_paid_when_both_move(w, books):
    fee = _charge(w, 2000, ago(18), "Resale fee")  # older, so the rule would pay it first
    december = _charge(w, DUES, ago(15), "December dues")
    check = _pay(w, DUES, ago(10), "5006", apply_to=[december.pk])
    assert allocation.charge_status(december) == "paid"
    _sell(w, ago(20))
    copy = {c.moved_from_id: c for c in _entries(w["lot"], moved_from__isnull=False)}
    assert allocation.applications_of(copy[check.pk].pk) == [(copy[december.pk].pk, DUES)]
    assert allocation.charge_status(copy[december.pk]) == "paid"
    assert allocation.charge_status(copy[fee.pk]) == "open"
    _check(books)


# --- BUG-003 / R2-003: the board sees what moved ----------------------------------------------


def test_moves_give_the_board_an_item_and_mark_the_copies(client, w, books):
    _dues, check = _post_buyers_month(w)
    buyer = _sell(w, ago(20))
    seller_account, buyer_account = _accounts(w["lot"])
    (notice,) = EntryMoveNotice.objects.unscoped("t").all()
    assert notice.count == 2 and notice.checked_at is None
    assert (notice.from_accounts, notice.to_accounts) == (
        seller_account.number,
        buyer_account.number,
    )
    items = [i for i in my_items.items_for(w["board"]) if "moved to another account" in i.title]
    (item,) = items
    assert item.title.startswith(f"Lot {w['lot'].number}: 2 entries")
    assert seller_account.number in item.detail and item.url.endswith("#moved")
    # The board page lists them and marks the copies.
    client.force_login(w["board"])
    page = client.get(reverse("lot_ledger", args=[w["lot"].pk])).content.decode()
    assert 'id="moved"' in page
    assert f"Moved from account {seller_account.number} on" in page
    # The owner's statement line says where it came from.
    client.force_login(buyer)
    page = client.get(reverse("my_account")).content.decode()
    assert f"Moved from account {seller_account.number} on" in page
    # An owner can't mark it checked; the board can, once.
    url = reverse("lot_ledger_action", args=[w["lot"].pk, "movechecked"])
    assert client.post(url, {"notice": notice.pk}).status_code in (403, 404)
    client.force_login(w["board"])
    client.post(url, {"notice": notice.pk})
    notice.refresh_from_db()
    assert notice.checked_by == w["board"]
    assert AuditEvent.objects.filter(action="ledger.moves_checked").count() == 1
    assert not [i for i in my_items.items_for(w["board"]) if "moved to another" in i.title]
    assert check.pk in [c.moved_from_id for c in _entries(w["lot"], moved_from__isnull=False)]
    _check(books)


def test_owner_of_another_lot_cannot_check_a_move_notice(client, w, books):
    _post_buyers_month(w)
    _sell(w, ago(20))
    (notice,) = EntryMoveNotice.objects.unscoped("t").all()
    other = make_member(lots=[LotFactory()])
    client.force_login(other)
    url = reverse("lot_ledger_action", args=[w["lot"].pk, "movechecked"])
    assert client.post(url, {"notice": notice.pk}).status_code in (403, 404)
    assert client.get(reverse("lot_ledger", args=[w["lot"].pk])).status_code in (403, 404)
    notice.refresh_from_db()
    assert notice.checked_at is None
    _check(books)


# --- BUG-007: the lot is locked before the periods are read -----------------------------------


def test_account_periods_are_read_after_locking_the_lot(w):
    with CaptureQueriesContext(connection) as ctx:
        community.end_ownership(_sellers_ownership(w), end_date=ago(1), actor=w["board"])
    sqls = [q["sql"] for q in ctx.captured_queries]
    lock = next(i for i, q in enumerate(sqls) if 'FROM "community_lot"' in q and "FOR UPDATE" in q)
    periods = next(i for i, q in enumerate(sqls) if 'FROM "community_account"' in q)
    assert lock < periods
