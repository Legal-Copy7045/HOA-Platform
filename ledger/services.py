from __future__ import annotations

import contextlib
import contextvars
import datetime
import re
from dataclasses import dataclass

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Count, Sum
from django.urls import reverse
from django.utils import timezone

from audit.services import record_event
from community.models import CommunityProfile, Lot
from community.rules import rule, rule_with_citation
from community.terms import charge_term, term_text
from core import conflicts, pdf
from core.money import format_cents
from core.policy import SYSTEM, require
from core.scoping import internal, system_access
from core.workflow import Transition, Workflow
from ledger import accounts
from ledger.accounts import ALL, CURRENT, MOVED_AWAY, account_q
from ledger.models import (
    Budget,
    BudgetLine,
    BudgetStatus,
    CollectionCase,
    EntryKind,
    IncomeSource,
    LedgerEntry,
    LetterKind,
    LineKind,
    ResaleRequest,
)


class LedgerError(ValueError):
    pass


def _user(actor):
    return None if actor is SYSTEM else actor


def _allocation():
    from ledger import allocation

    return allocation


def _authorize(actor, action):
    if actor is not SYSTEM:
        require(actor, action)


def _refuse_own(actor, matter, action: str, request=None) -> None:
    """Own matters (D65): no staff entry on one's own home's account."""
    conflicts.refuse(
        actor, matter, area="ledger", action=action, request=request, error=LedgerError
    )


# --- Balances -------------------------------------------------------------------------


@internal("ledger service")
def balance(lot_id: int, as_of: datetime.date | None = None, *, account=ALL) -> int:
    """The lot's balance: the sum over its ownership accounts, or one
    account's (``account``: an id, or CURRENT for today's owners)."""
    qs = LedgerEntry.objects.filter(lot_id=lot_id).filter(account_q(lot_id, account))
    if as_of:
        qs = qs.filter(posted_on__lte=as_of)
    return qs.aggregate(s=Sum("amount_cents"))["s"] or 0


@internal("ledger service")
def account_balances() -> dict[int, int]:
    """Balance of every ownership account that has entries."""
    return dict(
        LedgerEntry.objects.filter(account__isnull=False)
        .values_list("account_id")
        .annotate(s=Sum("amount_cents"))
        .values_list("account_id", "s")
    )


def past_due(lot_id: int, today: datetime.date | None = None, *, account=ALL) -> int:
    """What is actually overdue on ``today``: the open amounts of charges
    due before that day (open-item accounting, as the charges stood then).
    A charge due today or later, such as dues posted ahead for a later due
    date, does not count yet (BUG-006). Every delinquency notice and letter
    states this amount, never the whole balance (D33-acctfix-4)."""
    today = today or timezone.localdate()
    state = _allocation().state_as_of(lot_id, today, account=account)
    return sum(
        cents
        for pk, cents in state.open.items()
        if cents > 0 and state.entries[pk].posted_on < today
    )


@internal("ledger service")
def balances() -> dict[int, int]:
    return dict(
        LedgerEntry.objects.values_list("lot_id")
        .annotate(s=Sum("amount_cents"))
        .values_list("lot_id", "s")
    )


@dataclass
class OwnerStatement:
    brought_forward: int
    entries: list
    balance: int


@internal("ledger service")
def owner_statement(lot_id: int, since: datetime.date | None, *, account=CURRENT) -> OwnerStatement:
    """What a current owner sees: their ownership account (today's, by
    default; D33-acctfix-1), entries from their ownership start, with any
    earlier balance on that account shown as one 'brought forward' line (not
    the detail). Another account's money (a former owner's) is never in it,
    nor an entry moved to another account with its reversal (D34-fixmoney-1)."""
    qs = (
        LedgerEntry.objects.filter(lot_id=lot_id)
        .filter(account_q(lot_id, account))
        .exclude(MOVED_AWAY)
    )
    since = since or datetime.date(1900, 1, 1)
    forward = qs.filter(posted_on__lt=since).aggregate(s=Sum("amount_cents"))["s"] or 0
    entries = list(qs.filter(posted_on__gte=since))
    return OwnerStatement(forward, entries, forward + sum(e.amount_cents for e in entries))


@internal("ledger service")
def period_statement(
    lot_id: int,
    start: datetime.date,
    end: datetime.date | None,
    *,
    account=ALL,
    include_after_end: bool = False,
) -> OwnerStatement:
    """What a former owner sees: the entries of their ownership account
    posted while they owned the lot, with the balance brought forward from
    before their time and the balance on the day they sold. Nothing from
    after the sale, except, with ``include_after_end`` (their account went
    dormant with them, so nobody else is on it), later entries on that
    account, such as the board refunding a credit. An entry moved to the
    buyer's account (a sale recorded later with an earlier date) and its
    reversal are left out: they were never this owner's (D34-fixmoney-1)."""
    qs = (
        LedgerEntry.objects.filter(lot_id=lot_id)
        .filter(account_q(lot_id, account))
        .exclude(MOVED_AWAY)
    )
    forward = qs.filter(posted_on__lt=start).aggregate(s=Sum("amount_cents"))["s"] or 0
    entries = qs.filter(posted_on__gte=start)
    if end is not None and not include_after_end:
        entries = entries.filter(posted_on__lt=end)
    entries = list(entries)
    return OwnerStatement(forward, entries, forward + sum(e.amount_cents for e in entries))


# --- Posting ---------------------------------------------------------------------------


@dataclass
class UpcomingCharge:
    due_on: datetime.date
    description: str
    amount_cents: int
    covered_by_credit: bool


@internal("ledger service")
def upcoming_charges(
    balance_cents: int, *, lot_id: int | None = None, months: int = 3, on=None
) -> list[UpcomingCharge]:
    """The next monthly dues, from the dues rule. A credit on the account is
    shown being used up month by month (nothing here posts anything).

    A month whose assessment is already posted (posted ahead of time) is in
    the balance already, so it is not listed again (review BUG-005). Prepaid
    credit is drawn down only as each month posts (owner ruling 2026-11-26)."""
    amount = rule("dues.monthly_cents") or 0
    if not amount:
        return []
    on = on or timezone.localdate()
    day = min(rule("dues.due_day") or 1, 28)
    credit = -balance_cents if balance_cents < 0 else 0
    year, month = on.year, on.month
    if on.day >= day:
        month += 1
    if month > 12:
        year, month = year + 1, 1
    posted: set[str] = set()
    if lot_id is not None:
        posted = set(
            LedgerEntry.objects.filter(
                lot_id=lot_id,
                kind=EntryKind.ASSESSMENT,
                period__gte=_period(year, month),
                reversed_by__isnull=True,
            ).values_list("period", flat=True)
        )
    term = charge_term()
    out = []
    while len(out) < months:
        if month > 12:
            year, month = year + 1, 1
        if _period(year, month) not in posted:
            covered = credit >= amount
            credit = max(credit - amount, 0)
            out.append(
                UpcomingCharge(
                    datetime.date(year, month, day),
                    term.monthly_title(datetime.date(year, month, 1)),
                    amount,
                    covered,
                )
            )
        month += 1
    return out


