"""A co-owner removed by a deed change sees on My history only what is from
their own time (D87, finding R2-001): the account stays open for the owner
who remains, so checks written to it after they left are not theirs."""

from unittest import mock

import pytest
from django.urls import reverse

from community import services
from community.models import Account, Ownership, Role
from community.tests.factories import LotFactory, days_ago, make_member
from core import policy
from core.policy import SYSTEM
from core.scoping import system_access
from ledger import disbursements as disb
from ledger import services as ledger

pytestmark = pytest.mark.django_db


def test_removed_co_owner_does_not_see_checks_written_after_they_left(client):
    lot = LotFactory()
    stays = make_member(lots=[lot])
    removed = make_member()
    services.record_ownership(
        person=removed.person, lot=lot, start_date=days_ago(400), actor=SYSTEM
    )
    board, board2 = make_member(board=True), make_member(board=True)
    with system_access("t"):
        account = Account.objects.active().get(lot=lot)
        ownership = Ownership.objects.get(person=removed.person, lot=lot)
    with mock.patch("community.tasks.notify_board.defer"):
        services.end_ownership(ownership, end_date=days_ago(30), actor=SYSTEM)
    assert policy.FORMER_OWNER in policy.roles_for(removed).groups
    assert stays
    ledger.record_payment(
        lot=lot, amount_cents=7000, received_on=days_ago(5), reference="301", actor=board
    )
    manager = make_member(roles=[Role.MANAGER])
    batch = disb.propose_refund(
        account=account, amount_cents=7000, forwarding_address="9 Far Road", actor=manager
    )
    disb.approve(batch, actor=board2)
    (check,) = disb.issue_checks(batch, first_number="7781", issued_on=days_ago(0), actor=board)
    # Written to the account today's owner is on, after the removal.
    assert [c["number"] for c in disb.checks_for_account(account.pk)] == ["7781"]
    assert disb.checks_for_account(account.pk, before=days_ago(30)) == []
    client.force_login(removed)
    page = client.get(reverse("my_history"))
    assert page.status_code == 200
    html = page.content.decode()
    assert "7781" not in html
    assert "9 Far Road" not in html
    assert check.number == "7781"
