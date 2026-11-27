"""Month-end and year-end close (D70, D71), as automatic as the books allow.

A scheduled task (``ledger.tasks.books_auto_close``, daily) first codes new
bank lines by the board's rules and confirms unambiguous matches, then
checks each open month in order from day ``books.close_day`` of the next
month. When every blocking check passes and ``books.auto_close`` is on, the
month closes by itself (as SYSTEM, audited). Otherwise each problem is an
exception for the treasurer, on My items and the Close page, where each one
can be resolved. Once every month of a fiscal year is closed, the year
closes the same way and the year-end package is built.

A close run's stored results carry ids, counts and amounts only, never a
bank description or a name.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field

from django.conf import settings
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from audit.services import record_event
from community.rules import rule
from community.terms import term_text
from core import features
from core.money import format_cents
from core.policy import SYSTEM, can, require
from core.scoping import internal, system_access
from ledger.books import coding, funds, journal, periods
from ledger.books.funds import BooksError
from ledger.books.models import (
    CategoryKind,
    CloseHow,
    CloseRun,
    ExceptionAgreement,
    ExceptionNote,
    Fund,
    GLEntry,
    PeriodClose,
    PeriodKind,
)


def _user(actor):
    return None if actor is SYSTEM else actor


# --- Results ----------------------------------------------------------------------------------


@dataclass
class Item:
    key: str
    title: str
    detail: str = ""
    amount_cents: int = 0
    obj: object = None  # for the page only (a bank line, an account...)
    note: ExceptionNote | None = None
    accepted: bool = False
    explainable: bool = True  # may be explained with a note instead of fixed
    money: bool = False  # a note about it is a money difference (second board member)

    def as_json(self) -> dict:
        return {"key": self.key, "amount_cents": self.amount_cents, "accepted": self.accepted}


@dataclass
class Check:
    key: str
    title: str
    blocking: bool
    ok_text: str
    items: list[Item] = field(default_factory=list)
    skipped: str = ""
    help: str = ""

    @property
    def open_items(self) -> list[Item]:
        return [i for i in self.items if not i.accepted]

    @property
    def status(self) -> str:
        if self.skipped:
            return "skip"
        if not self.open_items:
            return "pass"
        return "fail" if self.blocking else "warn"

    def as_json(self) -> dict:
        return {
            "key": self.key,
            "status": self.status,
            "open": len(self.open_items),
            "items": [i.as_json() for i in self.items][:200],
        }


@dataclass
class Evaluation:
    kind: str
    period: str
    starts_on: datetime.date
    ends_on: datetime.date
    checks: list[Check]
    close: PeriodClose | None
    accounts: list = field(default_factory=list)  # journal.AccountRec, months only

    @property
    def is_closed(self) -> bool:
        return self.close is not None

    @property
    def ended(self) -> bool:
        return self.ends_on < timezone.localdate()

    @property
    def blocking(self) -> int:
        return sum(len(c.open_items) for c in self.checks if c.blocking and not c.skipped)

    @property
    def warnings(self) -> int:
        return sum(len(c.open_items) for c in self.checks if not c.blocking and not c.skipped)

    @property
    def passed(self) -> bool:
        return self.blocking == 0

    @property
    def closable(self) -> bool:
        return self.passed and self.ended and not self.is_closed

    @property
    def label(self) -> str:
        if self.kind == PeriodKind.MONTH:
            return f"{self.starts_on:%B %Y}"
        return f"Fiscal year {self.period[2:]}"

    def results(self) -> list[dict]:
        return [c.as_json() for c in self.checks]

    def summary(self) -> dict:
        return {
            "checks": self.results(),
            "accounts": [a.summary() for a in self.accounts],
            "blocking": self.blocking,
            "warnings": self.warnings,
        }


# --- Notes on exceptions ------------------------------------------------------------------------


def _notes(period: str) -> dict[tuple[str, str], ExceptionNote]:
    out = {}
    for note in (
        ExceptionNote.objects.filter(period=period).order_by("id").select_related("recorded_by")
    ):
        out[(note.check_key, note.item)] = note  # the latest wins
    return out


def note_accepted(note: ExceptionNote | None) -> bool:
    if note is None:
        return False
    if not note.needs_agreement:
        return True
    return ExceptionAgreement.objects.filter(note=note).exists()


def _explain(check: Check, notes: dict) -> Check:
    for item in check.items:
        note = notes.get((check.key, item.key))
        if note is None or not item.explainable:
            continue
        if item.money and note.amount_cents != item.amount_cents:
            continue  # the difference changed since it was explained
        item.note = note
        item.accepted = note_accepted(note)
    return check


def record_note(
    *,
    period: str,
    check_key: str,
    item: str,
    reason: str,
    amount_cents: int = 0,
    actor,
    request=None,
) -> ExceptionNote:
    """Explain an exception. A money difference, or a note the manager
    records, needs a different board member's agreement to count."""
    reason = (reason or "").strip()
    if len(reason) < 5:
        raise BooksError("Say what the difference is and why it is right.")
    if check_key not in CHECK_KEYS:
        raise BooksError("Choose the exception to explain.")
    if check_key == "late_fees":
        require(actor, "books.waive_late_fee")
        _refuse_own_lot(actor, item, request)
    else:
        require(actor, "books.difference")
    money = amount_cents != 0
    needs = money or not can(actor, "books.difference_agree")
    if check_key == "late_fees":
        needs = False  # a board decision not to charge the fee
    with transaction.atomic():
        note = ExceptionNote.objects.create(
            period=period[:8],
            check_key=check_key,
            item=(item or "")[:40],
            amount_cents=int(amount_cents),
            reason=reason[:2000],
            needs_agreement=needs,
            recorded_by=actor,
        )
    record_event(
        "books.exception_noted",
        actor=actor,
        request=request,
        target=note,
        metadata={
            "period": note.period,
            "check": check_key,
            "item": note.item,
            "amount_cents": note.amount_cents,
            "needs_agreement": needs,
        },
    )
    return note


