"""Open-item accounting (decision D32-payments).

Every charge on a lot (dues, late fees, other charges, resale fees) has its
own open balance: payments and credits are *applied* to specific charges by
append-only ``PaymentApplication`` rows. The lot's balance is unchanged by
any of this (sum of open charges - unapplied credit == balance-forward
balance); it only answers "which charges are still open".

How money is applied (governing rule ``ledger.assessments_before_fees``):

* By default the oldest charge (by due date) first; with the rule on, dues
  before late fees and other charges, oldest first within each.
* A payment may be directed to chosen charges; whatever is left follows the
  rule.
* Money with nothing to pay stays as credit and is applied automatically
  when the next charge posts (a prepayment draws down month by month, D30).
* Reversing an entry undoes its applications (negative rows) and matches
  the reversal to the entry, so a bounced payment reopens the charges it
  paid, and a reversed charge frees whatever paid it.
* Money is applied only to charges of the same ownership account
  (D33-acctfix-1, ledger/accounts.py): a seller's credit never pays the
  buyer's dues. Readers take ``account=`` (the whole lot by default).

The engine (``LotBook``) works on plain values so the data migration that
rebuilt applications for existing entries uses exactly the same steps as
live posting. Entries are replayed in posting (id) order.
"""

from __future__ import annotations

import contextlib
import contextvars
import datetime
from collections import defaultdict
from dataclasses import dataclass, field

from django.db import connection, transaction
from django.db.models import Max
from django.utils import timezone

from core.scoping import internal, system_access
from ledger.accounts import ALL, account_q, resolve

ORDER_RULE = "ledger.assessments_before_fees"
AUTO, DIRECTED, REVERSAL, UNWIND, MIGRATION = "auto", "directed", "reversal", "unwind", "migration"
CHARGE_KIND_VALUES = ("assessment", "late_fee", "other_charge", "resale_fee", "refund")
_LOCK_NAMESPACE = 5_320_001  # pg advisory lock namespace for lot allocation
_NUMBER_LOCK = 5_320_002

_suspended: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "allocation_suspended", default=False
)


@contextlib.contextmanager
def suspended():
    """Tests only: post entries without applying them (to exercise the
    rebuild of history)."""
    token = _suspended.set(True)
    try:
        yield
    finally:
        _suspended.reset(token)


# --- The engine ------------------------------------------------------------------------


@dataclass
class _Entry:
    id: int
    kind: str
    amount: int
    posted_on: datetime.date
    reverses_id: int | None = None
    account: int | None = None

    @classmethod
    def of(cls, e) -> _Entry:
        # Historical models before migration 0015 have no account.
        account = getattr(e, "account_id", None)
        return cls(e.id, e.kind, e.amount_cents, e.posted_on, e.reverses_id, account)


