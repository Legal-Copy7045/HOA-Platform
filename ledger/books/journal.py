"""The books as numbers (D69): journal lines with funds, balances by fund and
account, and each bank account's cash book for the month-end reconciliation.

The journal itself is ``ledger.accounting.journal_lines`` (owner ledger,
Stripe, disbursements, vendor payments and coded bank lines). Balances start
from the books' start date (Accounting setup's opening date): each bank
account's opening balance, the owner receivables then, and the fund balance
that makes each fund add up.
"""

from __future__ import annotations

import datetime
from collections import defaultdict
from dataclasses import dataclass

from django.db.models import Sum

from core.scoping import internal, system_access
from ledger.books import funds
from ledger.books.funds import EQUITY, INTERFUND_ACCOUNT, OPENING_EQUITY, account_kind


def _cents(value: int) -> str:
    return f"{value // 100}.{value % 100:02d}" if value >= 0 else f"-{_cents(-value)}"


@dataclass(frozen=True)
class JLine:
    date: datetime.date
    journal: str
    ref: str
    account: str
    debit: int
    credit: int
    memo: str
    lot_number: str
    fund: str
    kind: str  # hint: asset, expense, income, transfer...; else from the number
    pair: int

    def row(self) -> list[str]:
        """The journal export's row (ledger.accounting.EXPORT_HEADER)."""
        return [
            self.date.isoformat(),
            self.journal,
            self.ref,
            self.account,
            _cents(self.debit) if self.debit else "",
            _cents(self.credit) if self.credit else "",
            self.memo,
            self.lot_number,
        ]

    @property
    def account_kind(self) -> str:
        return account_kind(self.account, self.kind)


# --- Where each cash item is ------------------------------------------------------------------


class CashLocator:
    """Which bank account (GL account, fund) holds each cash-book item: the
    account of the bank line it is matched to, else the primary account."""

    def __init__(self):
        from ledger.bankfeeds.matching import active_matches
        from ledger.models import DepositItem, DisbursementEntry, EntryKind, LedgerEntry

        with system_access("books: cash locations"):
            self.matched = {
                (kind, obj): conn
                for kind, obj, conn in active_matches().values_list(
                    "kind", "object_id", "transaction__connection_id"
                )
            }
            totals = defaultdict(int)
            batch_of = {}
            for entry_id, batch_id, cents in DepositItem.objects.values_list(
                "entry_id", "batch_id", "amount_cents"
            ):
                totals[entry_id] += cents
                if cents > 0:
                    batch_of[entry_id] = batch_id
            self.deposit_of = {e: b for e, b in batch_of.items() if totals[e]}
            self.check_of = dict(
                DisbursementEntry.objects.filter(check_record__isnull=False).values_list(
                    "entry_id", "check_record_id"
                )
            )
            self.moved_from = dict(
                LedgerEntry.objects.filter(
                    moved_from__isnull=False, kind__in=[EntryKind.PAYMENT, EntryKind.REFUND]
                ).values_list("pk", "moved_from_id")
            )
        self._accounts: dict = {}

    def _account(self, connection_id):
        if connection_id not in self._accounts:
            self._accounts[connection_id] = funds.cash_account_of(connection_id)
        return self._accounts[connection_id]

    def connection(self, kind: str, object_id: int):
        conn = self.matched.get((kind, object_id))
        if conn is not None and funds.setup_for(conn) is not None:
            return conn
        p = funds.primary()
        return p.connection_id if p else None

    def item(self, kind: str, object_id: int) -> tuple[str, str]:
        return self._account(self.connection(kind, object_id))

    def _root(self, entry_id: int) -> int:
        seen = set()
        while entry_id in self.moved_from and entry_id not in seen:
            seen.add(entry_id)
            entry_id = self.moved_from[entry_id]
        return entry_id

    def entry(self, e) -> tuple[str, str]:
        """An owner payment (by its deposit) or a refund check entry."""
        if e.reverses_id and ("returned", e.pk) in self.matched:
            return self.item("returned", e.pk)
        base = e.reverses_id or e.pk
        for candidate in (base, self._root(base)):
            if candidate in self.check_of:
                return self.item("check", self.check_of[candidate])
            if candidate in self.deposit_of:
                return self.item("deposit", self.deposit_of[candidate])
        return self._account(None)