def agree(note: ExceptionNote, *, actor, request=None) -> ExceptionAgreement:
    """A board member other than the one who noted it agrees."""
    require(actor, "books.difference_agree")
    if note.recorded_by_id == actor.pk:
        raise BooksError("Another board member must agree with a difference you noted.")
    if not note.needs_agreement:
        raise BooksError("This note needs no agreement.")
    with transaction.atomic():
        note = ExceptionNote.objects.select_for_update().get(pk=note.pk)
        if ExceptionAgreement.objects.filter(note=note).exists():
            raise BooksError("Already agreed.")
        agreement = ExceptionAgreement.objects.create(note=note, agreed_by=actor)
    record_event(
        "books.exception_agreed",
        actor=actor,
        request=request,
        target=note,
        metadata={
            "period": note.period,
            "check": note.check_key,
            "amount_cents": note.amount_cents,
        },
    )
    return agreement


def pending_agreements(user=None) -> list[ExceptionNote]:
    qs = ExceptionNote.objects.filter(needs_agreement=True, agreement__isnull=True)
    if user is not None:
        qs = qs.exclude(recorded_by=user)
    closed = periods.standing_closes()
    return [n for n in qs.order_by("recorded_at") if n.period not in closed]


def _refuse_own_lot(actor, item: str, request) -> None:
    from community.models import Lot
    from core import conflicts

    if not item.startswith("lot:"):
        raise BooksError("Choose the home whose late fee is waived.")
    with system_access("books: waiver lot"):
        lot = Lot.objects.filter(pk=int(item[4:] or 0)).first()
    if lot is None:
        raise BooksError("Choose the home whose late fee is waived.")
    conflicts.refuse(
        actor, lot, area="ledger", action="waive_late_fee", request=request, error=BooksError
    )


# --- Statement balance typed by the treasurer -----------------------------------------------


def enter_balance(connection, *, as_of: datetime.date, balance_cents: int, actor, request=None):
    """The bank statement's ending balance, when the feed or file did not
    give it (D70)."""
    from ledger.models import BankBalance, BankTransactionSource

    require(actor, "books.balance")
    if as_of > timezone.localdate():
        raise BooksError("A statement balance cannot be in the future.")
    periods.refuse_closed(as_of, error=BooksError, what="That statement date")
    if funds.setup_for(connection.pk) is None:
        raise BooksError("Put this bank account in the books first.")
    row = BankBalance.objects.create(
        connection=connection,
        as_of=as_of,
        balance_cents=int(balance_cents),
        source=BankTransactionSource.ENTERED,
    )
    record_event(
        "books.balance_entered",
        actor=_user(actor),
        request=request,
        target=connection,
        metadata={"connection_id": connection.pk, "as_of": as_of.isoformat()},
    )
    return row


# --- Matching into a closed month ----------------------------------------------------------------


def refuse_match_into_closed(line, item_on, *, error=ValueError) -> None:
    """A match decides which bank account holds an item: once a month is
    closed, its items stay in the account they were in (D70)."""
    p = funds.primary()
    if p is None or line.connection_id == p.connection_id:
        if periods.is_closed(line.posted_on):
            raise error("This bank line is in a closed month.")
        return
    if periods.is_closed(item_on) or periods.is_closed(line.posted_on):
        raise error(
            "That item is in a closed month: it stays in the primary account. Post a "
            "correction in the open month instead."
        )


# --- The month checks ---------------------------------------------------------------------------