@dataclass
class LotBook:
    """One lot's entries and applications in memory."""

    assessments_first: bool = False
    entries: dict[int, _Entry] = field(default_factory=dict)
    pairs: dict[tuple[int, int], int] = field(default_factory=lambda: defaultdict(int))
    applied: dict[int, int] = field(default_factory=lambda: defaultdict(int))

    def load_entry(self, e) -> None:
        entry = e if isinstance(e, _Entry) else _Entry.of(e)
        self.entries[entry.id] = entry

    def load_application(self, source_id: int, charge_id: int, cents: int) -> None:
        self.pairs[(source_id, charge_id)] += cents
        self.applied[source_id] += cents
        self.applied[charge_id] += cents

    def remaining(self, entry_id: int) -> int:
        return abs(self.entries[entry_id].amount) - self.applied[entry_id]

    def _apply(self, source_id, charge_id, cents, kind, out) -> None:
        if cents:
            self.load_application(source_id, charge_id, cents)
            out.append((source_id, charge_id, cents, kind))

    def _charge_key(self, e: _Entry):
        if self.assessments_first:
            return (e.kind != "assessment", e.posted_on, e.id)
        return (e.posted_on, e.id)

    def settle(self, kind: str = AUTO, out=None) -> list:
        """Apply every unapplied credit to open charges, by the rule."""
        out = [] if out is None else out
        credits = sorted(
            (e for e in self.entries.values() if e.amount < 0 and self.remaining(e.id) > 0),
            key=lambda e: (e.posted_on, e.id),
        )
        charges = sorted(
            (e for e in self.entries.values() if e.amount > 0 and self.remaining(e.id) > 0),
            key=self._charge_key,
        )
        for credit in credits:
            for charge in charges:
                if charge.account != credit.account:
                    continue  # money stays with its own ownership account
                cents = min(self.remaining(credit.id), self.remaining(charge.id))
                self._apply(credit.id, charge.id, cents, kind, out)
                if not self.remaining(credit.id):
                    break
        return out

    def post(self, e, directed: list[int] | None = None, kind: str | None = None) -> list:
        """Add a newly posted entry; returns the applications to store as
        (source_id, charge_id, cents, kind)."""
        entry = e if isinstance(e, _Entry) else _Entry.of(e)
        self.entries[entry.id] = entry
        out: list = []
        original = self.entries.get(entry.reverses_id) if entry.reverses_id else None
        if original is not None:
            for (s, c), net in list(self.pairs.items()):
                if net and original.id in (s, c):
                    self._apply(s, c, -net, kind or UNWIND, out)
            cents = min(abs(original.amount), abs(entry.amount))
            if original.amount > 0:
                self._apply(entry.id, original.id, cents, kind or REVERSAL, out)
            else:
                self._apply(original.id, entry.id, cents, kind or REVERSAL, out)
        elif directed and entry.amount < 0:
            for charge_id in directed:
                charge = self.entries.get(charge_id)
                if not self.can_direct(entry, charge):
                    continue
                cents = min(self.remaining(entry.id), self.remaining(charge_id))
                if cents > 0:
                    self._apply(entry.id, charge_id, cents, kind or DIRECTED, out)
        self.settle(kind or AUTO, out)
        return out

    @staticmethod
    def can_direct(source: _Entry, charge: _Entry | None) -> bool:
        return (
            charge is not None
            and charge.amount > 0
            and not charge.reverses_id
            and charge.account == source.account
        )

    def separate_accounts(self, kind: str = MIGRATION) -> list:
        """Undo money applied across ownership accounts (history from before
        D33-acctfix-1), then apply it again within each account."""
        out: list = []
        for (s, c), net in list(self.pairs.items()):
            if net > 0 and self.entries[s].account != self.entries[c].account:
                self._apply(s, c, -net, kind, out)
        if out:
            self.settle(kind, out)
        return out

    def open_cents(self) -> int:
        return sum(self.remaining(e.id) for e in self.entries.values() if e.amount > 0) - sum(
            self.remaining(e.id) for e in self.entries.values() if e.amount < 0
        )


def _assessments_first() -> bool:
    from community.rules import rule

    return bool(rule(ORDER_RULE))


# --- Database layer --------------------------------------------------------------------


def _lock(key: int, value: int) -> None:
    with connection.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(%s, %s)", [key, value])


@internal("allocation")
def load_book(lot_id: int, *, exclude: int | None = None) -> LotBook:
    from ledger.models import LedgerEntry, PaymentApplication

    book = LotBook(assessments_first=_assessments_first())
    entries = LedgerEntry.objects.filter(lot_id=lot_id).order_by("id")
    if exclude is not None:
        entries = entries.exclude(pk=exclude)
    for e in entries:
        book.load_entry(e)
    for s, c, cents in PaymentApplication.objects.filter(lot_id=lot_id).values_list(
        "source_id", "charge_id", "amount_cents"
    ):
        book.load_application(s, c, cents)
    return book


@internal("charge number")
def _number(entry) -> None:
    from ledger.models import ChargeNumber

    if entry.amount_cents <= 0 or entry.kind not in CHARGE_KIND_VALUES:
        return
    if ChargeNumber.objects.filter(entry_id=entry.pk).exists():
        return
    _lock(_NUMBER_LOCK, 0)
    year = entry.posted_on.year
    top = ChargeNumber.objects.filter(year=year).aggregate(m=Max("seq"))["m"] or 0
    ChargeNumber.objects.create(entry_id=entry.pk, year=year, seq=top + 1)


