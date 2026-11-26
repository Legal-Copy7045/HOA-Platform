"""Intelligent Mail serial numbers taken at the same moment never repeat (D55).
Real concurrent transactions, so these tests commit."""

import threading

import pytest
from django.db import connection

from comms import papermail
from comms.models import MailPiece
from community import services as community
from community.tests.factories import LotFactory, PersonFactory, days_ago, make_member
from core.policy import SYSTEM

CONCURRENT = pytest.mark.django_db(transaction=True, serialized_rollback=True)


@pytest.fixture(scope="module", autouse=True)
def _allow_flush(django_db_setup, django_db_blocker):
    # Committed tests are emptied with TRUNCATE, which the append-only
    # triggers refuse: switch them off for this module only (as in
    # comms/tests/test_rsvp_concurrency.py).
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
def test_serial_allocation_is_atomic():
    board = make_member(board=True)
    for n in range(4):
        community.record_ownership(
            person=PersonFactory(),
            lot=LotFactory(street_address=f"{n + 1} Race Road\nAnytown, PA 19380"),
            start_date=days_ago(10),
            actor=SYSTEM,
        )
    papermail.save_settings(actor=board, mailer_id="654321", imb_enabled=True)
    mailings = [
        papermail.create_mailing(
            actor=board, defaults=papermail.source_defaults(), subject="Race", body="Hello."
        )
        for _ in range(4)
    ]
    errors = []
    start = threading.Barrier(len(mailings))

    def run(mailing):
        try:
            start.wait()
            papermail.letters_pdf(mailing, duplex=False)
        except Exception as exc:
            errors.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=run, args=(m,)) for m in mailings]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    serials = sorted(MailPiece.objects.unscoped("t").values_list("serial", flat=True))
    assert serials == list(range(1, 17))