CHECK_KEYS = {
    "setup",
    "previous",
    "bank_lines",
    "bank_balance",
    "reconciliation",
    "assessments",
    "late_fees",
    "undeposited",
    "payouts",
    "transfers",
    "funds",
    "stale_checks",
    "reserve_contribution",
    # year
    "months",
    "budget",
    "statements",
    "reserve_activity",
    "vendor_1099",
    "tax_tests",
}


def _setup_check() -> Check:
    check = Check(
        "setup",
        "The books are set up",
        True,
        "Start date, bank accounts, funds and opening balances are recorded.",
    )
    url = reverse("books_accounts")
    if funds.books_start() is None:
        check.items.append(
            Item(
                "start",
                "Record the books' start date and opening balance",
                "Accounting → Setup: the date the books start from.",
                obj=reverse("accounting") + "?tab=setup",
                explainable=False,
            )
        )
    setups = funds.accounts()
    if not setups:
        check.items.append(
            Item(
                "accounts",
                "Put the association's bank accounts in the books",
                "Each account the association has, with its fund and its balance on the "
                "start date. The accounts come from the bank feed.",
                obj=url,
                explainable=False,
            )
        )
    elif not any(s.is_primary for s in setups):
        check.items.append(
            Item(
                "primary",
                "Choose the primary operating account",
                "Where deposits, Stripe payouts and checks are, unless matched elsewhere.",
                obj=url,
                explainable=False,
            )
        )
    for s in setups:
        if s.opening_balance_cents is None:
            check.items.append(
                Item(
                    f"account:{s.connection_id}",
                    f"{s.connection.label}: opening balance missing",
                    "Its balance on the books' start date.",
                    obj=url,
                    explainable=False,
                )
            )
    from ledger.bankfeeds.services import connections

    in_books = {s.connection_id for s in setups}
    for conn in connections():
        if conn.pk not in in_books:
            check.items.append(
                Item(
                    f"account:{conn.pk}",
                    f"{conn.label} is not in the books",
                    "Choose its fund (operating or reserve).",
                    obj=url,
                    explainable=False,
                )
            )
    return check


def _months_open_before(key: str) -> list[str]:
    first = funds.first_month()
    if first is None:
        return []
    start, _ = periods.month_bounds(key)
    closed = periods.standing_closes()
    return [
        k
        for k in periods.months_between(first, start - datetime.timedelta(days=1))
        if k not in closed
    ]


def _bank_line_check(end: datetime.date) -> Check:
    check = Check(
        "bank_lines",
        "Every bank line is matched or coded",
        True,
        "Each line on every account is matched (deposit, payout, check, vendor payment) or "
        "coded to a category.",
        help="Code the line to a category, or match it on the Bank feed page. A rule codes "
        "lines like it by itself from now on.",
    )
    for line in coding.unhandled_lines(through=end):
        check.items.append(
            Item(
                f"line:{line.pk}",
                f"{line.posted_on:%b %-d} · {line.connection.label}",
                line.description,
                amount_cents=line.amount_cents,
                obj=line,
                explainable=False,
            )
        )
    return check


def _reconciliation_checks(end: datetime.date) -> tuple[Check, Check, list]:
    balance = Check(
        "bank_balance",
        "The bank's balance at month end is known",
        True,
        "Each account's ending balance came from the feed or the statement file.",
        help="Import the month's statement file (OFX/QFX carries the balance), sync the feed, "
        "or type the statement's ending balance.",
    )
    rec = Check(
        "reconciliation",
        "Each account reconciles to the bank",
        True,
        "Bank balance plus items in transit equals the books for every account.",
        help="A difference usually means a missing bank line or a wrong opening balance. A "
        "known difference can be explained; a second board member must agree.",
    )
    recs = []
    items = journal.cash_items(max(timezone.localdate(), end))
    for setup in funds.accounts():
        r = journal.reconcile_account(setup, end, items)
        recs.append(r)
        if r.bank_cents is None:
            balance.items.append(
                Item(
                    f"account:{setup.connection_id}",
                    f"{setup.connection.label}: enter the balance on {end:%B %-d, %Y}",
                    "No balance from the feed or a statement file for that day.",
                    obj=setup,
                    explainable=False,
                )
            )
        elif r.difference:
            rec.items.append(
                Item(
                    f"account:{setup.connection_id}",
                    f"{setup.connection.label}: off by {format_cents(r.difference)}",
                    f"Bank {format_cents(r.bank_cents)}, books {format_cents(r.book_cents)}, "
                    f"in transit {format_cents(r.outstanding_in + r.outstanding_out)}.",
                    amount_cents=r.difference,
                    obj=setup,
                    money=True,
                )
            )
    return balance, rec, recs