def cash_locator() -> CashLocator:
    return CashLocator()


# --- Balances by fund and account -------------------------------------------------------------


def journal(start: datetime.date, end: datetime.date) -> list[JLine]:
    from ledger.accounting import journal_lines

    if end < start:
        return []
    return journal_lines(start, end)


def with_interfund(lines: list[JLine]) -> list[JLine]:
    """Each fund balances by itself: a pair whose two sides are in different
    funds (a surplus credit from the reserve fund to owner receivables, a
    vendor paid from the reserve account...) gets a "due to / due from"
    line in each fund."""
    out = list(lines)
    by_pair: dict[int, list[JLine]] = defaultdict(list)
    for line in lines:
        by_pair[line.pair].append(line)
    for pair_lines in by_pair.values():
        if len(pair_lines) != 2 or pair_lines[0].fund == pair_lines[1].fund:
            continue
        debit, credit = sorted(pair_lines, key=lambda x: x.credit)
        cents = debit.debit
        base = debit
        out.append(
            JLine(
                base.date,
                base.journal,
                base.ref,
                INTERFUND_ACCOUNT,
                0,
                cents,
                base.memo,
                base.lot_number,
                debit.fund,
                EQUITY,
                base.pair,
            )
        )
        out.append(
            JLine(
                base.date,
                base.journal,
                base.ref,
                INTERFUND_ACCOUNT,
                cents,
                0,
                base.memo,
                base.lot_number,
                credit.fund,
                EQUITY,
                base.pair,
            )
        )
    return out


@dataclass
class Balances:
    """Net debit (debit minus credit) by (fund, account), and each account's kind."""

    net: dict
    kinds: dict

    def by_kind(self, kind: str, fund: str | None = None) -> dict[str, int]:
        out: dict[str, int] = defaultdict(int)
        for (f, account), cents in self.net.items():
            if self.kinds.get(account) == kind and (fund is None or f == fund):
                out[account] += cents
        return dict(out)

    def total(self, kind: str, fund: str | None = None) -> int:
        return sum(self.by_kind(kind, fund).values())

    def funds(self) -> list[str]:
        present = {f for f, _a in self.net}
        return [f for f in funds.FUNDS if f in present] + sorted(present - set(funds.FUNDS))


def sum_lines(lines: list[JLine]) -> Balances:
    net: dict = defaultdict(int)
    kinds: dict = {}
    for line in with_interfund(lines):
        net[(line.fund, line.account)] += line.debit - line.credit
        kinds.setdefault(line.account, line.account_kind)
    return Balances(dict(net), kinds)


def opening_lines() -> list[JLine]:
    """The books' start: each bank account's opening balance and the owner
    receivables then, against the fund balance at the start."""
    from ledger.models import AccountingSetup, LedgerEntry

    start = funds.books_start()
    if start is None:
        return []
    gl = AccountingSetup.get().accounts()
    out: list[JLine] = []
    n = 0
    setups = funds.accounts()
    if not setups:
        setups_cash = [(gl["cash"], funds.Fund.OPERATING, AccountingSetup.get().opening_cash_cents)]
    else:
        setups_cash = [(s.gl_account, s.fund, s.opening_balance_cents or 0) for s in setups]
    with system_access("books: opening receivables"):
        receivable = (
            LedgerEntry.objects.filter(posted_on__lte=start).aggregate(s=Sum("amount_cents"))["s"]
            or 0
        )
    items = [*setups_cash, (gl["receivable"], funds.Fund.OPERATING, receivable)]
    for account, fund, cents in items:
        if not cents:
            continue
        n -= 1
        debit, credit = (cents, 0) if cents > 0 else (0, -cents)
        out.append(
            JLine(
                start,
                "Opening",
                "OPEN",
                account,
                debit,
                credit,
                "Opening balance",
                "",
                fund,
                "asset",
                n,
            )
        )
        out.append(
            JLine(
                start,
                "Opening",
                "OPEN",
                OPENING_EQUITY,
                credit,
                debit,
                "Opening balance",
                "",
                fund,
                EQUITY,
                n,
            )
        )
    return out