@internal("allocation")
def after_post(entry, directed: list[int] | None = None) -> list:
    """Number a new charge and apply money for a newly posted entry."""
    from ledger.models import PaymentApplication

    if _suspended.get():
        return []
    with transaction.atomic():
        _lock(_LOCK_NAMESPACE, entry.lot_id)
        _number(entry)
        book = load_book(entry.lot_id, exclude=entry.pk)
        rows = book.post(entry, directed)
        PaymentApplication.objects.bulk_create(
            [
                PaymentApplication(
                    lot_id=entry.lot_id,
                    source_id=s,
                    charge_id=c,
                    amount_cents=cents,
                    kind=kind,
                    effective_on=entry.posted_on,
                )
                for s, c, cents, kind in rows
            ]
        )
    return rows


def on_entry_saved(sender, instance, created, raw=False, **kwargs) -> None:
    if created and not raw:
        after_post(instance, getattr(instance, "_apply_to", None))


def apply(entry, charge_ids: list[int]) -> list:
    """Apply what is still unapplied of ``entry`` (a payment or credit) to
    the chosen charges, in that order; the rest follows the rule."""
    from ledger.models import PaymentApplication

    with transaction.atomic(), system_access("allocation"):
        _lock(_LOCK_NAMESPACE, entry.lot_id)
        book = load_book(entry.lot_id)
        out: list = []
        for charge_id in charge_ids:
            charge = book.entries.get(charge_id)
            if not book.can_direct(book.entries[entry.pk], charge):
                continue
            cents = min(book.remaining(entry.pk), book.remaining(charge_id))
            if cents > 0:
                book._apply(entry.pk, charge_id, cents, DIRECTED, out)
        book.settle(AUTO, out)
        today = timezone.localdate()
        PaymentApplication.objects.bulk_create(
            [
                PaymentApplication(
                    lot_id=entry.lot_id,
                    source_id=s,
                    charge_id=c,
                    amount_cents=cents,
                    kind=k,
                    effective_on=today,
                )
                for s, c, cents, k in out
            ]
        )
    return out


# --- Rebuilding history (data migration 0009) -------------------------------------------


def backfill(entry_model, application_model, number_model, *, qs=None) -> dict[str, int]:
    """Build applications and charge numbers for entries posted before
    open-item accounting, replaying each lot's history in posting order with
    the default rule. Idempotent: lots that already have applications and
    charges that already have numbers are left alone."""
    qs = qs or (lambda model: model.objects.all())
    done_lots = set(qs(application_model).values_list("lot_id", flat=True).distinct())
    lot_ids = sorted(set(qs(entry_model).values_list("lot_id", flat=True).distinct()))
    # The historical model of migration 0009 has no ``effective_on`` yet
    # (migration 0012 fills it in for those rows).
    dated = "effective_on" in {f.name for f in application_model._meta.get_fields()}
    created = 0
    for lot_id in lot_ids:
        if lot_id in done_lots:
            continue
        book = LotBook()
        rows = []
        for e in qs(entry_model).filter(lot_id=lot_id).order_by("id"):
            rows += [(*row, e.posted_on) for row in book.post(_Entry.of(e), kind=MIGRATION)]
        application_model.objects.bulk_create(
            [
                application_model(
                    lot_id=lot_id,
                    source_id=s,
                    charge_id=c,
                    amount_cents=cents,
                    kind=MIGRATION,
                    **({"effective_on": on} if dated else {}),
                )
                for s, c, cents, _k, on in rows
            ]
        )
        created += len(rows)
    numbered = set(qs(number_model).values_list("entry_id", flat=True))
    tops: dict[int, int] = defaultdict(int)
    for year, seq in qs(number_model).values_list("year", "seq"):
        tops[year] = max(tops[year], seq)
    new_numbers = []
    for e in (
        qs(entry_model)
        .filter(kind__in=CHARGE_KIND_VALUES, amount_cents__gt=0)
        .order_by("posted_on", "id")
    ):
        if e.pk in numbered:
            continue
        tops[e.posted_on.year] += 1
        new_numbers.append(
            number_model(entry_id=e.pk, year=e.posted_on.year, seq=tops[e.posted_on.year])
        )
    number_model.objects.bulk_create(new_numbers)
    return {"applications": created, "numbers": len(new_numbers)}


