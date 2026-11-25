"""Decision D21: late fees can be defined and posted automatically, but
start manual; the board posts from the late list."""

import datetime

import pytest
from django.core.exceptions import PermissionDenied
from django.urls import reverse

from community import services as community
from community.models import Role
from community.tests.factories import LotFactory, make_member
from core.policy import SYSTEM
from ledger import services
from ledger.models import LedgerEntry

pytestmark = pytest.mark.django_db
MONTH = (2026, 10)
DUE = datetime.date(2026, 10, 1)
AFTER_GRACE = datetime.date(2026, 10, 20)


@pytest.fixture
def w():
    board = make_member(board=True)
    late_lot, paid_lot = LotFactory(), LotFactory()
    for key, value in (
        ("dues.monthly_cents", "4500"),
        ("late_fee.cents", "2500"),
        ("late_fee.grace_days", "15"),
    ):
        community.set_rule(key=key, value=value, citation="Board resolution (test)", actor=SYSTEM)
    services.post_monthly_assessments(year=MONTH[0], month=MONTH[1], actor=SYSTEM)
    services.record_payment(lot=paid_lot, amount_cents=4500, received_on=DUE, actor=board)
    return {"board": board, "late": late_lot, "paid": paid_lot}


def _fees(lot):
    return LedgerEntry.objects.unscoped("t").filter(lot=lot, kind="late_fee")


def test_only_unpaid_lots_past_the_grace_period_are_late(w):
    assert services.late_fee_candidates(year=2026, month=10, on=DUE) == []
    late = services.late_fee_candidates(year=2026, month=10, on=AFTER_GRACE)
    assert [r["lot"] for r in late] == [w["late"]]


def test_board_posts_once_per_lot_and_month(w):
    assert services.post_late_fees(year=2026, month=10, actor=w["board"], on=AFTER_GRACE) == 1
    assert services.post_late_fees(year=2026, month=10, actor=w["board"], on=AFTER_GRACE) == 0
    assert _fees(w["late"]).get().amount_cents == 2500
    assert not _fees(w["paid"]).exists()


def test_manager_cannot_charge_late_fees(w):
    with pytest.raises(PermissionDenied):
        services.post_late_fees(
            year=2026, month=10, actor=make_member(roles=[Role.MANAGER]), on=AFTER_GRACE
        )


def test_automatic_posting_is_off_by_default(w):
    assert services.post_late_fees(year=2026, month=10, actor=SYSTEM, on=AFTER_GRACE) == 0
    community.set_rule(
        key="late_fee.auto_apply", value="true", citation="Board resolution (test)", actor=SYSTEM
    )
    assert services.post_late_fees(year=2026, month=10, actor=SYSTEM, on=AFTER_GRACE) == 1


def test_late_list_on_the_owner_accounts_page(client, w):
    client.force_login(w["board"])
    page = client.get(reverse("ledger_home")).content
    assert b"Late this month" in page and b"Automatic posting is <strong>off" in page
