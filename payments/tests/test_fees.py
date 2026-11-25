"""Decision D21: owners pay a convenience fee, shown before they pay."""

import pytest
from django.urls import reverse

from community import services as community
from community.tests.factories import LotFactory, make_member
from core.policy import SYSTEM
from payments.services import convenience_fee, payment_options

pytestmark = pytest.mark.django_db


def _set(key, value):
    community.set_rule(key=key, value=value, citation="Board resolution (test)", actor=SYSTEM)


def test_no_fee_until_the_board_sets_one():
    assert convenience_fee(10_000, "card") == 0
    assert convenience_fee(10_000, "bank") == 0


def test_card_fee_is_a_percentage_rounded_up_and_bank_fee_is_flat():
    _set("payments.card_fee_bp", "295")
    _set("payments.bank_fee_cents", "100")
    assert convenience_fee(10_000, "card") == 295
    assert convenience_fee(4_501, "card") == 133  # 132.78 rounds up
    assert convenience_fee(4_501, "bank") == 100
    options = {o["method"]: o for o in payment_options(4_500)}
    assert options["card"]["total_cents"] == 4_500 + 133
    assert options["bank"]["citation"] == "Board resolution (test)"


def test_fee_shown_on_my_account(client):
    from ledger import services as ledger

    _set("payments.card_fee_bp", "300")
    lot = LotFactory()
    owner = make_member(lots=[lot])
    ledger.post_charge(
        lot=lot,
        kind="other_charge",
        amount_cents=10_000,
        description="x",
        actor=make_member(board=True),
    )
    client.force_login(owner)
    page = client.get(reverse("my_account")).content
    assert b"convenience fee" in page and b"$103.00" in page
