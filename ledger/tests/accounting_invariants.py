"""Global accounting invariants, checked after every scenario (QA, M5).

``Books.check()`` asserts, for the whole database:

* every lot, and every ownership account (D33-acctfix-1): open charges -
  unapplied credit == balance-forward balance;
* every charge's open amount is between 0 and its original amount, and no
  payment or credit is applied beyond its own amount (and no pair is net
  negative, and applications join a credit to a charge of the same lot and
  the same ownership account);
* no row of an append-only money table changed or vanished since the last
  check (snapshots), and Postgres refuses UPDATE/DELETE on those tables
  (the trigger for the owner connection; the grants for the runtime role
  when a connection for it is given);
* every online payment in the ledger has its StripePayment and vice versa,
  and, once synced, every Stripe payment line has a ledger payment;
* the journal export balances (overall and per reference), its owner
  receivables equal the sum of balances, and it names no owner.
"""

from __future__ import annotations

import datetime
from collections import defaultdict

from django.db import DatabaseError, connection, transaction

from core.money import parse_cents
from core.scoping import system_access

APPEND_ONLY_TABLES = {
    "ledger.LedgerEntry": "ledger_ledgerentry",
    "ledger.PaymentApplication": "ledger_paymentapplication",
    "ledger.ChargeNumber": "ledger_chargenumber",
    "ledger.DepositBatch": "ledger_depositbatch",
    "ledger.DepositItem": "ledger_deposititem",
    "ledger.ReconciledItem": "ledger_reconcileditem",
    "ledger.BankReconciliation": "ledger_bankreconciliation",
    "ledger.BankTransaction": "ledger_banktransaction",
    "ledger.BankMatch": "ledger_bankmatch",
    "payments.StripePayment": "payments_stripepayment",
    "payments.StripeEvent": "payments_stripeevent",
    "payments.StripeBalanceTransaction": "payments_stripebalancetransaction",
    "payments.StripePayout": "payments_stripepayout",
    "payments.StripePayoutItem": "payments_stripepayoutitem",
    "payments.StripeReconciliation": "payments_stripereconciliation",
}
WIDE = (datetime.date(2000, 1, 1), datetime.date(2100, 12, 31))


def _model(label):
    from django.apps import apps

    return apps.get_model(label)


def _rows(label) -> dict:
    manager = _model(label).objects
    qs = manager.unscoped("qa invariants") if hasattr(manager, "unscoped") else manager.all()
    key = _model(label)._meta.pk.attname
    return {r[key]: r for r in qs.values()}


def journal_totals(rows) -> dict[str, int]:
    """Net debit - credit per account from journal rows."""
    out: dict[str, int] = defaultdict(int)
    for _date, _journal, _ref, account, debit, credit, _memo, *_lot in rows:
        out[account] += (parse_cents(debit) if debit else 0) - (
            parse_cents(credit) if credit else 0
        )
    return out


def journal(start=WIDE[0], end=WIDE[1]):
    from ledger.accounting import journal_rows

    return journal_rows(start, end)


def assert_lot_books():
    from community.models import Lot
    from ledger import allocation, services
    from ledger.models import LedgerEntry, PaymentApplication

    with system_access("qa invariants"):
        lot_ids = list(Lot.objects.values_list("pk", flat=True))
        entries = {e.pk: e for e in LedgerEntry.objects.all()}
        apps_rows = list(PaymentApplication.objects.all())
    for app in apps_rows:
        source, charge = entries[app.source_id], entries[app.charge_id]
        assert source.amount_cents < 0 < charge.amount_cents, f"application #{app.pk} sides"
        assert source.lot_id == charge.lot_id == app.lot_id, f"application #{app.pk} lots"
    pairs: dict[tuple[int, int], int] = defaultdict(int)
    for app in apps_rows:
        pairs[(app.source_id, app.charge_id)] += app.amount_cents
    assert all(v >= 0 for v in pairs.values()), "a payment is net negative on a charge"
    for (source_id, charge_id), net in pairs.items():
        # Rows from before D33-acctfix-1 may cross accounts, undone since.
        assert not net or entries[source_id].account_id == entries[charge_id].account_id, (
            f"entry #{source_id} pays entry #{charge_id} of another account"
        )
    for lot_id in lot_ids:
        book = allocation.load_book(lot_id)
        for entry_id, e in book.entries.items():
            left = book.remaining(entry_id)
            assert 0 <= left <= abs(e.amount), (
                f"lot {lot_id} entry #{entry_id}: remaining {left} of {e.amount}"
            )
        opened = sum(c.open_cents for c in allocation.open_charges(lot_id))
        bal = services.balance(lot_id)
        assert opened - allocation.unapplied_credit(lot_id) == bal, (
            f"lot {lot_id}: open {opened} - credit {allocation.unapplied_credit(lot_id)} != {bal}"
        )
        for c in allocation.charges(lot_id, open_only=False):
            assert 0 <= c.open_cents <= c.original_cents
        accounts = {e.account_id for e in entries.values() if e.lot_id == lot_id}
        for account in accounts:
            opened = sum(c.open_cents for c in allocation.open_charges(lot_id, account=account))
            credit = allocation.unapplied_credit(lot_id, account=account)
            bal = services.balance(lot_id, account=account)
            assert opened - credit == bal, (
                f"lot {lot_id} account {account}: open {opened} - credit {credit} != {bal}"
            )
        assert sum(services.balance(lot_id, account=a) for a in accounts) == services.balance(
            lot_id
        ), f"lot {lot_id}: accounts do not add up to the lot"