def _period(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"


def assessment_months(today: datetime.date, *, back: int = 3, ahead: int = 3) -> list[dict]:
    """Months to offer when posting the monthly charge (D48, replaces the
    browser's month grid): ``back`` months before this one to ``ahead`` after,
    each with how many homes already have it posted. Posting again stays safe:
    homes already charged for a month are skipped."""
    first = today.replace(day=1)
    months = []
    for offset in range(-back, ahead + 1):
        index = first.year * 12 + first.month - 1 + offset
        months.append(datetime.date(index // 12, index % 12 + 1, 1))
    periods = [_period(m.year, m.month) for m in months]
    with system_access("count posted assessments"):
        lot_count = Lot.objects.count()
        posted = {
            row["period"]: row["n"]
            for row in LedgerEntry.objects.filter(kind=EntryKind.ASSESSMENT, period__in=periods)
            .order_by()
            .values("period")
            .annotate(n=Count("lot_id", distinct=True))
        }
    out = []
    for month, period in zip(months, periods, strict=True):
        n = posted.get(period, 0)
        out.append(
            {
                "value": period,
                "label": f"{month:%B %Y}",
                "posted": bool(lot_count) and n >= lot_count,
                "partly": 0 < n < lot_count,
            }
        )
    return out


# The automatic posting (ledger.tasks.monthly_assessments) runs early on the
# 1st; after this hour, site time, a month still not posted is flagged.
AUTO_POST_DEADLINE_HOUR = 6


def auto_post_status(now: datetime.datetime | None = None) -> dict:
    """For the Owner accounts card when ``dues.auto_post`` is on (D48): this
    month's posting (how many homes, when), the next run, and whether the
    run looks missed (past the 1st's run and this month not on every home)."""
    now = timezone.localtime(now or timezone.now())
    first = now.date().replace(day=1)
    period = _period(first.year, first.month)
    with system_access("auto-post status"):
        lot_count = Lot.objects.count()
        rows = LedgerEntry.objects.filter(kind=EntryKind.ASSESSMENT, period=period).order_by()
        homes = rows.values("lot_id").distinct().count()
        first_created = rows.order_by("created_at").values_list("created_at", flat=True).first()
    next_first = (first + datetime.timedelta(days=32)).replace(day=1)
    past_run = now.date() > first or now.hour >= AUTO_POST_DEADLINE_HOUR
    return {
        "month": first,
        "period": period,
        "homes": homes,
        "posted_on": timezone.localdate(first_created) if first_created else None,
        "next": next_first,
        "complete": bool(lot_count) and homes >= lot_count,
        "missed": past_run and bool(lot_count) and homes < lot_count,
    }


@internal("ledger service")
def post_monthly_assessments(*, year: int, month: int, actor, request=None) -> int:
    _authorize(actor, "ledger.manage")
    amount = rule("dues.monthly_cents")
    term = charge_term()
    if not amount:
        raise LedgerError(
            term_text(
                "Set the monthly {term} amount (with its citation) in Community settings first.",
                term,
            )
        )
    day = min(rule("dues.due_day") or 1, 28)
    posted_on = datetime.date(year, month, day)
    period = _period(year, month)
    # One name for the monthly charge (WT-003, D42): "Assessment - December 2026".
    description = term.monthly_title(posted_on)
    created = 0
    with system_access("post assessments"):
        lots = list(Lot.objects.all())
    for lot in lots:
        try:
            with transaction.atomic():
                LedgerEntry.objects.create(
                    lot=lot,
                    posted_on=posted_on,
                    kind=EntryKind.ASSESSMENT,
                    amount_cents=amount,
                    period=period,
                    description=description,
                    posted_by=_user(actor),
                )
            created += 1
        except IntegrityError:
            continue  # already posted for this lot and month
    record_event(
        "ledger.assessments_posted",
        actor=_user(actor),
        request=request,
        metadata={"period": period, "lots": created, "amount_cents": amount},
    )
    # The monthly statement emails (D42): queued after commit, by the
    # automatic posting and the board's button alike; each account at most
    # once a month (ledger.StatementEmail), so a second posting sends nothing.
    from ledger.tasks import send_statement_emails

    transaction.on_commit(lambda: send_statement_emails.defer(year=year, month=month))
    return created


_ACCOUNT_NUMBER = re.compile(r"\d{9,}")


def _duplicate_reference(lot, reference: str) -> bool:
    """A payment with this check number or reference is already on this home's
    ledger and still stands. A reversed payment (a bounced check) does not
    count, so the same check can be deposited again (review BUG-006). Both
    payment entry paths use this, so they agree."""
    if not reference:
        return False
    return (
        LedgerEntry.objects.unscoped("duplicate check")
        .filter(
            lot=lot,
            kind=EntryKind.PAYMENT,
            reference=reference[:40],
            reversed_by__isnull=True,
        )
        .exists()
    )


ONLINE_METHODS = frozenset({"online", "stripe"})


def _check_apply_to(lot, apply_to, account_id=None) -> list[int]:
    """Charge ids chosen for a payment: open charges of this lot and of the
    account the payment goes to only (D33-acctfix-1)."""
    if not apply_to:
        return []
    from ledger import allocation

    account = account_id if account_id is not None else accounts.CURRENT
    open_ids = {c.entry.pk for c in allocation.open_charges(lot.pk, account=account)}
    ids = []
    for value in apply_to:
        try:
            charge_id = int(value)
        except (TypeError, ValueError) as exc:
            raise LedgerError("Choose charges from this home's open charges.") from exc
        if charge_id not in open_ids:
            raise LedgerError("Choose charges from this home's open charges.")
        if charge_id not in ids:
            ids.append(charge_id)
    return ids


def _post_payment(
    lot, *, amount_cents, received_on, method, reference, apply_to, actor, account_id=None
):
    entry = LedgerEntry(
        lot=lot,
        account_id=account_id,
        posted_on=received_on,
        kind=EntryKind.PAYMENT,
        amount_cents=-amount_cents,
        description=f"Payment ({method})",
        reference=reference[:40],
        posted_by=_user(actor),
    )
    entry._apply_to = apply_to  # read by ledger.allocation when the entry posts
    with transaction.atomic():
        entry.save()
    return entry


@internal("ledger service")
def record_payment(
    *,
    lot: Lot,
    amount_cents: int,
    received_on,
    method: str = "check",
    reference: str = "",
    apply_to: list[int] | None = None,
    account_id: int | None = None,
    actor,
    request=None,
) -> LedgerEntry:
    """A payment received by check, cash, ACH... ``apply_to``: charges the
    payer chose, in order; the rest follows the application rule.
    ``account_id``: the ownership account it pays (by default the one its
    date falls in), e.g. a former owner's arrears paid at settlement.

    Online (Stripe) payments are never entered here: only the Stripe sync
    records them, from what Stripe itself reports (D32-payments)."""
    _authorize(actor, "ledger.manage")
    _refuse_own(actor, lot, "record_payment", request)
    if (method or "").strip().lower() in ONLINE_METHODS:
        raise LedgerError("Online payments are recorded automatically from Stripe.")
    if amount_cents <= 0:
        raise LedgerError("A payment must be a positive amount.")
    if _ACCOUNT_NUMBER.search(reference.replace(" ", "").replace("-", "")):
        raise LedgerError("Enter a check number or short reference, never an account number.")
    if _duplicate_reference(lot, reference):
        raise LedgerError(f"Payment {reference[:40]} for this home is already recorded.")
    account_id = accounts.check_account(lot.pk, account_id)
    if account_id is None:
        account_id = accounts.account_on(lot.pk, received_on)
    entry = _post_payment(
        lot,
        amount_cents=amount_cents,
        received_on=received_on,
        method=method,
        reference=reference,
        apply_to=_check_apply_to(lot, apply_to, account_id),
        actor=actor,
        account_id=account_id,
    )
    record_event(
        "ledger.payment_recorded",
        actor=_user(actor),
        request=request,
        target=entry,
        metadata={"lot_id": lot.pk, "amount_cents": amount_cents},
    )
    return entry


@internal("ledger service")
def record_online_payment(
    *,
    lot: Lot,
    amount_cents: int,
    received_on,
    payment_intent_id: str,
    apply_to,
    actor,
    account_id: int | None = None,
) -> LedgerEntry:
    """Only for payments.sync, after Stripe confirmed the payment (actor is
    SYSTEM; no form or view reaches this). ``account_id``: the account of
    the person who paid (stored on the checkout when they started it), kept
    even if the home was sold meanwhile (BUG-003, D34-fixmoney-1); the
    chosen charges are that account's. Without it, the account of
    ``received_on``."""
    if actor is not SYSTEM:
        raise PermissionError("Online payments are recorded only by the Stripe sync.")
    if amount_cents <= 0 or not payment_intent_id:
        raise LedgerError("An online payment needs an amount and its Stripe reference.")
    account_id = accounts.check_account(lot.pk, account_id)
    if account_id is None:
        account_id = accounts.account_on(lot.pk, received_on)
    open_ids = {c.entry.pk for c in _allocation().open_charges(lot.pk, account=account_id)}
    entry = _post_payment(
        lot,
        account_id=account_id,
        amount_cents=amount_cents,
        received_on=received_on,
        method="online",
        reference=payment_intent_id,
        apply_to=[int(c) for c in (apply_to or []) if int(c) in open_ids],
        actor=actor,
    )
    record_event(
        "ledger.payment_recorded",
        actor=None,
        target=entry,
        metadata={"lot_id": lot.pk, "amount_cents": amount_cents},
    )
    return entry


@dataclass
class BatchPayment:
    lot: Lot
    amount_cents: int
    received_on: datetime.date
    reference: str = ""


def check_payment(p: BatchPayment, *, today: datetime.date | None = None) -> list[str]:
    """Why one payment row cannot be recorded (empty when it can)."""
    today = today or timezone.localdate()
    errors = []
    if p.amount_cents <= 0:
        errors.append("Enter an amount above $0.00.")
    if p.received_on > today:
        errors.append("The date received cannot be in the future.")
    if _ACCOUNT_NUMBER.search(p.reference.replace(" ", "").replace("-", "")):
        errors.append("Enter a check number, never an account number.")
    elif _duplicate_reference(p.lot, p.reference):
        errors.append(f"Check {p.reference} for this home is already recorded.")
    return errors


@internal("ledger service")
def record_payments(payments: list[BatchPayment], *, actor, request=None) -> list[LedgerEntry]:
    """A stack of checks entered on one page: all are recorded, or none.
    Each is a normal payment (with its own audit event) plus one batch event."""
    _authorize(actor, "ledger.manage")
    if not payments:
        raise LedgerError("Enter at least one payment.")
    for p in payments:
        _refuse_own(actor, p.lot, "record_payment", request)
    seen = set()
    for p in payments:
        if check_payment(p):
            raise LedgerError("Fix the payments marked below; nothing was recorded.")
        if p.reference and (p.lot.pk, p.reference) in seen:
            raise LedgerError(f"Check {p.reference} is entered twice for the same home.")
        seen.add((p.lot.pk, p.reference))
    with transaction.atomic():
        entries = [
            record_payment(
                lot=p.lot,
                amount_cents=p.amount_cents,
                received_on=p.received_on,
                method="check",
                reference=p.reference,
                actor=actor,
                request=request,
            )
            for p in payments
        ]
    record_event(
        "ledger.payments_batch_recorded",
        actor=_user(actor),
        request=request,
        metadata={
            "count": len(entries),
            "total_cents": sum(p.amount_cents for p in payments),
        },
    )
    return entries


@internal("ledger service")
def post_charge(
    *,
    lot: Lot,
    kind: str,
    amount_cents: int,
    description: str,
    posted_on=None,
    account_id: int | None = None,
    actor,
    request=None,
) -> LedgerEntry:
    _authorize(actor, "ledger.manage")
    _refuse_own(actor, lot, "post_charge", request)
    if kind not in (EntryKind.OTHER_CHARGE, EntryKind.LATE_FEE, EntryKind.RESALE_FEE):
        raise LedgerError(term_text("Use the monthly {term} posting for the {term}."))
    if kind == EntryKind.LATE_FEE:
        _authorize(actor, "late_fee.post")  # a board decision, not the manager's
        if not settings.LEDGER_LATE_FEES_ENABLED:
            raise LedgerError(
                "Late fees are switched off on this server (LEDGER_LATE_FEES_ENABLED)."
            )
    if amount_cents <= 0 or not description.strip():
        raise LedgerError("A charge needs a positive amount and a description.")
    entry = LedgerEntry.objects.create(
        lot=lot,
        account_id=accounts.check_account(lot.pk, account_id),
        posted_on=posted_on or timezone.localdate(),
        kind=kind,
        amount_cents=amount_cents,
        description=description.strip()[:200],
        posted_by=_user(actor),
    )
    record_event(
        "ledger.charge_posted",
        actor=_user(actor),
        request=request,
        target=entry,
        metadata={"lot_id": lot.pk, "kind": kind, "amount_cents": amount_cents},
    )
    return entry


@internal("ledger service")
def post_credit(
    *,
    lot: Lot,
    amount_cents: int,
    description: str,
    account_id: int | None = None,
    actor,
    request=None,
) -> LedgerEntry:
    _authorize(actor, "ledger.manage")
    _refuse_own(actor, lot, "post_credit", request)
    if amount_cents <= 0 or not description.strip():
        raise LedgerError("A credit needs a positive amount and a reason.")
    entry = LedgerEntry.objects.create(
        lot=lot,
        account_id=accounts.check_account(lot.pk, account_id),
        posted_on=timezone.localdate(),
        kind=EntryKind.CREDIT,
        amount_cents=-amount_cents,
        description=description.strip()[:200],
        posted_by=_user(actor),
    )
    record_event(
        "ledger.credit_posted",
        actor=_user(actor),
        request=request,
        target=entry,
        metadata={"lot_id": lot.pk, "amount_cents": amount_cents},
    )
    return entry


def _recorded_refunds(payment: LedgerEntry):
    """Refunds recorded against an online payment that still stand (a
    recorded refund carries the payment's Stripe reference)."""
    return LedgerEntry.objects.unscoped("recorded refunds").filter(
        lot_id=payment.lot_id,
        kind=EntryKind.REFUND,
        reference=payment.reference,
        reversed_by__isnull=True,
    )


@internal("ledger service")
def record_online_refund(
    *, payment: LedgerEntry, amount_cents: int, refund_id: str, actor, request=None
) -> LedgerEntry:
    """Record on the account the part of an online payment refunded at Stripe
    (the payment's share, without the surcharge: D33-acctfix-5). Only for
    payments.services.record_refund, from a refund Stripe reported. The owner
    has the money back, so the account goes up by it; the journal books it
    against Stripe clearing. A full refund may instead reverse the payment,
    which also reopens the charges it paid."""
    _authorize(actor, "ledger.manage")
    if payment.kind != EntryKind.PAYMENT or not payment.reference or amount_cents <= 0:
        raise LedgerError("Only part of an online payment can be recorded as refunded.")
    description = f"Refund at Stripe {refund_id}"[:200]
    with transaction.atomic():
        LedgerEntry.objects.unscoped("refund").select_for_update().get(pk=payment.pk)
        if LedgerEntry.objects.unscoped("refund").filter(reverses=payment).exists():
            raise LedgerError("This payment was reversed; that already records the refund.")
        refunds = _recorded_refunds(payment)
        if refunds.filter(description=description).exists():
            raise LedgerError("This refund is already recorded on the account.")
        already = refunds.aggregate(s=Sum("amount_cents"))["s"] or 0
        if already + amount_cents > -payment.amount_cents:
            raise LedgerError("The refunds would be more than the payment.")
        entry = LedgerEntry.objects.create(
            lot_id=payment.lot_id,
            account_id=payment.account_id,
            posted_on=timezone.localdate(),
            kind=EntryKind.REFUND,
            amount_cents=amount_cents,
            description=description,
            reference=payment.reference,
            posted_by=_user(actor),
        )
    record_event(
        "ledger.refund_recorded",
        actor=_user(actor),
        request=request,
        target=entry,
        metadata={"lot_id": payment.lot_id, "amount_cents": amount_cents},
    )
    return entry


@internal("ledger service")
def reverse_entry(entry: LedgerEntry, *, reason: str, actor, request=None) -> LedgerEntry:
    """Corrections are new, opposite entries (CLAUDE.md rule 5)."""
    _authorize(actor, "ledger.manage")
    _refuse_own(actor, entry, "reverse", request)
    if not reason.strip():
        raise LedgerError("Give the reason for the reversal.")
    if entry.kind == EntryKind.REVERSAL:
        raise LedgerError("A reversal cannot itself be reversed; post a new entry instead.")
    if entry.kind == EntryKind.PAYMENT and entry.reference and _recorded_refunds(entry).exists():
        raise LedgerError(
            "Part of this payment is recorded as refunded; reverse that refund entry first, "
            "or the refund would count twice."
        )
    try:
        with transaction.atomic():
            reversal = LedgerEntry.objects.create(
                lot_id=entry.lot_id,
                posted_on=timezone.localdate(),
                kind=EntryKind.REVERSAL,
                amount_cents=-entry.amount_cents,
                reverses=entry,
                description=f"Reversal of #{entry.pk}: {reason.strip()}"[:200],
                posted_by=_user(actor),
            )
    except IntegrityError as exc:
        raise LedgerError("This entry has already been reversed.") from exc
    record_event(
        "ledger.entry_reversed",
        actor=_user(actor),
        request=request,
        target=reversal,
        metadata={"lot_id": entry.lot_id, "reverses": entry.pk},
    )
    return reversal


# --- Moving entries after a sale recorded with an earlier date (D34-fixmoney-1) -------------

_period_change: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "ledger_period_change", default=None
)


@contextlib.contextmanager
def account_periods_change(lot_id: int, *, actor, request=None):
    """Wrap a change to a lot's ownership account periods (community
    services: recording or ending an ownership, a transfer). When the outer
    change is done, entries already posted that now belong to another
    account are moved there (``move_entries``). Nested changes of the same
    lot are handled once, by the outermost."""
    active = _period_change.get()
    if active is not None and lot_id in active:
        yield
        return
    token = _period_change.set({**(active or {}), lot_id: True})
    try:
        with transaction.atomic():
            # Lock the lot before reading the periods (fixres BUG-007): two
            # changes to the same lot at once take turns, so the second
            # compares with what the first left.
            with system_access("lock lot for account periods"):
                list(Lot.objects.select_for_update().filter(pk=lot_id).values_list("pk"))
            before = accounts.periods(lot_id)
            yield
            move_entries(lot_id, before, actor=actor, request=request)
            # Automatic payments end with the ownership account (D46).
            from payments.services import ownership_changed

            ownership_changed(lot_id)
    finally:
        _period_change.reset(token)


@internal("ledger service")
def move_entries(lot_id: int, before: list, *, actor, request=None) -> list[LedgerEntry]:
    """Move to the right ownership account every entry the old account
    periods (``before``) had put on another one (ledger.accounts.
    moves_after_change). Each move is two new entries and never changes an
    amount: a reversal on the old account, dated like the original, and a
    copy on the new account (``moved_from``), same date, kind, amount,
    reference and month. A payment keeps the charges it was applied to:
    they are read before anything is reversed, and the payment's copy is
    applied first to the copies of those charges (fixres BUG-002). The board
    gets a My items entry listing the moves (``EntryMoveNotice``, fixres
    BUG-003). Returns the copies."""
    from community.models import Account
    from ledger import allocation
    from ledger.models import EntryMoveNotice

    plan = accounts.moves_after_change(lot_id, before)
    if not plan:
        return []
    numbers = dict(Account.objects.filter(lot_id=lot_id).values_list("pk", "number"))
    moving = {entry.pk for entry, _t in plan}
    # What each moving payment pays now, read before any reversal unwinds it.
    directed = {
        entry.pk: [c for c, _cents in allocation.applications_of(entry.pk) if c in moving]
        for entry, _t in plan
        if entry.amount_cents < 0
    }
    with transaction.atomic():
        for entry, target in plan:
            LedgerEntry.objects.create(
                lot_id=lot_id,
                posted_on=entry.posted_on,
                kind=EntryKind.REVERSAL,
                amount_cents=-entry.amount_cents,
                reverses=entry,
                description=f"Moved to account {numbers.get(target, target)}: #{entry.pk}"[:200],
                posted_by=_user(actor),
            )
        copies: dict[int, LedgerEntry] = {}
        for entry, target in plan:
            copy = LedgerEntry(
                lot_id=lot_id,
                account_id=target,
                posted_on=entry.posted_on,
                kind=entry.kind,
                amount_cents=entry.amount_cents,
                description=entry.description,
                reference=entry.reference,
                period=entry.period,
                moved_from=entry,
                posted_by=_user(actor),
            )
            copy._defer_allocation = True  # applied below, all together
            copy.save()
            copies[entry.pk] = copy
        allocation.post_moved(
            lot_id,
            [
                (copies[entry.pk], [copies[c].pk for c in directed.get(entry.pk, [])])
                for entry, _t in plan
            ],
        )
        out = list(copies.values())
        old_ids = sorted({e.account_id for e, _t in plan if e.account_id})
        new_ids = sorted({t for _e, t in plan})
        EntryMoveNotice.objects.create(
            lot_id=lot_id,
            copy_ids=[c.pk for c in out],
            from_accounts=(", ".join(numbers.get(a, str(a)) for a in old_ids) or "no account")[
                :200
            ],
            to_accounts=", ".join(numbers.get(a, str(a)) for a in new_ids)[:200],
        )
        record_event(
            "ledger.entries_moved",
            actor=_user(actor),
            request=request,
            metadata={
                "lot_id": lot_id,
                "count": len(out),
                "from_accounts": ",".join(str(a) for a in old_ids),
                "to_accounts": ",".join(str(a) for a in new_ids),
            },
        )
    return out


REASSIGNABLE = {EntryKind.PAYMENT, EntryKind.CREDIT}


def reassign_entry(
    entry: LedgerEntry, *, account_id: int, reason: str, actor, request=None
) -> LedgerEntry:
    """The board moves a payment or credit to another ownership account of
    the same lot (owner decision 2026-11-26), e.g. a seller's settlement
    check that a backdated sale moved to the buyer. Like every move it never
    changes an amount: a reversal on the old account and a copy on the
    chosen one (``moved_from``), applied there by the application rule. A
    later change of ownership dates leaves it where the board put it
    (``accounts.moves_after_change`` only moves entries the date rule
    placed). Returns the copy."""
    from community.models import Account
    from ledger import allocation

    _authorize(actor, "ledger.reassign")
    _refuse_own(actor, entry, "reassign", request)
    reason = reason.strip()
    if not reason:
        raise LedgerError("Give the reason for moving it.")
    if entry.kind not in REASSIGNABLE:
        raise LedgerError("Only a payment or a credit can be moved to another account.")
    # Authorized above (board only); the entry came from the caller's scoped
    # lookup, and the target account must belong to the same lot.
    with transaction.atomic(), system_access("board moves an entry to another account"):
        list(Lot.objects.select_for_update().filter(pk=entry.lot_id).values_list("pk"))
        entry = LedgerEntry.objects.unscoped("reassign").select_for_update().get(pk=entry.pk)
        if LedgerEntry.objects.unscoped("reassign").filter(reverses=entry).exists():
            raise LedgerError("This entry was already reversed or moved.")
        target = Account.objects.filter(pk=account_id, lot_id=entry.lot_id).first()
        if target is None:
            raise LedgerError("Choose one of this home's accounts.")
        if target.pk == entry.account_id:
            raise LedgerError("It is already on that account.")
        numbers = dict(Account.objects.filter(lot_id=entry.lot_id).values_list("pk", "number"))
        LedgerEntry.objects.create(
            lot_id=entry.lot_id,
            posted_on=entry.posted_on,
            kind=EntryKind.REVERSAL,
            amount_cents=-entry.amount_cents,
            reverses=entry,
            description=f"Moved to account {target.number}: #{entry.pk} ({reason})"[:200],
            posted_by=_user(actor),
        )
        copy = LedgerEntry(
            lot_id=entry.lot_id,
            account_id=target.pk,
            posted_on=entry.posted_on,
            kind=entry.kind,
            amount_cents=entry.amount_cents,
            description=entry.description,
            reference=entry.reference,
            period=entry.period,
            moved_from=entry,
            posted_by=_user(actor),
        )
        copy._defer_allocation = True
        copy.save()
        allocation.post_moved(entry.lot_id, [(copy, [])])
        record_event(
            "ledger.entry_reassigned",
            actor=_user(actor),
            request=request,
            metadata={
                "lot_id": entry.lot_id,
                "entry_id": entry.pk,
                "from_account": str(numbers.get(entry.account_id, "")),
                "to_account": str(target.number),
            },
        )
    return copy


@internal("ledger service")
def moved_notes(entries) -> dict[int, str]:
    """For the moved copies among ``entries``: "Moved from account N on
    <date>" (fixres BUG-003), shown on board pages and owner statements."""
    from core.dates import short_date

    ids = [e.moved_from_id for e in entries if getattr(e, "moved_from_id", None)]
    if not ids:
        return {}
    numbers = dict(
        LedgerEntry.objects.unscoped("moved notes")
        .filter(pk__in=ids)
        .values_list("pk", "account__number")
    )
    return {
        e.pk: f"Moved from account {numbers.get(e.moved_from_id) or '(none)'}"
        f" on {short_date(e.created_at)}"
        for e in entries
        if getattr(e, "moved_from_id", None)
    }


def open_move_notices(lot_id: int, *, user) -> list:
    """The lot's move notices the board has not checked yet, each with its
    copies (``notice.moved``: list of (entry, note))."""
    from ledger.models import EntryMoveNotice

    notices = list(
        EntryMoveNotice.objects.for_user(user).filter(lot_id=lot_id, checked_at__isnull=True)
    )
    for notice in notices:
        moved = list(LedgerEntry.objects.for_user(user).filter(pk__in=notice.copy_ids))
        notes = moved_notes(moved)
        notice.moved = [(e, notes.get(e.pk, "")) for e in moved]
    return notices


def check_move_notice(notice, *, actor, request=None):
    """The board checked the moved entries (and put back, with the account
    choice, any that belonged where they were)."""
    from ledger.models import EntryMoveNotice

    _authorize(actor, "ledger.manage")
    _refuse_own(actor, notice, "check_moves", request)
    updated = (
        EntryMoveNotice.objects.unscoped("check move notice")
        .filter(pk=notice.pk, checked_at__isnull=True)
        .update(checked_at=timezone.now(), checked_by=_user(actor))
    )
    if not updated:
        raise LedgerError("These moved entries are already marked checked.")
    record_event(
        "ledger.moves_checked",
        actor=_user(actor),
        request=request,
        target=notice,
        metadata={"lot_id": notice.lot_id, "count": notice.count},
    )
    return notice


# --- Aging (credits applied to the oldest charges first) ------------------------------------

# One set of buckets everywhere (UX review): Current (due today or not yet
# due), then 1-30, 31-60, 61-90 and over 90 days past the charge's due date.
BUCKETS = ("current", "1_30", "31_60", "61_90", "over_90")
BUCKET_LABELS = {
    "current": "Current",
    "1_30": "1\u201330 days",
    "31_60": "31\u201360 days",
    "61_90": "61\u201390 days",
    "over_90": "Over 90 days",
}


def _bucket(age_days: int) -> str:
    if age_days <= 0:
        return "current"
    if age_days <= 30:
        return "1_30"
    if age_days <= 60:
        return "31_60"
    if age_days <= 90:
        return "61_90"
    return "over_90"


def aging(lot_id: int, as_of: datetime.date | None = None, *, account=ALL) -> dict[str, int]:
    as_of = as_of or timezone.localdate()
    charges, credit = _allocation().open_items_as_of(lot_id, as_of, account=account)
    result = dict.fromkeys(BUCKETS, 0)
    for posted_on, open_cents in charges:
        result[_bucket((as_of - posted_on).days)] += open_cents
    result["credit"] = credit
    return result


def aging_columns(a: dict[str, int]) -> list[tuple[str, int]]:
    """(label, cents) for each bucket, in order, for tables and stat cards."""
    return [(BUCKET_LABELS[b], a[b]) for b in BUCKETS]


@dataclass
class AmountDue:
    amount_cents: int  # what is owed now (0 when paid up or in credit)
    due_on: datetime.date | None  # when the oldest unpaid charge is or was due
    past_due: bool


def amount_due(lot_id: int, today: datetime.date | None = None, *, account=CURRENT) -> AmountDue:
    """What is owed on an account (today's owners' by default) and by when
    ("Due by <date>": the oldest unpaid charge's date). Shared by My account
    and the dashboard's balance to-do."""
    today = today or timezone.localdate()
    account = accounts.resolve(lot_id, account)
    owed = balance(lot_id, account=account)
    if owed <= 0:
        return AmountDue(0, None, False)
    open_ = _allocation().open_charges(lot_id, account=account)
    due_on = min((c.due_on for c in open_), default=today)
    return AmountDue(owed, due_on, due_on < today)


@dataclass
class StatementRow:
    posted_on: datetime.date | None
    description: str
    charge_cents: int
    payment_cents: int
    balance_cents: int


def statement_rows(statement: OwnerStatement) -> list[StatementRow]:
    """The statement as Charges / Payments / running Balance columns, so a
    payment reads as a payment, not as a negative amount."""
    running = statement.brought_forward
    rows = []
    if statement.brought_forward:
        rows.append(StatementRow(None, "Balance brought forward", 0, 0, running))
    notes = moved_notes(statement.entries)
    for e in statement.entries:
        running += e.amount_cents
        rows.append(
            StatementRow(
                e.posted_on,
                f"{e.description} ({notes[e.pk]})" if e.pk in notes else e.description,
                max(e.amount_cents, 0),
                max(-e.amount_cents, 0),
                running,
            )
        )
    return rows


@internal("ledger service")
def delinquent_lots(min_age_days: int = 30, as_of=None) -> list[tuple[Lot, int, dict]]:
    """Lots with charges open past ``min_age_days``, with what is open on
    them. Open charges, not the netted balance: a former owner's credit on
    the old account never hides the current owner's arrears (D33-acctfix-1)."""
    out = []
    with system_access("delinquency list"):
        lots = {lot.pk: lot for lot in Lot.objects.all()}
    for lot_id in balances():
        a = aging(lot_id, as_of)
        owed = sum(a[b] for b in BUCKETS)
        if owed <= 0:
            continue
        overdue = a["31_60"] + a["61_90"] + a["over_90"] if min_age_days >= 30 else owed
        if overdue > 0:
            out.append((lots[lot_id], owed, a))
    return sorted(out, key=lambda r: -r[1])


@internal("ledger service")
def statement_pdf(lot: Lot, *, since: datetime.date | None = None, reminder: bool = False):
    """The account statement (and, with ``reminder``, the first delinquency
    notice with the statement attached), in the association's layout."""
    from ledger import letters

    if reminder:
        return letters.notice_pdf(lot, "first")
    return letters.statement_pdf(lot, since=since)


# --- Collections (tracking only; no automatic fees or legal steps) -----------------------

COLLECTION = Workflow(
    "collection",
    [
        Transition(
            "send_reminder", {"open"}, "reminder_sent", "collections.manage", "First reminder sent"
        ),
        Transition(
            "send_second_reminder",
            {"reminder_sent"},
            "second_reminder",
            "collections.manage",
            "Second reminder sent",
        ),
        Transition(
            "send_final_notice",
            {"second_reminder"},
            "final_notice",
            "collections.manage",
            "Final notice sent",
        ),
        Transition(
            "payment_plan",
            {"open", "reminder_sent", "second_reminder", "final_notice"},
            "payment_plan",
            "collections.manage",
            "Payment plan agreed",
            note_required=True,
        ),
        Transition(
            "refer",
            {"second_reminder", "final_notice", "payment_plan"},
            "referred",
            "collections.refer",
            "Referred for legal collection (board)",
            note_required=True,
        ),
        Transition(
            "close",
            {
                "open",
                "reminder_sent",
                "second_reminder",
                "final_notice",
                "payment_plan",
                "referred",
            },
            "closed",
            "collections.manage",
            "Close (paid or resolved)",
            note_required=True,
        ),
    ],
)


@internal("ledger service")
def open_case(*, lot: Lot, notes: str = "", actor, request=None) -> CollectionCase:
    require(actor, "collections.manage")
    conflicts.refuse(
        actor, lot, area="collection", action="open", request=request, error=LedgerError
    )
    if CollectionCase.objects.filter(lot=lot).exclude(state="closed").exists():
        raise LedgerError("This home already has an open collection case.")
    try:
        with transaction.atomic():
            case = CollectionCase.objects.create(lot=lot, opened_by=actor, notes=notes)
    except IntegrityError as exc:
        raise LedgerError("This home already has an open collection case.") from exc
    record_event(
        "collection.opened",
        actor=actor,
        request=request,
        target=case,
        metadata={"lot_id": lot.pk, "balance_cents": balance(lot.pk)},
    )
    return case


# --- Budgets -------------------------------------------------------------------------------


def fiscal_months(fiscal_year: int) -> list[tuple[int, int]]:
    start = CommunityProfile.get().fiscal_year_start_month
    months = []
    year, month = fiscal_year, start
    for _ in range(12):
        months.append((year, month))
        month += 1
        if month == 13:
            year, month = year + 1, 1
    return months


def create_budget(*, fiscal_year: int, actor, request=None) -> Budget:
    require(actor, "budget.manage")
    try:
        budget = Budget.objects.create(fiscal_year=fiscal_year)
    except IntegrityError as exc:
        raise LedgerError("That year already has a budget.") from exc
    record_event(
        "budget.created",
        actor=actor,
        request=request,
        target=budget,
        metadata={"fiscal_year": fiscal_year},
    )
    return budget


def add_line(
    budget: Budget,
    *,
    kind: str,
    name: str,
    annual_cents: int,
    category=None,
    income_source: str = "",
    actor,
    request=None,
) -> BudgetLine:
    require(actor, "budget.manage")
    if budget.status != BudgetStatus.DRAFT:
        raise LedgerError(
            "An adopted budget cannot be changed; adopt a revised budget for a "
            "new year or record a board resolution."
        )
    if kind == LineKind.EXPENSE and category is None:
        raise LedgerError("Expense lines need an expense category.")
    if kind == LineKind.INCOME and income_source not in IncomeSource.values:
        raise LedgerError("Income lines need an income source.")
    line = BudgetLine.objects.create(
        budget=budget,
        kind=kind,
        name=name.strip()[:120],
        annual_cents=annual_cents,
        category=category,
        income_source=income_source,
    )
    record_event(
        "budget.line_added",
        actor=actor,
        request=request,
        target=budget,
        metadata={"kind": kind, "annual_cents": annual_cents},
    )
    return line


def adopt_budget(budget: Budget, *, actor, request=None) -> Budget:
    require(actor, "budget.manage")
    if budget.status != BudgetStatus.DRAFT or not budget.lines.exists():
        raise LedgerError("Only a draft budget with lines can be adopted.")
    Budget.objects.filter(pk=budget.pk).update(
        status=BudgetStatus.ADOPTED, adopted_on=timezone.localdate(), adopted_by=actor
    )
    record_event(
        "budget.adopted",
        actor=actor,
        request=request,
        target=budget,
        metadata={"fiscal_year": budget.fiscal_year},
    )
    budget.refresh_from_db()
    return budget


@internal("ledger service")
def _actual_for(line: BudgetLine, months) -> list[int]:
    from operations.models import Invoice

    out = []
    for year, month in months:
        if line.kind == LineKind.EXPENSE:
            total = (
                Invoice.objects.filter(
                    category=line.category,
                    state__in=["approved", "paid"],
                    invoice_date__year=year,
                    invoice_date__month=month,
                ).aggregate(s=Sum("amount_cents"))["s"]
                or 0
            )
        else:
            kinds = {
                IncomeSource.ASSESSMENTS: [EntryKind.ASSESSMENT],
                IncomeSource.RESALE_FEES: [EntryKind.RESALE_FEE],
                IncomeSource.OTHER: [EntryKind.OTHER_CHARGE, EntryKind.LATE_FEE],
            }
            total = (
                LedgerEntry.objects.filter(
                    kind__in=kinds[line.income_source],
                    posted_on__year=year,
                    posted_on__month=month,
                    reversed_by__isnull=True,
                ).aggregate(s=Sum("amount_cents"))["s"]
                or 0
            )
        out.append(total)
    return out


@internal("ledger service")
def budget_vs_actual(budget: Budget) -> dict:
    months = fiscal_months(budget.fiscal_year)
    rows = []
    for line in budget.lines.select_related("category"):
        planned = line.monthly_cents()
        actual = _actual_for(line, months)
        rows.append(
            {
                "line": line,
                "planned": planned,
                "actual": actual,
                "planned_total": sum(planned),
                "actual_total": sum(actual),
                "variance": sum(actual) - sum(planned),
            }
        )
    return {"months": months, "rows": rows}


# --- Resale certificates ----------------------------------------------------------------------


ISSUED_CERTIFICATE_LABEL = "Issued certificate"


@internal("ledger service")
def _issue(obj, actor):
    obj.issued_at = timezone.now()
    obj.save(update_fields=["issued_at"])
    # Keep the certificate exactly as issued (review D5): later downloads serve
    # this stored copy, never a fresh rendering from today's data.
    from django.core.files.uploadedfile import SimpleUploadedFile

    from documents.attachments import attach

    attach(
        obj,
        SimpleUploadedFile(
            f"resale-certificate-{obj.pk}.pdf", resale_certificate(obj), "application/pdf"
        ),
        actor=actor,
        label=ISSUED_CERTIFICATE_LABEL,
        allowed={"pdf"},
    )
    if obj.fee_cents:
        LedgerEntry.objects.create(
            lot_id=obj.lot_id,
            posted_on=timezone.localdate(),
            kind=EntryKind.RESALE_FEE,
            amount_cents=obj.fee_cents,
            description=f"Resale certificate #{obj.pk}",
            posted_by=_user(actor),
        )


RESALE = Workflow(
    "resale",
    [
        Transition("start", {"submitted"}, "in_progress", "resale.manage", "Start preparing"),
        Transition(
            "issue",
            {"submitted", "in_progress"},
            "issued",
            "resale.manage",
            "Issue certificate",
            after=_issue,
        ),
        Transition(
            "cancel",
            {"submitted", "in_progress"},
            "cancelled",
            "resale.manage",
            "Cancel",
            note_required=True,
        ),
    ],
)


@internal("ledger service")
def request_resale(
    *,
    lot: Lot,
    requester_name: str,
    requester_email: str = "",
    expected_settlement=None,
    actor,
    request=None,
) -> ResaleRequest:
    require(actor, "resale.request")
    from core.policy import can, roles_for

    if not can(actor, "resale.manage") and lot.pk not in roles_for(actor).owned_lot_ids:
        raise LedgerError("Owners can request a certificate only for their own home.")
    days = rule("resale.deadline_days")
    obj = ResaleRequest.objects.create(
        lot=lot,
        requested_by=actor,
        requester_name=requester_name.strip()[:120],
        requester_email=requester_email,
        expected_settlement=expected_settlement,
        fee_cents=rule("resale.fee_cents"),
        due_on=timezone.localdate() + datetime.timedelta(days=days) if days else None,
    )
    record_event(
        "resale.requested", actor=actor, request=request, target=obj, metadata={"lot_id": lot.pk}
    )
    return obj


@internal("ledger service")
def resale_certificate(obj: ResaleRequest) -> bytes:
    """The resale certificate under 68 Pa.C.S. § 5407, in the layout of the
    association's sample: every value filled in from the portal is in bold.
    Association-wide facts come from Community settings; this lot's figures
    from its account and records. The board confirms it with counsel."""
    from compliance.models import Violation

    profile = CommunityProfile.get()
    lot = obj.lot
    with system_access("resale certificate"):
        open_violations = list(
            Violation.objects.filter(lot=lot)
            .exclude(state__in=["closed", "dismissed", "cured"])
            .exclude(noticed_at__isnull=True)
        )
    dues, dues_cite = rule_with_citation("dues.monthly_cents")
    due_day = min(rule("dues.due_day") or 1, 28)
    # The selling owners' account only (D33-acctfix-1): what a buyer takes on.
    bal = balance(lot.pk, as_of=timezone.localdate(), account=CURRENT)
    today = timezone.localdate()

    def b(text) -> str:
        return f"**{text}**"

    def stated(text: str) -> str:
        text = " ".join((text or "").split())
        return b(text) if text else b("None")

    violations_text = (
        "; ".join(
            f"{v.get_category_display()} (notice of {v.noticed_at:%B %-d, %Y})"
            for v in open_violations
        )
        if open_violations
        else ""
    )
    if obj.other_disclosures.strip():
        violations_text = (violations_text + "; " if violations_text else "") + (
            obj.other_disclosures.strip()
        )
    designations = []
    for line in (profile.resale_reserve_designations or "").splitlines():
        amount, _, project = line.partition("|")
        if amount.strip() or project.strip():
            designations.append((amount.strip(), project.strip()))
    body = [
        ("heading", "HOMEOWNER ASSOCIATION RESALE CERTIFICATE"),
        ("normal", ""),
        (
            "normal",
            "This certificate is provided pursuant to the provisions of Section 5407 of the "
            f"Pennsylvania Uniform Planned Community Act. {b(profile.legal_name)} (hereinafter "
            f'"Association") represents the following with respect to {b(lot.legal_label)} '
            f'(hereinafter "Unit"), as of {b(today.strftime("%B %-d, %Y"))}.',
        ),
        ("normal", ""),
        (
            "normal",
            "1. The sale of the Unit is not subject to any right of first refusal or any other "
            "restraint on the free alienability of the Unit known to the Association unless "
            f"otherwise stated here: {stated(profile.resale_restraints)}",
        ),
        (
            "normal",
            "2. The regular monthly common expense assessment of the Unit is "
            + (b(format_cents(dues)) if dues else b("not set"))
            + f", payable in {b('monthly')} installments, due on or before day {b(due_day)} of "
            "each month" + (f" ({b(dues_cite)})" if dues_cite else "") + ".",
        ),
        ("normal", ""),
        (
            "normal",
            "As of the date of this certificate, there are no unpaid common expenses or special "
            "assessments currently due and payable by the Unit owner except as otherwise stated "
            "here: "
            + (
                b(f"{format_cents(bal)} unpaid, per the attached account") if bal > 0 else b("None")
            ),
        ),
        (
            "normal",
            "As of the date of this certificate, there are no surplus fund credits applicable to "
            "the Unit except as otherwise stated here: "
            + (b(f"{format_cents(-bal)} credit on the account") if bal < 0 else b("None")),
        ),
        ("normal", ""),
        (
            "normal",
            "3. No other fees are presently due to the Association by the Unit owner(s) except as "
            f"otherwise stated here: {stated(profile.resale_other_fees)}",
        ),
        ("normal", ""),
        (
            "normal",
            "The Unit owner is personally responsible for all County, Township and School real "
            "estate taxes on the Unit as well as all utilities furnished to the individual unit.",
        ),
        ("normal", ""),
        (
            "normal",
            "4. As of the date of this certificate no capital expenditures are proposed by the "
            "Association for the current or the two next succeeding fiscal years unless otherwise "
            f"stated here: {stated(profile.resale_capital_expenditures)}",
        ),
        (
            "normal",
            "5. The Association has "
            + (
                b(format_cents(profile.resale_reserves_cents))
                if profile.resale_reserves_cents is not None
                else b("not stated")
            )
            + " reserved for capital expenditures. Those reserves have not been specifically "
            "designated by the Association for specified projects unless otherwise stated here:"
            + ("" if designations else f" {b('None')}"),
        ),
    ]
    for amount, project in designations:
        body.append(("normal", f"     Amount {b(amount)}     Project: {b(project)}"))
    budget = Budget.objects.filter(status=BudgetStatus.ADOPTED).first()
    body += [
        (
            "normal",
            "6. The most recent regularly prepared balance sheet and income and expense "
            "statement, if any, of the Association is attached hereto and incorporated herein "
            "by reference.",
        ),
        (
            "normal",
            "7. The current operating budget of the Association"
            + (f" ({b(budget)})" if budget else "")
            + " is attached hereto and incorporated herein by reference.",
        ),
        (
            "normal",
            "8. At present, there exist no judgments against the Association known to it, nor "
            "are there any pending lawsuits known to the Association to which it is a party "
            f"unless otherwise stated here: {stated(profile.resale_judgments)}",
        ),
        (
            "normal",
            "9. Pursuant to the requirements of the Uniform Planned Community Act, the "
            "Association maintains public liability insurance of the common facilities in the "
            "amount of "
            + (
                b(format_cents(profile.resale_insurance_cents))
                if profile.resale_insurance_cents is not None
                else b("not stated")
            )
            + ". Such insurance is maintained with "
            + b(profile.resale_insurance_company or "not stated")
            + ".",
        ),
        (
            "normal",
            "10. The Executive Board of the Association has no knowledge of any alterations or "
            "improvements to the above referenced Unit or the limited common facilities assigned "
            "thereto that are in violation of any provision of the Declaration unless otherwise "
            f"stated here: {stated(violations_text)}",
        ),
        ("normal", ""),
        ("normal", f"Requested by: {b(obj.requester_name)}"),
        ("normal", ""),
        ("normal", "_______________________________________          ____________________"),
        ("normal", f"For {profile.legal_name}" + " " * 30 + "Date"),
        ("normal", ""),
        (
            "small",
            "Prepared from the association's records. The board confirms the certificate "
            "contains everything 68 Pa.C.S. § 5407 requires before it is relied upon.",
        ),
    ]
    # The attached account: each open charge (D32-payments), on its own page
    # so the certificate's wording above is unchanged.
    from ledger.letters import open_charges_paragraphs

    body += [("pagebreak", ""), ("heading", f"Account of {lot.legal_label}")]
    body += open_charges_paragraphs(lot)
    return pdf.render(body, title=f"Resale certificate {obj.pk}")


def issued_certificate(obj: ResaleRequest):
    """The stored PDF given to the buyer, or None before issue."""
    from documents.attachments import attachments_for

    return attachments_for(obj).filter(label=ISSUED_CERTIFICATE_LABEL).order_by("-id").first()


@internal("ledger service")
def save_disclosures(obj: ResaleRequest, text: str, *, actor, request=None) -> None:
    require(actor, "resale.manage")
    conflicts.refuse(
        actor, obj, area="resale", action="disclosures", request=request, error=LedgerError
    )
    with transaction.atomic():
        locked = ResaleRequest.objects.select_for_update().get(pk=obj.pk)
        if locked.state not in ("submitted", "in_progress"):
            raise LedgerError("Disclosures can be changed only before the certificate is issued.")
        ResaleRequest.objects.filter(pk=obj.pk).update(other_disclosures=text[:5000])
        record_event("resale.disclosures_saved", actor=actor, request=request, target=obj)


def set_case_follow_up(case: CollectionCase, on, *, actor, request=None) -> CollectionCase:
    """Board or manager: the date to check on this collection case next."""
    _authorize(actor, "collections.manage")
    conflicts.refuse(
        actor, case, area="collection", action="follow_up", request=request, error=LedgerError
    )
    CollectionCase.objects.unscoped("follow-up").filter(pk=case.pk).update(follow_up_on=on)
    case.follow_up_on = on
    record_event(
        "followup.set",
        actor=actor,
        request=request,
        target=case,
        metadata={"kind": "collection", "object_id": case.pk, "on": on.isoformat() if on else None},
    )
    return case


# --- Late fees (decision D21: definable, automatic only when switched on) --------------


def late_fee_candidates(*, year: int, month: int, on=None) -> list[dict]:
    """Lots whose dues for this month are still open (unpaid, in whole or in
    part) on ``on``, past the grace period of the late-fee rule, and have no
    late fee for the month yet (D33-acctfix-2). Other open charges, such as a
    charge not yet due, never make the month's dues late; a credit that paid
    the dues (a prepayment) means they are not late. ``balance_cents`` is
    what is still open of that month's dues."""
    on = on or timezone.localdate()
    fee = rule("late_fee.cents")
    grace = rule("late_fee.grace_days")
    if not fee or grace is None:
        return []
    period = _period(year, month)
    out = []
    with system_access("late fee candidates"):
        assessments = LedgerEntry.objects.filter(kind=EntryKind.ASSESSMENT, period=period)
        already = set(
            LedgerEntry.objects.filter(kind=EntryKind.LATE_FEE, period=period).values_list(
                "lot_id", flat=True
            )
        )
        for entry in assessments.select_related("lot"):
            late_from = entry.posted_on + datetime.timedelta(days=int(grace) + 1)
            if on < late_from or entry.lot_id in already:
                continue
            open_cents = _allocation().state_as_of(entry.lot_id, on).open.get(entry.pk, 0)
            if open_cents <= 0:
                continue  # this month's dues are paid (or reversed)
            out.append(
                {
                    "lot": entry.lot,
                    "balance_cents": open_cents,
                    "late_from": late_from,
                    # The fee belongs with the dues it penalises (BUG-002).
                    "account_id": entry.account_id,
                }
            )
    return out


@internal("ledger service")
def post_late_fees(*, year: int, month: int, actor, request=None, on=None) -> int:
    """Post the late fee to each late lot for the month (once per lot and month).
    The board does this by button; SYSTEM only when automatic posting is on."""
    if actor is SYSTEM:
        if not rule("late_fee.auto_apply"):
            return 0
    else:
        _authorize(actor, "late_fee.post")
    if not settings.LEDGER_LATE_FEES_ENABLED:
        raise LedgerError("Late fees are switched off on this server (LEDGER_LATE_FEES_ENABLED).")
    fee, citation = rule_with_citation("late_fee.cents")
    if not fee:
        raise LedgerError(
            "Set the late fee amount (with its citation) in Community settings first."
        )
    period = _period(year, month)
    created = 0
    for row in late_fee_candidates(year=year, month=month, on=on):
        try:
            with transaction.atomic():
                LedgerEntry.objects.create(
                    lot=row["lot"],
                    account_id=row["account_id"],
                    posted_on=on or timezone.localdate(),
                    kind=EntryKind.LATE_FEE,
                    amount_cents=fee,
                    period=period,
                    description=f"Late fee {datetime.date(year, month, 1):%B %Y}"
                    + (f" ({citation})" if citation else ""),
                    posted_by=_user(actor),
                )
            created += 1
        except IntegrityError:
            continue
    record_event(
        "ledger.late_fees_posted",
        actor=_user(actor),
        request=request,
        metadata={
            "period": period,
            "lots": created,
            "amount_cents": fee,
            "automatic": actor is SYSTEM,
        },
    )
    return created


# --- Budget notices to owners (Milestone 4) ------------------------------------------


def budget_summary(budget: Budget) -> dict:
    lines = list(budget.lines.all())
    income = sum(line.annual_cents for line in lines if line.kind == "income")
    expense = sum(line.annual_cents for line in lines if line.kind != "income")
    return {"lines": lines, "income": income, "expense": expense, "net": income - expense}


def send_budget_notice(budget: Budget, *, stage: str, actor, request=None) -> dict:
    """Email owners who consented to electronic notice; list those needing
    paper. ``stage`` is "proposed" (before the meeting) or "adopted" (with the
    monthly dues). Counsel should confirm the notice period for your Bylaws."""
    require(actor, "budget.manage")
    from django.core.mail import send_mail

    from community.services import owner_notice_recipients

    if stage == "proposed":
        if budget.status != BudgetStatus.DRAFT or not budget.lines.exists():
            raise LedgerError("Send the proposed budget while it is a draft with lines.")
    elif stage == "adopted":
        if budget.status != BudgetStatus.ADOPTED:
            raise LedgerError("Adopt the budget first.")
    else:
        raise LedgerError("Unknown notice.")
    summary = budget_summary(budget)
    link = settings.PORTAL_BASE_URL + reverse("owner_budget")
    dues, cite = rule_with_citation("dues.monthly_cents")
    if stage == "proposed":
        subject = f"[{settings.SITE_NAME}] Proposed budget for {budget}"
        body = (
            f"The board has proposed the {budget}: income {format_cents(summary['income'])}, "
            f"expenses {format_cents(summary['expense'])}.\n\nSee the details:\n{link}\n"
        )
    else:
        term = charge_term().variants()
        subject = f"[{settings.SITE_NAME}] Adopted budget and {term['term']} for {budget}"
        body = (
            f"The board adopted the {budget} on {budget.adopted_on:%B %-d, %Y}.\n"
            + (
                f"Monthly {term['term']}: {format_cents(dues)}"
                + (f" ({cite})" if cite else "")
                + "\n"
                if dues
                else ""
            )
            + f"\nSee the details:\n{link}\n"
        )
    body += f"\n-- \n{settings.SITE_NAME}"
    emails, paper = owner_notice_recipients()
    for address in emails:
        send_mail(subject, body, None, [address], fail_silently=True)
    field = "proposed_notice_sent_at" if stage == "proposed" else "adopted_notice_sent_at"
    Budget.objects.filter(pk=budget.pk).update(**{field: timezone.now()})
    record_event(
        "budget.notice_sent",
        actor=actor,
        request=request,
        target=budget,
        metadata={"stage": stage, "emailed": len(emails), "paper": len(paper)},
    )
    return {"emailed": len(emails), "paper": paper}


def budget_notice_values(budget: Budget, stage: str) -> dict:
    """The budget notice's facts and default wording for a paper copy (D55),
    the same figures as the notice email. The caller checks ``mail.print``."""
    if stage not in ("proposed", "adopted"):
        raise LedgerError("Unknown notice.")
    if stage == "adopted" and budget.status != BudgetStatus.ADOPTED:
        raise LedgerError("Adopt the budget first.")
    summary = budget_summary(budget)
    dues, cite = rule_with_citation("dues.monthly_cents")
    term = charge_term().variants()
    values = {
        "budget": str(budget),
        "budget_income": format_cents(summary["income"]),
        "budget_expense": format_cents(summary["expense"]),
        "monthly_amount": (format_cents(dues) + (f" ({cite})" if cite else "")) if dues else "",
        "budget_link": settings.PORTAL_BASE_URL + reverse("owner_budget"),
        "adopted_on": (
            f"{budget.adopted_on:%B} {budget.adopted_on.day}, {budget.adopted_on.year}"
            if budget.adopted_on
            else ""
        ),
    }
    if stage == "proposed":
        subject = "Proposed budget for {budget}"
        body = (
            "The board has proposed the {budget}: income {budget_income}, expenses "
            "{budget_expense}.\n\nThe details are on the owners' portal (sign-in required): "
            "{budget_link}\n\nIf you have questions, please contact {contact}."
        )
    else:
        subject = f"Adopted budget and {term['term']} for {{budget}}"
        body = (
            "The board adopted the {budget} on {adopted_on}.\n\n"
            + (f"Monthly {term['term']}: {{monthly_amount}}\n\n" if dues else "")
            + "The details are on the owners' portal (sign-in required): {budget_link}\n\n"
            "If you have questions, please contact {contact}."
        )
    return {"values": values, "subject": subject, "body": body}


def paper_letter_templates() -> list[tuple[str, str]]:
    """Letters and certificates wording that can be mailed as a paper
    mailing (D55): the delinquency notices, (kind, name)."""
    return [
        (k, LetterKind(k).label) for k in (LetterKind.FIRST, LetterKind.SECOND, LetterKind.FINAL)
    ]


def paper_letter_template(kind: str) -> dict:
    from ledger import letters

    if kind not in dict(paper_letter_templates()):
        raise LedgerError("Unknown letter.")
    tpl = letters.template(kind)
    return {"subject": tpl.subject, "body": tpl.body}


def paper_letter_certified(kind: str) -> bool:
    """Is this wording sent certified and regular mail?"""
    from ledger import letters

    return kind in dict(paper_letter_templates()) and bool(letters.template(kind).certified)


def paper_letter_values(lot: Lot) -> dict:
    """{amount} and {days} for one home's delinquency wording (D55): the
    amount past due on the current account and the notice period."""
    from ledger import letters

    days = rule("collections.notice_days") or 10
    return {
        "amount": format_cents(max(past_due(lot.pk, None, account=CURRENT), 0)),
        "days": letters._days_text(int(days)),
    }


def owner_visible_budget():
    """The budget owners see: the adopted one for the current or latest year,
    or a proposed one the board has sent out."""
    proposed = Budget.objects.filter(
        status=BudgetStatus.DRAFT, proposed_notice_sent_at__isnull=False
    ).first()
    adopted = Budget.objects.filter(status=BudgetStatus.ADOPTED).first()
    if proposed and (adopted is None or proposed.fiscal_year > adopted.fiscal_year):
        return proposed
    return adopted


def save_letter_template(
    kind: str, *, subject: str, body: str, certified: bool, actor, request=None
):
    from ledger.models import LetterKind, LetterTemplate

    _authorize(actor, "letters.manage")
    if kind not in LetterKind.values:
        raise LedgerError("Unknown letter.")
    if not (body or "").strip():
        raise LedgerError("The letter needs some text.")
    LetterTemplate.objects.update_or_create(
        kind=kind,
        defaults={
            "subject": " ".join((subject or "").split())[:200],
            "body": body.strip()[:6000],
            "certified": bool(certified),
            "updated_by": actor,
            "updated_at": timezone.now(),
        },
    )
    record_event(
        "ledger.letter_template_saved", actor=actor, request=request, metadata={"kind": kind}
    )


def generate_notice(lot: Lot, kind: str, *, actor, request=None) -> bytes:
    """A delinquency notice (with statement) or a statement, for printing."""
    from ledger import letters

    _authorize(actor, "collections.manage")
    conflicts.refuse(actor, lot, area="collection", action="notice", request=request)
    data = letters.statement_pdf(lot) if kind == "statement" else letters.notice_pdf(lot, kind)
    record_event(
        "ledger.notice_generated",
        actor=actor,
        request=request,
        target=lot,
        metadata={"kind": kind, "lot_id": lot.pk},
    )
    return data
