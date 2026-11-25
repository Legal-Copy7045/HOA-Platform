import json

import pytest

from audit.models import AuditEvent
from community.models import Ownership, OwnershipType, today
from community.services import CommunityError, end_ownership, record_ownership, transfer_lot
from community.tests.factories import LotFactory, PersonFactory, UserFactory, days_ago

pytestmark = pytest.mark.django_db


@pytest.fixture
def board_user():
    user = UserFactory()
    PersonFactory(user=user)
    return user


def test_active_means_start_inclusive_end_exclusive():
    ownership = Ownership(start_date=days_ago(10), end_date=today())
    assert ownership.is_active(days_ago(10))
    assert ownership.is_active(days_ago(1))
    assert not ownership.is_active(today())


def test_ending_an_ownership_today_removes_access_immediately(board_user):
    owner, lot = PersonFactory(), LotFactory()
    ownership = record_ownership(person=owner, lot=lot, start_date=days_ago(30), actor=board_user)
    assert Ownership.objects.filter(person=owner).active().exists()

    end_ownership(ownership, actor=board_user)

    assert not Ownership.objects.filter(person=owner).active().exists()
    ownership.refresh_from_db()
    assert ownership.end_date == today()
    event = AuditEvent.objects.get(action="ownership.ended")
    assert event.actor == board_user
    assert event.target_id == str(ownership.pk)


def test_multiple_active_owners_per_lot_are_allowed(board_user):
    lot = LotFactory()
    for _ in range(2):
        record_ownership(
            person=PersonFactory(),
            lot=lot,
            start_date=days_ago(5),
            ownership_type=OwnershipType.JOINT,
            actor=board_user,
        )
    assert lot.ownerships.active().count() == 2


def test_transfer_ends_all_current_owners_and_keeps_history(board_user):
    lot = LotFactory()
    sellers = [PersonFactory(), PersonFactory()]
    for seller in sellers:
        record_ownership(
            person=seller,
            lot=lot,
            start_date=days_ago(400),
            ownership_type=OwnershipType.JOINT,
            actor=board_user,
        )
    buyer = PersonFactory()

    created = transfer_lot(lot, new_owners=[buyer], actor=board_user)

    assert [o.person for o in created] == [buyer]
    assert created[0].ownership_type == OwnershipType.SOLE
    active = list(lot.ownerships.active())
    assert [o.person for o in active] == [buyer]
    # History stays on the lot: the sellers' records still exist, ended today.
    ended = lot.ownerships.filter(person__in=sellers)
    assert ended.count() == 2
    assert all(o.end_date == today() for o in ended)
    assert all(o.history.count() == 2 for o in ended)  # created, then ended
    actions = list(AuditEvent.objects.values_list("action", flat=True))
    assert actions.count("ownership.ended") == 2
    assert actions.count("ownership.transferred") == 1


def test_transfer_validates_new_owners(board_user):
    lot, buyer = LotFactory(), PersonFactory()
    with pytest.raises(CommunityError):
        transfer_lot(lot, new_owners=[], actor=board_user)
    with pytest.raises(CommunityError):
        transfer_lot(lot, new_owners=[buyer, buyer], actor=board_user)


def test_overlapping_ownership_for_same_person_and_lot_is_refused(board_user):
    owner, lot = PersonFactory(), LotFactory()
    record_ownership(person=owner, lot=lot, start_date=days_ago(30), actor=board_user)
    with pytest.raises(CommunityError):
        record_ownership(person=owner, lot=lot, start_date=days_ago(5), actor=board_user)


def test_ending_rules(board_user):
    ownership = record_ownership(
        person=PersonFactory(), lot=LotFactory(), start_date=days_ago(3), actor=board_user
    )
    with pytest.raises(CommunityError):
        end_ownership(ownership, end_date=days_ago(10), actor=board_user)
    end_ownership(ownership, actor=board_user)
    with pytest.raises(CommunityError):
        end_ownership(ownership, actor=board_user)


def test_nobody_records_an_ownership_for_themselves(board_user):
    with pytest.raises(CommunityError):
        record_ownership(
            person=board_user.person, lot=LotFactory(), start_date=days_ago(1), actor=board_user
        )
    assert not AuditEvent.objects.filter(action="ownership.recorded").exists()


def test_audit_metadata_contains_no_names_or_addresses(board_user):
    owner, lot = PersonFactory(), LotFactory()
    transfer_lot(lot, new_owners=[owner], actor=board_user)
    dumped = json.dumps(list(AuditEvent.objects.values("metadata")))
    assert owner.display_name not in dumped
    assert owner.email not in dumped
    assert lot.street_address not in dumped


@pytest.mark.parametrize("make", [LotFactory, PersonFactory])
def test_community_records_are_never_deleted(make):
    with pytest.raises(PermissionError):
        make().delete()
