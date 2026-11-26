"""People taking the last place at the same moment: the slot (or the event's
headcount cap) is never overfilled. Real concurrent transactions, so these
tests commit."""

import threading
from datetime import timedelta

import pytest
from django.db import connection
from django.utils import timezone

from comms import rsvp
from comms import services as comms
from comms.models import EventRsvp, VolunteerSignup
from community.tests.factories import LotFactory, make_member

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


def _race(people, fn):
    barrier = threading.Barrier(len(people))
    results = []

    def go(user):
        try:
            barrier.wait()
            fn(user)
            results.append("ok")
        except rsvp.RsvpError:
            results.append("refused")
        finally:
            connection.close()

    threads = [threading.Thread(target=go, args=(u,)) for u in people]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return sorted(results)


def _event():
    board = make_member(board=True)
    event = comms.create_event(
        title="Cleanup day", starts_at=timezone.now() + timedelta(days=3), actor=board
    )
    return board, event


@CONCURRENT
def test_concurrent_sign_ups_never_overfill_a_slot():
    board, event = _event()
    slot = rsvp.add_slot(event, title="Set up tables", capacity=2, actor=board)
    people = [make_member(lots=[LotFactory()]) for _ in range(4)]
    results = _race(people, lambda user: rsvp.sign_up(slot, actor=user))
    assert results == ["ok", "ok", "refused", "refused"]
    assert VolunteerSignup.objects.unscoped("t").filter(slot=slot).count() == 2


@CONCURRENT
def test_concurrent_going_answers_respect_the_cap():
    board, event = _event()
    rsvp.save_settings(event, enabled=True, cap=1, actor=board)
    people = [make_member(lots=[LotFactory()]) for _ in range(3)]
    results = _race(people, lambda user: rsvp.answer(event, answer="going", actor=user))
    assert results == ["ok", "refused", "refused"]
    assert EventRsvp.objects.unscoped("t").filter(event=event, answer="going").count() == 1