def _assessment_check(key: str) -> Check:
    from community.models import Lot
    from ledger.models import EntryKind, LedgerEntry

    check = Check(
        "assessments",
        term_text("The month's {terms} are posted"),
        True,
        term_text("Every home has the month's {term}."),
    )
    if not rule("dues.monthly_cents"):
        check.skipped = term_text("No monthly {term} amount is set.")
        return check
    start, _ = periods.month_bounds(key)
    books = funds.books_start()
    if books is not None and start <= books:
        check.skipped = "Before the books' start."
        return check
    with system_access("books: assessments posted"):
        lots = Lot.objects.count()
        posted = (
            LedgerEntry.objects.filter(kind=EntryKind.ASSESSMENT, period=key)
            .values("lot_id")
            .distinct()
            .count()
        )
    if posted < lots:
        check.items.append(
            Item(
                "",
                term_text(f"{lots - posted} home(s) without the {{term}} for {start:%B %Y}"),
                term_text("Post the month's {terms} on Owner accounts."),
                obj=reverse("ledger_home"),
                explainable=False,
            )
        )
    return check


def _late_fee_check(key: str) -> Check:
    from ledger.services import late_fee_candidates

    check = Check(
        "late_fees",
        "Late fees due under the rules are posted or waived",
        True,
        "No home has an unpaid month past the grace period without its late fee.",
        help="Post the late fees, or waive one with the reason (a board decision).",
    )
    if not rule("late_fee.cents") or not settings.LEDGER_LATE_FEES_ENABLED:
        check.skipped = "No late fee is set."
        return check
    start, end = periods.month_bounds(key)
    for row in late_fee_candidates(year=start.year, month=start.month, on=end):
        check.items.append(
            Item(
                f"lot:{row['lot'].pk}",
                f"{row['lot'].label}: no late fee",
                f"{format_cents(row['balance_cents'])} of the month still open",
                amount_cents=rule("late_fee.cents"),
                obj=row["lot"],
            )
        )
    return check


def _undeposited_check(end: datetime.date) -> Check:
    from ledger.accounting import undeposited_payments

    days = int(rule("books.undeposited_days") or 5)
    check = Check(
        "undeposited",
        f"No payment waits more than {days} days to be deposited",
        True,
        "Every check or cash payment received is in a deposit.",
        help="Record the deposit on the Accounting page.",
    )
    limit = end - datetime.timedelta(days=days)
    start = funds.books_start() or datetime.date(1900, 1, 1)
    for e in undeposited_payments():
        if start < e.posted_on <= limit:
            check.items.append(
                Item(
                    f"entry:{e.pk}",
                    f"{e.posted_on:%b %-d} · {e.lot.label}",
                    e.reference or "Payment",
                    amount_cents=-e.amount_cents,
                    obj=reverse("accounting"),
                )
            )
    return check


def _payout_check(start: datetime.date, end: datetime.date) -> Check:
    from ledger.bankfeeds.matching import active_matches
    from payments.sync import payouts_for_matching

    check = Check(
        "payouts",
        "Stripe payouts arrived and are matched",
        True,
        "Every payout due in the month reached the bank and is matched to its line.",
        help="Match the payout on the Bank feed page. A payout Stripe has not paid yet "
        "appears here until it arrives.",
    )
    rows = [
        p for p in payouts_for_matching(start, end) if p["status"] not in ("failed", "canceled")
    ]
    if not rows:
        check.skipped = "No Stripe payouts this month."
        return check
    matched = set(active_matches().filter(kind="payout").values_list("object_id", flat=True))
    for p in rows:
        if p["id"] in matched:
            continue
        what = "not arrived yet" if p["status"] != "paid" else "not matched to a bank line"
        check.items.append(
            Item(
                f"payout:{p['id']}",
                f"Payout of {p['arrival_date']:%b %-d}: {what}",
                p["payout_id"],
                amount_cents=p["amount_cents"],
                obj=reverse("bank_feed"),
                money=True,
            )
        )
    return check


TRANSFER_DAYS = 5


