"""Accounting scenarios under real concurrency (independent QA, M5): payments
for the same lot at the same moment, and payments while the monthly dues
post. Real transactions on separate connections, so these tests commit."""

import datetime
import threading

import pytest
from django.db import connection

from community import services as community
from community.tests.factories import LotFactory, make_member
from core.policy import SYSTEM
from core.scoping import system_access
from ledger import allocation, services
from ledger.models import EntryKind, LedgerEntry, PaymentApplication
from ledger.tests.accounting_invariants import Books

# Committed tests are flushed with TRUNCATE, which the append-only triggers
# refuse: switch them off for this module only (as the reservation
# concurrency tests do) and back on at the end. The invariant helper skips
# its trigger probe here; the runtime role's grants are still checked.
CONCURRENT = pytest.mark.django_db(transaction=True, serialized_rollback=True)
D = datetime.date


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


def _run_together(*calls):
    barrier = threading.Barrier(len(calls))
    errors = []

    def go(fn):
        try:
            barrier.wait()
            fn()
        except Exception as exc:  # reported below
            errors.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=go, args=(fn,)) for fn in calls]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors


def _setup():
    board = make_member(board=True)
    community.set_rule(
        key="dues.monthly_cents", value="4500", citation="Declaration §7.2 (QA)", actor=SYSTEM
    )
    return board


@CONCURRENT
def test_simultaneous_payments_for_one_lot_never_over_apply(app_role_connection):
    board = _setup()
    lot = LotFactory()
    charges = [
        services.post_charge(
            lot=lot,
            kind=EntryKind.OTHER_CHARGE,
            amount_cents=3000,
            description=f"Fee {i}",
            posted_on=D(2026, 3, 1 + i),
            actor=board,
        )
        for i in range(2)
    ]

    def pay(ref, cents):
        return lambda: services.record_payment(
            lot=lot, amount_cents=cents, received_on=D(2026, 3, 9), reference=ref, actor=board
        )

    _run_together(*(pay(f"c{i}", 2000) for i in range(4)))  # 8000 for 6000 of charges
    with system_access("t"):
        applied = sum(
            PaymentApplication.objects.filter(charge__in=charges).values_list(
                "amount_cents", flat=True
            )
        )
    assert applied == 6000
    assert all(allocation.charge_status(c) == "paid" for c in charges)
    assert allocation.unapplied_credit(lot.pk) == 2000 == -services.balance(lot.pk)
    Books().check(app_role_connection=app_role_connection, triggers=False)


@CONCURRENT
def test_payments_recorded_while_the_monthly_dues_post(app_role_connection):
    board = _setup()
    lots = [LotFactory() for _ in range(8)]

    def dues():
        services.post_monthly_assessments(year=2026, month=4, actor=SYSTEM)

    def payments():
        for i, lot in enumerate(lots):
            services.record_payment(
                lot=lot,
                amount_cents=4500,
                received_on=D(2026, 4, 1),
                reference=f"p{i}",
                actor=board,
            )

    _run_together(dues, payments)
    with system_access("t"):
        dues_entries = list(LedgerEntry.objects.filter(kind=EntryKind.ASSESSMENT))
    assert len(dues_entries) == len(lots)
    for entry in dues_entries:
        assert allocation.charge_status(entry) == "paid"
        assert services.balance(entry.lot_id) == 0
        assert allocation.unapplied_credit(entry.lot_id) == 0
    Books().check(app_role_connection=app_role_connection, triggers=False)