def assert_online_payments(*, synced: bool = False):
    from ledger.models import EntryKind, LedgerEntry
    from payments.models import CheckoutState, StripeBalanceTransaction, StripePayment
    from payments.sync import PAYMENT_TYPES

    with system_access("qa invariants"):
        online = {
            e.pk: e
            for e in LedgerEntry.objects.filter(
                kind=EntryKind.PAYMENT, description="Payment (online)"
            )
        }
        payments = list(StripePayment.objects.select_related("checkout", "entry"))
    assert {p.entry_id for p in payments} == set(online), "ledger online payments vs Stripe"
    for p in payments:
        assert -p.entry.amount_cents == p.base_cents == p.checkout.base_cents
        assert p.surcharge_cents == p.checkout.surcharge_cents
        assert p.entry.reference == p.payment_intent_id == p.checkout.payment_intent_id
        assert p.checkout.state == CheckoutState.PAID
        assert p.entry.lot_id == p.lot_id == p.checkout.lot_id
    lines = StripeBalanceTransaction.objects.filter(type__in=PAYMENT_TYPES)
    if synced:
        recorded = {p.charge_id for p in payments}
        assert {b.source_id for b in lines} <= recorded, "Stripe payment without ledger payment"
        assert recorded <= {b.source_id for b in lines}, "ledger payment without Stripe line"


def assert_journal():
    from community.models import Person
    from ledger import services

    rows = journal()
    debits = sum(parse_cents(r[4]) for r in rows if r[4])
    credits = sum(parse_cents(r[5]) for r in rows if r[5])
    assert debits == credits, f"journal debits {debits} != credits {credits}"
    per_ref: dict[str, int] = defaultdict(int)
    for r in rows:
        per_ref[r[2]] += (parse_cents(r[4]) if r[4] else 0) - (parse_cents(r[5]) if r[5] else 0)
    assert not [k for k, v in per_ref.items() if v], "a journal reference does not balance"
    from ledger.models import AccountingSetup

    receivable = AccountingSetup.get().accounts()["receivable"]
    assert journal_totals(rows).get(receivable, 0) == sum(services.balances().values())
    with system_access("qa invariants"):
        people = list(Person.objects.values_list("display_name", "email"))
    text = "\n".join(",".join(r) for r in rows)
    for name, email in people:
        assert name not in text, "journal names an owner"
        assert not email or email not in text, "journal shows an email"


class Books:
    """Tracks append-only tables across checks within one test."""

    def __init__(self):
        self.seen: dict[str, dict] = {}

    def assert_append_only(self, app_role_connection=None, *, triggers=True):
        for label, table in APPEND_ONLY_TABLES.items():
            now = _rows(label)
            pk = _model(label)._meta.pk.column
            for key, row in self.seen.get(label, {}).items():
                assert key in now, f"{label} #{key} was deleted"
                assert now[key] == row, f"{label} #{key} was changed"
            self.seen[label] = now
            if now and triggers:
                for statement in (
                    f'UPDATE "{table}" SET "{pk}" = "{pk}"',  # nosec B608
                    f'DELETE FROM "{table}"',  # nosec B608
                ):
                    try:
                        with transaction.atomic(), connection.cursor() as cur:
                            cur.execute(statement)
                    except DatabaseError as exc:
                        assert "append-only" in str(exc) or "foreign key" in str(exc), exc
                    else:
                        raise AssertionError(f"{statement} was allowed")
            if app_role_connection is not None:
                import psycopg

                for statement in (
                    f'UPDATE "{table}" SET "{pk}" = "{pk}" WHERE false',  # nosec B608
                    f'DELETE FROM "{table}" WHERE false',  # nosec B608
                ):
                    try:
                        app_role_connection.execute(statement)
                    except psycopg.errors.InsufficientPrivilege:
                        continue
                    raise AssertionError(f"runtime role may run {statement}")

    def check(self, *, synced: bool = False, app_role_connection=None, triggers: bool = True):
        """``triggers=False`` only where a module switched the triggers off to
        let committed tests flush (the concurrency tests)."""
        assert_lot_books()
        assert_online_payments(synced=synced)
        assert_journal()
        self.assert_append_only(app_role_connection, triggers=triggers)