def _transfer_check(start: datetime.date, end: datetime.date) -> Check:
    check = Check(
        "transfers",
        "Transfers between the association's accounts have both sides",
        True,
        "Each transfer out of one account is matched by the same amount into another.",
        help="Code the other account's line as a transfer too. A transfer still in transit "
        "at month end can be explained.",
    )
    entries = list(
        GLEntry.objects.filter(
            category__kind=CategoryKind.TRANSFER,
            reverses__isnull=True,
            reversed_by__isnull=True,
            posted_on__gte=start - datetime.timedelta(days=TRANSFER_DAYS),
            posted_on__lte=end + datetime.timedelta(days=TRANSFER_DAYS),
        ).select_related("transaction__connection")
    )
    used: set[int] = set()
    for g in sorted(entries, key=lambda x: (x.posted_on, x.pk)):
        if g.pk in used:
            continue
        other = next(
            (
                o
                for o in entries
                if o.pk not in used
                and o.pk != g.pk
                and o.amount_cents == -g.amount_cents
                and o.transaction.connection_id != g.transaction.connection_id
                and abs((o.posted_on - g.posted_on).days) <= TRANSFER_DAYS
            ),
            None,
        )
        if other is not None:
            used |= {g.pk, other.pk}
            continue
        if start <= g.posted_on <= end:
            check.items.append(
                Item(
                    f"gl:{g.pk}",
                    f"{g.posted_on:%b %-d} · {g.transaction.connection.label}",
                    "Transfer with no matching line on another account",
                    amount_cents=g.amount_cents,
                    obj=g,
                    money=True,
                )
            )
    return check


@internal("books: fund_positions")
def fund_positions(as_of: datetime.date) -> dict[str, dict]:
    """Each fund's cash and balance at the end of ``as_of`` (from the books)."""
    b = journal.balances_as_of(as_of)
    cash_accounts = {s.gl_account for s in funds.accounts()}
    out = {}
    for fund in funds.FUNDS:
        assets = b.by_kind(funds.ASSET, fund)
        out[fund] = {
            "cash": sum(v for a, v in assets.items() if a in cash_accounts),
            "balance": b.total(funds.ASSET, fund) + b.total(funds.LIABILITY, fund),
        }
    return out


def books_in_use() -> bool:
    """The books are switched on and set up (a start date)."""
    return features.enabled("books") and funds.books_start() is not None


@internal("books: closed_fund_positions")
def closed_fund_positions() -> tuple[datetime.date, dict[str, dict]] | None:
    """(last day of the last closed month, ``fund_positions`` then), or None
    while the books are not in use or no month is closed. Other areas (the
    cash-flow forecast, the owners' finance summary) read fund balances here."""
    if not books_in_use():
        return None
    key = periods.last_closed_month()
    if key is None:
        return None
    _start, end = periods.month_bounds(key)
    return end, fund_positions(end)


def _fund_check(end: datetime.date) -> Check:
    check = Check(
        "funds",
        "No fund is below zero",
        True,
        "The operating and reserve funds' cash and balances are zero or more.",
        help="A fund below zero means money was spent from the wrong fund or an entry is "
        "missing. If it is right (a board-approved loan between funds), explain it.",
    )
    for fund, pos in fund_positions(end).items():
        label = funds.FUND_LABELS[fund]
        if pos["cash"] < 0:
            check.items.append(
                Item(
                    f"fund:{fund}:cash",
                    f"{label}: cash is {format_cents(pos['cash'])}",
                    amount_cents=pos["cash"],
                    money=True,
                )
            )
        if pos["balance"] < 0:
            check.items.append(
                Item(
                    f"fund:{fund}",
                    f"{label}: balance is {format_cents(pos['balance'])}",
                    amount_cents=pos["balance"],
                    money=True,
                )
            )
    return check


def _stale_check(end: datetime.date) -> Check:
    from ledger import disbursements
    from ledger.bankfeeds.matching import active_matches
    from operations.models import InvoicePayment

    days = int(rule("books.stale_check_days") or 90)
    check = Check(
        "stale_checks",
        f"No check is outstanding more than {days} days",
        False,
        "Every check written has cleared the bank, or is recent.",
        help="Ask the payee; Pennsylvania's unclaimed property law applies to long-uncashed "
        "checks. A warning: it does not stop the close.",
    )
    start = funds.books_start() or datetime.date(1900, 1, 1)
    limit = end - datetime.timedelta(days=days)
    for c in disbursements.outstanding_checks(start, end):
        if c.issued_on <= limit and not c.cleared_on:
            check.items.append(
                Item(
                    f"check:{c.pk}",
                    f"Check #{c.number} of {c.issued_on:%b %-d, %Y}",
                    c.lot_label,
                    amount_cents=-c.amount_cents,
                )
            )
    matched = set(active_matches().filter(kind="payment").values_list("object_id", flat=True))
    for p in InvoicePayment.objects.filter(
        paid_on__gt=start, paid_on__lte=limit, method="check"
    ).select_related("invoice__vendor"):
        if p.pk not in matched:
            check.items.append(
                Item(
                    f"payment:{p.pk}",
                    f"Check to {p.invoice.vendor.name} of {p.paid_on:%b %-d, %Y}",
                    p.reference,
                    amount_cents=-p.amount_cents,
                )
            )
    return check


def monthly_reserve_cents() -> int:
    value = rule("books.reserve_monthly_cents")
    if value:
        return int(value)
    from ledger.models import ReservePlanSettings

    return max(0, ReservePlanSettings.get().annual_contribution_cents or 0) // 12