# --- Reading ---------------------------------------------------------------------------

MONTHS = [datetime.date(2000, m, 1).strftime("%B") for m in range(1, 13)]
DASH = "\N{EN DASH}"


def charge_title(entry) -> str:
    """What owners call a charge: "Dues - March 2026", or its description."""
    period = entry.period or ""
    month = ""
    if len(period) == 7 and period[4] == "-" and period[5:].isdigit():
        month = f"{MONTHS[int(period[5:]) - 1]} {period[:4]}"
    if entry.kind == "assessment" and month:
        return f"Dues {DASH} {month}"
    if entry.kind == "late_fee" and month:
        return f"Late fee {DASH} {month}"
    if entry.kind == "resale_fee":
        return "Resale certificate fee"
    return entry.description


@dataclass
class AppliedPayment:
    entry_id: int
    posted_on: datetime.date
    description: str
    reference: str
    amount_cents: int


@dataclass
class ChargeView:
    entry: object
    number: str
    title: str
    due_on: datetime.date
    original_cents: int
    open_cents: int
    payments: list[AppliedPayment]
    reversed: bool = False

    @property
    def status(self) -> str:
        if self.reversed:
            return "reversed"
        return _status(self.original_cents, self.open_cents)

    @property
    def status_label(self) -> str:
        return {"open": "Open", "partial": "Partly paid", "paid": "Paid", "reversed": "Reversed"}[
            self.status
        ]

    @property
    def label(self) -> str:
        return f"Invoice {self.number}" if self.number else f"Charge #{self.entry.pk}"


def _status(original: int, open_: int) -> str:
    if open_ <= 0:
        return "paid"
    return "open" if open_ >= original else "partial"


@internal("allocation")
def charge_status(entry) -> str:
    """ "open", "partial" or "paid" for a charge entry."""
    book = load_book(entry.lot_id)
    return _status(abs(entry.amount_cents), book.remaining(entry.pk))


