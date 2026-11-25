"""Two board members confirming overlapping bookings at the same moment:
exactly one wins (real concurrent transactions, so these tests commit)."""

import threading

import pytest
from django.db import connection

from community import reservations
from community.models import Reservation
from community.tests.factories import LotFactory, make_member
from community.tests.test_reservations import _area, _at
from core.models import FeatureSetting

# These tests commit, so Django empties the tables afterwards with TRUNCATE.
# The append-only triggers refuse TRUNCATE; switch them off for this module
# only (the test database's owner may) and back on at the end.
CONCURRENT = pytest.mark.django_db(transaction=True, serialized_rollback=True)


@pytest.fixture(scope="module", autouse=True)
def _allow_flush(django_db_setup, django_db_blocker):
    with django_db_blocker.unblock(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT DISTINCT c.relname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE NOT t.tgisinternal AND n.nspname = current_schema()"
        )
        tables = [row[0] for row in cursor.fetchall()]
        for table in tables:
            cursor.execute(f'ALTER TABLE "{table}" DISABLE TRIGGER USER')  # nosec B608
    yield
    with django_db_blocker.unblock(), connection.cursor() as cursor:
        for table in tables:
            cursor.execute(f'ALTER TABLE "{table}" ENABLE TRIGGER USER')  # nosec B608


@CONCURRENT
def test_concurrent_approvals_confirm_only_one():
    FeatureSetting.objects.create(key="reservations", enabled=True)
    lot_a, lot_b = LotFactory(), LotFactory()
    owner_a, owner_b = make_member(lots=[lot_a]), make_member(lots=[lot_b])
    board_1, board_2 = make_member(board=True), make_member(board=True)
    area = _area()
    first = reservations.request_reservation(
        area=area, lot_id=lot_a.pk, starts_at=_at(3, 10), ends_at=_at(3, 12), actor=owner_a
    )
    second = reservations.request_reservation(
        area=area, lot_id=lot_b.pk, starts_at=_at(3, 11), ends_at=_at(3, 13), actor=owner_b
    )
    barrier = threading.Barrier(2)
    results = []

    def go(res, actor):
        try:
            barrier.wait()
            reservations.approve(res, actor=actor)
            results.append("ok")
        except reservations.ReservationError:
            results.append("refused")
        finally:
            connection.close()

    threads = [
        threading.Thread(target=go, args=(first, board_1)),
        threading.Thread(target=go, args=(second, board_2)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == ["ok", "refused"]
    confirmed = Reservation.objects.unscoped("t").filter(state="confirmed").count()
    assert confirmed == 1


@CONCURRENT
def test_concurrent_instant_bookings_confirm_only_one():
    lot_a, lot_b = LotFactory(), LotFactory()
    owners = [make_member(lots=[lot_a]), make_member(lots=[lot_b])]
    area = _area(needs_approval=False)
    barrier = threading.Barrier(2)
    results = []

    def go(owner, lot):
        try:
            barrier.wait()
            reservations.request_reservation(
                area=area, lot_id=lot.pk, starts_at=_at(3, 10), ends_at=_at(3, 12), actor=owner
            )
            results.append("ok")
        except reservations.ReservationError:
            results.append("refused")
        finally:
            connection.close()

    threads = [
        threading.Thread(target=go, args=(owners[0], lot_a)),
        threading.Thread(target=go, args=(owners[1], lot_b)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == ["ok", "refused"]