def _reserve_check(start: datetime.date, end: datetime.date) -> Check:
    from django.db.models import Sum

    check = Check(
        "reserve_contribution",
        "The month's reserve contribution is set aside",
        False,
        "The transfer to the reserve account is at least the budgeted monthly amount.",
        help="Transfer the contribution to the reserve account (Declaration §8.7: the reserve "
        "the board determines). A warning: it does not stop the close.",
    )
    expected = monthly_reserve_cents()
    if not expected or not any(s.fund == Fund.RESERVE for s in funds.accounts()):
        check.skipped = "No reserve account or monthly contribution is set."
        return check
    moved = (
        GLEntry.objects.filter(
            category__kind=CategoryKind.TRANSFER,
            cash_fund=Fund.RESERVE,
            posted_on__range=(start, end),
        ).aggregate(s=Sum("amount_cents"))["s"]
        or 0
    )
    if moved < expected:
        check.items.append(
            Item(
                "",
                f"{format_cents(moved)} set aside of {format_cents(expected)}",
                amount_cents=expected - moved,
            )
        )
    return check


@internal("books: evaluate_month")
def evaluate_month(key: str) -> Evaluation:
    start, end = periods.month_bounds(key)
    notes = _notes(key)
    standing = periods.standing_closes()
    previous = Check(
        "previous",
        "Earlier months are closed",
        True,
        "Months close in order.",
    )
    for k in _months_open_before(key):
        s, _e = periods.month_bounds(k)
        previous.items.append(Item(k, f"{s:%B %Y} is not closed", obj=k, explainable=False))
    balance, rec, recs = _reconciliation_checks(end)
    checks = [
        _setup_check(),
        previous,
        _bank_line_check(end),
        balance,
        rec,
        _assessment_check(key),
        _late_fee_check(key),
        _undeposited_check(end),
        _payout_check(start, end),
        _transfer_check(start, end),
        _fund_check(end),
        _stale_check(end),
        _reserve_check(start, end),
    ]
    checks = [_explain(c, notes) for c in checks]
    return Evaluation(PeriodKind.MONTH, key, start, end, checks, standing.get(key), recs)


# --- The year checks ----------------------------------------------------------------------------


@internal("books: evaluate_year")
def evaluate_year(fiscal_year: int) -> Evaluation:
    from ledger.books import statements
    from ledger.models import Budget, BudgetStatus

    key = periods.year_key(fiscal_year)
    start, end = periods.year_bounds(fiscal_year)
    notes = _notes(key)
    months = Check("months", "Every month of the year is closed", True, "All twelve months closed.")
    closed = periods.standing_closes()
    first = funds.first_month()
    for k in periods.year_months(fiscal_year):
        s, e = periods.month_bounds(k)
        if first is None or e < first:
            continue
        if k not in closed:
            months.items.append(Item(k, f"{s:%B %Y} is not closed", obj=k, explainable=False))
    budget = Check(
        "budget",
        "An adopted budget to compare with",
        False,
        "Budget against actual is computed for the year.",
        help="Without an adopted budget the statements show actual figures only.",
    )
    if not Budget.objects.filter(fiscal_year=fiscal_year, status=BudgetStatus.ADOPTED).exists():
        budget.items.append(Item("", f"No adopted budget for {fiscal_year}"))
    sheet = Check(
        "statements",
        "The statements balance",
        True,
        "Assets less liabilities equal each fund's balance.",
    )
    reserve = Check(
        "reserve_activity",
        "Reserve activity reconciles",
        True,
        "The reserve fund's cash in the books equals its bank accounts' reconciled balances.",
    )
    if first is not None and end >= first:
        bs = statements.balance_sheet(end)
        for fund, f in bs["funds"].items():
            if f["assets"] - f["liabilities"] != f["fund_balance"]:
                sheet.items.append(
                    Item(
                        f"fund:{fund}",
                        f"{funds.FUND_LABELS.get(fund, fund)} does not balance",
                        amount_cents=f["assets"] - f["liabilities"] - f["fund_balance"],
                        explainable=False,
                    )
                )
        positions = fund_positions(end)
        items = journal.cash_items(max(timezone.localdate(), end))
        reserve_book = sum(
            journal.reconcile_account(s, end, items).book_cents
            for s in funds.accounts()
            if s.fund == Fund.RESERVE
        )
        if reserve_book != positions[Fund.RESERVE]["cash"]:
            reserve.items.append(
                Item(
                    "fund:reserve",
                    "Reserve cash in the books differs from its accounts",
                    amount_cents=positions[Fund.RESERVE]["cash"] - reserve_book,
                    money=True,
                )
            )
    vendors = Check(
        "vendor_1099",
        "1099 totals are ready",
        True,
        "Vendors paid at or over the 1099 threshold have a W-9 on file.",
        help="Get the vendor's W-9 and tick W-9 on file on the vendor, or explain (for "
        "example a corporation that needs no 1099).",
    )
    from ledger.books import tax as tax_docs

    for row in tax_docs.vendor_1099s(end.year):
        if not row["w9"]:
            vendors.items.append(
                Item(
                    f"vendor:{row['vendor'].pk}",
                    f"{row['vendor'].name}: W-9 missing",
                    amount_cents=row["total_cents"],
                )
            )
    from ledger.books import tax

    tax_check = Check(
        "tax_tests",
        "The association qualifies to file Form 1120-H",
        True,
        "At least 60% of gross income is exempt-function income and at least 90% of spending "
        "is on association property (IRC § 528).",
        help="A failed test means Form 1120 may be needed instead of Form 1120-H: ask the tax "
        "preparer, then explain with their advice.",
    )
    if first is not None and end >= first:
        w = tax.form_1120h(fiscal_year)
        if not w["sixty"]["passes"]:
            tax_check.items.append(
                Item(
                    "sixty",
                    f"60% test fails ({w['sixty']['percent']}% exempt-function "
                    "income): Form 1120 may be needed",
                )
            )
        if not w["ninety"]["passes"]:
            tax_check.items.append(
                Item(
                    "ninety",
                    f"90% test fails ({w['ninety']['percent']}% on association "
                    "property): Form 1120 may be needed",
                )
            )
    checks = [_explain(c, notes) for c in (months, sheet, reserve, budget, vendors, tax_check)]
    return Evaluation(
        PeriodKind.YEAR, key, start, end, checks, periods.standing_closes(PeriodKind.YEAR).get(key)
    )