@internal("books: balances_as_of")
def balances_as_of(as_of: datetime.date) -> Balances:
    """The trial balance at the end of ``as_of`` (from the books' start)."""
    start = funds.books_start()
    if start is None or as_of < start:
        return Balances({}, {})
    return sum_lines(opening_lines() + journal(start + datetime.timedelta(days=1), as_of))


@internal("books: activity")
def activity(start: datetime.date, end: datetime.date) -> Balances:
    """What moved between ``start`` and ``end`` (inclusive), within the books."""
    first = funds.books_start()
    if first is None:
        return Balances({}, {})
    start = max(start, first + datetime.timedelta(days=1))
    return sum_lines(journal(start, end))


# --- Each bank account's cash book ------------------------------------------------------------


@dataclass(frozen=True)
class CashItem:
    kind: str  # deposit, payout, payment, returned, check, check_void, gl
    object_id: int
    on: datetime.date
    amount_cents: int  # positive into the account
    connection_id: int | None
    bank_on: datetime.date | None  # the matched (or coded) bank line's date
    pairs_with: str = ""  # a void and what it voids: "deposit:12", "check:4"


@internal("books: cash_items")
def cash_items(through: datetime.date) -> list[CashItem]:
    """Every cash-book item after the books' start through ``through``."""
    from ledger import accounting as acc
    from ledger.bankfeeds.matching import active_matches
    from ledger.books.models import GLEntry
    from ledger.models import (
        BankTransaction,
        CheckEventKind,
        DepositBatch,
        DisbursementCheck,
        DisbursementCheckEvent,
    )
    from operations.models import InvoicePayment

    start = funds.books_start()
    if start is None or through <= start:
        return []
    locate = CashLocator()
    line_dates = dict(
        BankTransaction.objects.filter(
            pk__in=list(active_matches().values_list("transaction_id", flat=True))
        ).values_list("pk", "posted_on")
    )
    match_line = {
        (kind, obj): line
        for kind, obj, line in active_matches().values_list("kind", "object_id", "transaction_id")
    }

    def bank_on(kind, obj):
        line = match_line.get((kind, obj))
        return line_dates.get(line) if line else None

    out: list[CashItem] = []
    for d in DepositBatch.objects.filter(deposited_on__gt=start, deposited_on__lte=through):
        pairs = f"deposit:{d.voids_id}" if d.voids_id else ""
        out.append(
            CashItem(
                "deposit",
                d.pk,
                d.deposited_on,
                d.total_cents,
                locate.connection("deposit", d.voids_id or d.pk),
                bank_on("deposit", d.pk),
                pairs,
            )
        )
    for p in acc._payouts(start, through):
        out.append(
            CashItem(
                "payout",
                p.pk,
                p.arrival_date,
                p.amount_cents,
                locate.connection("payout", p.pk),
                bank_on("payout", p.pk),
            )
        )
    for p in InvoicePayment.objects.filter(paid_on__gt=start, paid_on__lte=through):
        out.append(
            CashItem(
                "payment",
                p.pk,
                p.paid_on,
                -p.amount_cents,
                locate.connection("payment", p.pk),
                bank_on("payment", p.pk),
            )
        )
    for e in acc._returned_items(start, through):
        out.append(
            CashItem(
                "returned",
                e.pk,
                e.posted_on,
                -e.amount_cents,
                locate.connection("returned", e.pk),
                bank_on("returned", e.pk),
            )
        )
    for c in DisbursementCheck.objects.filter(issued_on__gt=start, issued_on__lte=through):
        out.append(
            CashItem(
                "check",
                c.pk,
                c.issued_on,
                -c.amount_cents,
                locate.connection("check", c.pk),
                bank_on("check", c.pk),
            )
        )
    for ev in DisbursementCheckEvent.objects.filter(
        kind=CheckEventKind.VOIDED, on__lte=through, check_record__issued_on__gt=start
    ).select_related("check_record"):
        c = ev.check_record
        out.append(
            CashItem(
                "check_void",
                ev.pk,
                ev.on,
                c.amount_cents,
                locate.connection("check", c.pk),
                None,
                f"check:{c.pk}",
            )
        )
    for g in GLEntry.objects.filter(posted_on__gt=start, posted_on__lte=through).select_related(
        "transaction"
    ):
        out.append(
            CashItem(
                "gl",
                g.pk,
                g.posted_on,
                g.amount_cents,
                g.transaction.connection_id,
                g.transaction.posted_on,
            )
        )
    return out