@internal("allocation")
def charges(lot_id: int, *, open_only: bool = True, since=None, account=ALL) -> list[ChargeView]:
    """The lot's charges with their open amounts and the payments applied to
    each, oldest first. ``since``: leave out charges posted before (an
    owner's era); their open total is in ``open_before``. ``account``: one
    ownership account's charges only (ledger/accounts.py)."""
    from ledger.models import ChargeNumber, LedgerEntry, PaymentApplication

    book = load_book(lot_id)
    entries = {
        e.pk: e
        for e in LedgerEntry.objects.filter(lot_id=lot_id).filter(account_q(lot_id, account))
    }
    numbers = {n.entry_id: n.label for n in ChargeNumber.objects.filter(entry__lot_id=lot_id)}
    applied: dict[int, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    for s, c, cents in PaymentApplication.objects.filter(lot_id=lot_id).values_list(
        "source_id", "charge_id", "amount_cents"
    ):
        applied[c][s] += cents
    reversed_ids = {e.reverses_id for e in entries.values() if e.reverses_id}
    out = []
    for e in sorted(entries.values(), key=lambda e: (e.posted_on, e.pk)):
        if e.amount_cents <= 0 or e.kind not in CHARGE_KIND_VALUES:
            continue
        open_ = book.remaining(e.pk)
        if open_only and open_ <= 0:
            continue
        if since is not None and e.posted_on < since:
            continue
        pays = []
        for source_id, cents in sorted(applied[e.pk].items()):
            src = entries.get(source_id)
            if not cents or src is None or src.reverses_id:
                continue
            pays.append(
                AppliedPayment(source_id, src.posted_on, src.description, src.reference, cents)
            )
        out.append(
            ChargeView(
                e,
                numbers.get(e.pk, ""),
                charge_title(e),
                e.posted_on,
                e.amount_cents,
                open_,
                pays,
                e.pk in reversed_ids,
            )
        )
    return out


def open_charges(lot_id: int, *, since=None, account=ALL) -> list[ChargeView]:
    return charges(lot_id, open_only=True, since=since, account=account)


@internal("allocation")
def open_before(lot_id: int, since, *, account=ALL) -> int:
    """Open amount of charges posted before ``since`` (an owner sees it as
    one "from before your ownership" line, not the detail)."""
    from ledger.models import LedgerEntry

    book = load_book(lot_id)
    return sum(
        book.remaining(e.pk)
        for e in LedgerEntry.objects.filter(
            lot_id=lot_id, posted_on__lt=since, amount_cents__gt=0, kind__in=CHARGE_KIND_VALUES
        ).filter(account_q(lot_id, account))
    )


@internal("allocation")
def unapplied_credit(lot_id: int, *, account=ALL) -> int:
    book = load_book(lot_id)
    account = resolve(lot_id, account)
    return sum(
        book.remaining(e.id)
        for e in book.entries.values()
        if e.amount < 0 and (account == ALL or e.account == account)
    )


@dataclass
class AsOf:
    """A lot's open items as they stood on a date."""

    entries: dict  # id -> LedgerEntry posted by then
    open: dict[int, int]  # charge id -> open cents
    credit: int  # money not applied to any charge
    balance: int


@internal("allocation")
def state_as_of(lot_id: int, as_of: datetime.date, *, account=ALL) -> AsOf:
    """Open amounts on ``as_of``: only entries posted by then, and only the
    applications made by entries posted by then (``effective_on``), so a
    reversal posted later does not reach back (D33-acctfix-3).

    Backdated entries can make a past state slightly inconsistent (an
    application dated before an earlier one it followed); open amounts are
    kept between zero and the charge, and the credit is what keeps open
    charges - credit equal to the balance on that date."""
    from ledger.models import LedgerEntry, PaymentApplication

    entries = {
        e.pk: e
        for e in LedgerEntry.objects.filter(lot_id=lot_id, posted_on__lte=as_of).filter(
            account_q(lot_id, account)
        )
    }
    applied: dict[int, int] = defaultdict(int)
    for s, c, cents in PaymentApplication.objects.filter(
        lot_id=lot_id, effective_on__lte=as_of
    ).values_list("source_id", "charge_id", "amount_cents"):
        if s in entries and c in entries:
            applied[c] += cents
    open_ = {
        e.pk: min(max(e.amount_cents - applied[e.pk], 0), e.amount_cents)
        for e in entries.values()
        if e.amount_cents > 0
    }
    balance = sum(e.amount_cents for e in entries.values())
    credit = max(sum(open_.values()) - balance, 0)
    return AsOf(entries, open_, credit, balance)


def open_items_as_of(lot_id: int, as_of: datetime.date, *, account=ALL) -> tuple[list[list], int]:
    """[(due_on, open_cents)] of charges open on ``as_of`` and the unapplied
    credit, counting only entries and applications posted by then (aging)."""
    state = state_as_of(lot_id, as_of, account=account)
    charges_ = sorted(
        [state.entries[pk].posted_on, cents] for pk, cents in state.open.items() if cents > 0
    )
    return charges_, state.credit


@internal("allocation")
def applications_of(entry_id: int) -> list[tuple[int, int]]:
    """(charge entry id, cents) this payment or credit is applied to now."""
    from ledger.models import PaymentApplication

    net: dict[int, int] = defaultdict(int)
    for c, cents in PaymentApplication.objects.filter(source_id=entry_id).values_list(
        "charge_id", "amount_cents"
    ):
        net[c] += cents
    return [(c, v) for c, v in sorted(net.items()) if v]