# --- Closing ------------------------------------------------------------------------------------


def close_month(key: str, *, actor, how=CloseHow.MANUAL, request=None) -> PeriodClose:
    if actor is not SYSTEM:
        require(actor, "books.close")
    ev = evaluate_month(key)
    if not ev.ended:
        raise BooksError("A month can close only after it has ended.")
    if not ev.passed:
        raise BooksError(f"{ev.blocking} exception(s) must be resolved before {ev.label} closes.")
    return periods.close_period(
        kind=PeriodKind.MONTH, key=key, summary=ev.summary(), how=how, actor=actor, request=request
    )


def close_year(fiscal_year: int, *, actor, how=CloseHow.MANUAL, request=None) -> PeriodClose:
    if actor is not SYSTEM:
        require(actor, "books.close")
    ev = evaluate_year(fiscal_year)
    if not ev.ended:
        raise BooksError("A year can close only after it has ended.")
    if not ev.passed:
        raise BooksError(f"{ev.blocking} exception(s) must be resolved before the year closes.")
    from ledger.books import statements

    summary = ev.summary()
    summary["annual_accounting"] = statements.annual_accounting(fiscal_year)
    close = periods.close_period(
        kind=PeriodKind.YEAR, key=ev.period, summary=summary, how=how, actor=actor, request=request
    )
    from ledger.books import package, tax

    tax.ensure_deadlines(actor=SYSTEM, fiscal_year=fiscal_year)
    transaction.on_commit(
        lambda: package.build(
            fiscal_year, actor=SYSTEM if how == CloseHow.AUTO else actor, automatic=True
        )
    )
    return close


def _record_run(ev: Evaluation, closed: bool) -> CloseRun | None:
    results = ev.results()
    latest = CloseRun.objects.filter(kind=ev.kind, period=ev.period).first()
    if latest and latest.results == results and latest.closed == closed:
        return latest
    return CloseRun.objects.create(
        kind=ev.kind,
        period=ev.period,
        passed=ev.passed,
        closed=closed,
        blocking=ev.blocking,
        warnings=ev.warnings,
        results=results,
    )


def due_months(today: datetime.date | None = None) -> list[str]:
    """Open months the automatic close may look at today: ended, from the
    books' first month, and past ``books.close_day`` of the next month."""
    today = today or timezone.localdate()
    first = funds.first_month()
    if first is None:
        return []
    day = max(1, min(int(rule("books.close_day") or 3), 28))
    closed = periods.standing_closes()
    out = []
    for key in periods.months_between(first, today):
        start, end = periods.month_bounds(key)
        opens = periods.next_month(start) + datetime.timedelta(days=day - 1)
        if end < today and today >= opens and key not in closed:
            out.append(key)
    return out


