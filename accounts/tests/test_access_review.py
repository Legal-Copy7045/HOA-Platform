"""Quarterly access review (Milestone 4)."""

import datetime

import pytest
from django.core.exceptions import PermissionDenied
from django.urls import reverse
from django.utils import timezone

from accounts import access_review
from accounts.models import AccessReview, User
from audit.models import AuditEvent
from community import services
from community.models import Account, Office, Ownership, Role
from community.tests.factories import LotFactory, PersonFactory, days_ago, make_member
from core.append_only import AppendOnlyError
from core.policy import SYSTEM

pytestmark = pytest.mark.django_db


def _row(rows, user):
    return next(r for r in rows if r.user.pk == user.pk)


def test_rows_show_access_and_flags():
    lot = LotFactory()
    owner = make_member(lots=[lot])
    User.objects.filter(pk=owner.pk).update(last_login=timezone.now())
    board = make_member(board=True)
    services.record_office(
        person=board.person, office=Office.TREASURER, start_date=days_ago(5), actor=SYSTEM
    )
    User.objects.filter(pk=board.pk).update(
        last_login=timezone.now() - datetime.timedelta(days=120)
    )
    admin = make_member(roles=[Role.SYSADMIN], mfa=False)
    manager = make_member(roles=[Role.MANAGER])
    seller = make_member(lots=[LotFactory()])
    ownership = Ownership.objects.unscoped("t").get(person=seller.person)
    services.end_ownership(ownership, end_date=days_ago(1), actor=SYSTEM)
    rows = access_review.rows()

    r = _row(rows, owner)
    assert r.lots == [lot.number] and r.mfa and r.flags == []
    r = _row(rows, board)
    assert r.board and r.offices == ["Treasurer"] and "inactive" in r.flags
    r = _row(rows, admin)
    assert {"sysadmin", "no_mfa", "never"} <= set(r.flags)
    assert "manager" in _row(rows, manager).flags
    assert "former_owner" in _row(rows, seller).flags
    counts = access_review.summary(rows)
    assert counts["logins"] == 5 and counts["sysadmins"] == 1 and counts["without_mfa"] == 1


def test_contact_on_a_dormant_account_is_flagged():
    lot = LotFactory()
    owner = PersonFactory()
    services.record_ownership(person=owner, lot=lot, start_date=days_ago(400), actor=SYSTEM)
    account = Account.objects.unscoped("t").get(lot=lot, closed_on__isnull=True)
    contact = make_member()
    services.add_account_contact(
        account, person=contact.person, kind="authorized", start_date=days_ago(300), actor=SYSTEM
    )
    assert "dormant_contact" not in _row(access_review.rows(), contact).flags
    Account.objects.unscoped("t").filter(pk=account.pk).update(closed_on=days_ago(1))
    assert "dormant_contact" in _row(access_review.rows(), contact).flags


def test_board_records_a_review_append_only_and_audited(client):
    board, board2 = make_member(board=True), make_member(board=True)
    make_member(lots=[LotFactory()])
    assert access_review.is_due()
    client.force_login(board)
    page = client.get(reverse("access_review"))
    assert page.status_code == 200
    assert AuditEvent.objects.filter(action="access_review.viewed", actor=board).exists()
    client.post(reverse("access_review_record"))
    review = AccessReview.objects.get()
    assert review.reviewed_by == board and review.logins == 3
    event = AuditEvent.objects.get(action="access_review.recorded")
    assert event.metadata["logins"] == 3
    assert not access_review.is_due()
    with pytest.raises(AppendOnlyError):
        review.save()
    with pytest.raises(AppendOnlyError):
        AccessReview.objects.all().delete()
    # A second board member reviews too: shown as "also reviewed".
    client.force_login(board2)
    client.post(reverse("access_review_record"))
    client.force_login(board)
    page = client.get(reverse("access_review")).content.decode()
    assert "Also reviewed by" in page and board.person.display_name in page


def test_only_the_board_can_see_or_record_it(client):
    for user in (
        make_member(lots=[LotFactory()]),
        make_member(roles=[Role.SYSADMIN]),
        make_member(roles=[Role.MANAGER]),
        make_member(roles=[Role.AUDITOR]),
    ):
        client.force_login(user)
        assert client.get(reverse("access_review")).status_code == 403
        assert client.post(reverse("access_review_record")).status_code == 403
        with pytest.raises(PermissionDenied):
            access_review.record_review(actor=user)
    assert not AccessReview.objects.exists()


def test_my_items_reminds_the_board_when_due():
    from core.my_items import items_for

    board = make_member(board=True)
    manager = make_member(roles=[Role.MANAGER])

    def titles(user):
        return [i.title for i in items_for(user)]

    assert "Quarterly access review" in titles(board)
    assert "Quarterly access review" not in titles(manager)
    access_review.record_review(actor=board)
    assert "Quarterly access review" not in titles(board)
    AccessReview.objects.create(
        reviewed_by=board, reviewed_at=timezone.now() - datetime.timedelta(days=200)
    )
    assert "Quarterly access review" not in titles(board)  # the latest one counts
    assert not access_review.is_due(timezone.now() + datetime.timedelta(days=60))
    assert access_review.is_due(timezone.now() + datetime.timedelta(days=91))