@dataclass
class AccountRec:
    setup: object
    as_of: datetime.date
    bank_cents: int | None
    bank_source: str
    book_cents: int
    outstanding_in: int
    outstanding_out: int
    bank_ahead: int
    outstanding: list
    unhandled: int

    @property
    def adjusted_bank(self) -> int | None:
        if self.bank_cents is None:
            return None
        return self.bank_cents + self.outstanding_in + self.outstanding_out - self.bank_ahead

    @property
    def difference(self) -> int | None:
        adjusted = self.adjusted_bank
        return None if adjusted is None else adjusted - self.book_cents

    def summary(self) -> dict:
        return {
            "connection_id": self.setup.connection_id,
            "fund": self.setup.fund,
            "as_of": self.as_of.isoformat(),
            "bank_cents": self.bank_cents,
            "bank_source": self.bank_source,
            "book_cents": self.book_cents,
            "outstanding_in": self.outstanding_in,
            "outstanding_out": self.outstanding_out,
            "bank_ahead": self.bank_ahead,
            "difference": self.difference,
            "unhandled": self.unhandled,
        }


def bank_balance(connection, on: datetime.date) -> tuple[int | None, str]:
    """The bank's balance at the end of ``on`` and where it came from."""
    from ledger.bankfeeds.services import feed_balance
    from ledger.models import BankBalance

    cents = feed_balance(connection, on)
    if cents is None:
        return None, ""
    row = (
        BankBalance.objects.filter(connection=connection, as_of__gte=on)
        .order_by("as_of", "-id")
        .first()
    ) or BankBalance.objects.filter(connection=connection, as_of__lt=on).order_by(
        "-as_of", "-id"
    ).first()
    return cents, row.source if row else ""


@internal("books: reconcile_account")
def reconcile_account(setup, as_of: datetime.date, items: list[CashItem] | None = None):
    """One bank account at the end of ``as_of``: the bank's balance, the cash
    book, what is outstanding either way, and the difference."""
    from ledger.books.coding import unhandled_lines

    if items is None:
        from django.utils import timezone

        items = cash_items(max(timezone.localdate(), as_of))
    mine = [i for i in items if i.connection_id == setup.connection_id and i.on <= as_of]
    book = (setup.opening_balance_cents or 0) + sum(i.amount_cents for i in mine)
    voided = {i.pairs_with for i in mine if i.pairs_with}
    outstanding = []
    for i in mine:
        if i.bank_on is not None and i.bank_on <= as_of:
            continue
        key = f"{i.kind}:{i.object_id}"
        if i.pairs_with or (key in voided and i.bank_on is None):
            continue  # a void and the unmatched item it voids cancel out
        outstanding.append(i)
    ahead = [
        i
        for i in items
        if i.connection_id == setup.connection_id
        and i.on > as_of
        and i.bank_on is not None
        and i.bank_on <= as_of
    ]
    bank, source = bank_balance(setup.connection, as_of)
    return AccountRec(
        setup=setup,
        as_of=as_of,
        bank_cents=bank,
        bank_source=source,
        book_cents=book,
        outstanding_in=sum(i.amount_cents for i in outstanding if i.amount_cents > 0),
        outstanding_out=sum(i.amount_cents for i in outstanding if i.amount_cents < 0),
        bank_ahead=sum(i.amount_cents for i in ahead),
        outstanding=outstanding,
        unhandled=len(unhandled_lines(through=as_of, connection_ids={setup.connection_id})),
    )