@internal("books: run")
def run(today: datetime.date | None = None) -> dict:
    """The daily automatic close (D70, D71)."""
    if not features.enabled("books"):
        return {"coded": 0, "matched": 0, "closed": [], "waiting": []}
    coded = coding.apply_rules()
    matched = coding.auto_match()
    closed, waiting = [], []
    for key in due_months(today):
        ev = evaluate_month(key)
        do_close = ev.closable and bool(rule("books.auto_close"))
        if do_close:
            try:
                close_month(key, actor=SYSTEM, how=CloseHow.AUTO)
            except (BooksError, periods.PeriodError):
                do_close = False
        _record_run(ev, do_close)
        if not do_close:
            waiting.append(key)
            break
        closed.append(key)
    # Fiscal years whose months are all closed.
    first = funds.first_month()
    if first is not None:
        today = today or timezone.localdate()
        year_closes = periods.standing_closes(PeriodKind.YEAR)
        for fy in range(periods.fiscal_year_of(first), periods.fiscal_year_of(today) + 1):
            key = periods.year_key(fy)
            _start, end = periods.year_bounds(fy)
            if key in year_closes or end >= today:
                continue
            ev = evaluate_year(fy)
            if ev.checks[0].open_items:
                continue  # months still open: nothing to say about the year yet
            do_close = ev.closable and bool(rule("books.auto_close_year"))
            if do_close:
                try:
                    close_year(fy, actor=SYSTEM, how=CloseHow.AUTO)
                except (BooksError, periods.PeriodError):
                    do_close = False
            _record_run(ev, do_close)
            (closed if do_close else waiting).append(key)
    if coded or matched or closed:
        record_event(
            "books.auto_close_ran",
            actor=None,
            metadata={"coded": coded, "matched": matched, "closed": len(closed)},
        )
    return {"coded": coded, "matched": matched, "closed": closed, "waiting": waiting}


# --- My items -----------------------------------------------------------------------------------


def open_runs() -> list[CloseRun]:
    """The latest close run of every period still open whose checks failed."""
    standing = {
        PeriodKind.MONTH: periods.standing_closes(),
        PeriodKind.YEAR: periods.standing_closes(PeriodKind.YEAR),
    }
    out, seen = [], set()
    for run_ in CloseRun.objects.order_by("-ran_at", "-id")[:200]:
        if (run_.kind, run_.period) in seen:
            continue
        seen.add((run_.kind, run_.period))
        if not run_.passed and run_.period not in standing[run_.kind]:
            out.append(run_)
    return sorted(out, key=lambda r: r.period)


def run_label(run_: CloseRun) -> str:
    if run_.kind == PeriodKind.MONTH:
        start, _e = periods.month_bounds(run_.period)
        return f"{start:%B %Y}"
    return f"Fiscal year {run_.period[2:]}"


def open_exception_lines() -> list[str]:
    """Plain lines for the handover sheet."""
    out = [
        f"{run_label(r)}: {r.blocking} exception(s) and {r.warnings} warning(s), found "
        f"{timezone.localtime(r.ran_at):%B %-d, %Y}"
        for r in open_runs()
    ]
    out += [
        f"{n.period}: a note of {format_cents(n.amount_cents)} waits for a second board member"
        for n in pending_agreements()
    ]
    out += [
        f"{r.close.period}: a reopen request waits for a decision"
        for r in periods.pending_reopens()
    ]
    return out


def board_items(user) -> list:
    """What waits on the treasurer and the board (core.my_items)."""
    from core.my_items import Item as MyItem

    if not features.enabled("books"):
        return []
    out = []
    url = reverse("books_close")
    for run_ in open_runs():
        out.append(
            MyItem(
                "Finance",
                f"{run_label(run_)} books: {run_.blocking} exception"
                f"{'' if run_.blocking == 1 else 's'} to resolve",
                "The automatic close found these; everything else balanced.",
                f"{url}?period={run_.period}",
                "warning",
                nav="books_close",
                action_label="Resolve",
                action_url=f"{url}?period={run_.period}",
            )
        )
    if can(user, "books.difference_agree"):
        for note in pending_agreements(user):
            out.append(
                MyItem(
                    "Finance",
                    f"Agree a books difference of {format_cents(note.amount_cents)}"
                    if note.amount_cents
                    else "Agree a note on the books",
                    f"{note.period}: noted by another board member, needs a second board member.",
                    f"{url}?period={note.period}",
                    "warning",
                    nav="books_close",
                )
            )
    if can(user, "books.reopen_approve"):
        for req in periods.pending_reopens():
            if req.requested_by_id == user.pk:
                continue
            out.append(
                MyItem(
                    "Finance",
                    f"Approve or decline reopening {req.close.period}",
                    "Another board member asked to reopen a closed period.",
                    f"{url}?period={req.close.period}",
                    "warning",
                    nav="books_close",
                )
            )
    return out
