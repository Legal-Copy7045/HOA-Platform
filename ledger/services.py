from __future__ import annotations

import datetime
import re
from dataclasses import dataclass

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.urls import reverse
from django.utils import timezone

from audit.services import record_event
from community.models import CommunityProfile, Lot
from community.rules import rule, rule_with_citation
from core import pdf
from core.money import format_cents
from core.policy import SYSTEM, require
from core.scoping import internal, system_access
from core.workflow import Transition, Workflow
from ledger.models import (
    CHARGE_KINDS,
    Budget,
    BudgetLine,
    BudgetStatus,
    CollectionCase,
    EntryKind,
    IncomeSource,
    LedgerEntry,
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


# --- Balances -------------------------------------------------------------------------


@internal("ledger service")
def balance(lot_id: int, as_of: datetime.date | None = None) -> int:
    qs = LedgerEntry.objects.filter(lot_id=lot_id)
    if as_of:
        qs = qs.filter(posted_on__lte=as_of)
    return qs.aggregate(s=Sum("amount_cents"))["s"] or 0


def past_due(lot_id: int, today: datetime.date | None = None) -> int:
    """What is actually overdue: charges dated before today, less every
    payment and credit so far. Assessments auto-posted for a due date later
    this month do not count yet (BUG-006)."""
    from django.db.models import Q

    today = today or timezone.localdate()
    qs = (
        LedgerEntry.objects.unscoped("past due")
        .filter(lot_id=lot_id, posted_on__lte=today)
        .filter(~Q(kind__in=CHARGE_KINDS) | Q(posted_on__lt=today))
    )
    return max(qs.aggregate(s=Sum("amount_cents"))["s"] or 0, 0)


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
def owner_statement(lot_id: int, since: datetime.date) -> OwnerStatement:
    """What a current owner sees: entries from their ownership start, with any
    earlier balance shown as one 'brought forward' line (not the detail)."""
    forward = (
        LedgerEntry.objects.filter(lot_id=lot_id, posted_on__lt=since).aggregate(
            s=Sum("amount_cents")
        )["s"]
        or 0
    )
    entries = list(LedgerEntry.objects.filter(lot_id=lot_id, posted_on__gte=since))
    return OwnerStatement(forward, entries, forward + sum(e.amount_cents for e in entries))


@internal("ledger service")
def period_statement(
    lot_id: int, start: datetime.date, end: datetime.date | None
) -> OwnerStatement:
    """What a former owner sees: only the entries posted while they owned the
    lot, with the balance brought forward from before their time and the
    balance on the day they sold. Nothing from after the sale."""
    forward = (
        LedgerEntry.objects.filter(lot_id=lot_id, posted_on__lt=start).aggregate(
            s=Sum("amount_cents")
        )["s"]
        or 0
    )
    entries = LedgerEntry.objects.filter(lot_id=lot_id, posted_on__gte=start)
    if end is not None:
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
                    f"Monthly dues {_period(year, month)}",
                    amount,
                    covered,
                )
            )
        month += 1
    return out


def _period(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"


@internal("ledger service")
def post_monthly_assessments(*, year: int, month: int, actor, request=None) -> int:
    _authorize(actor, "ledger.manage")
    amount, citation = rule_with_citation("dues.monthly_cents")
    if not amount:
        raise LedgerError(
            "Set the monthly dues amount (with its citation) in Community settings first."
        )
    day = min(rule("dues.due_day") or 1, 28)
    posted_on = datetime.date(year, month, day)
    period = _period(year, month)
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
                    description=f"Assessment {posted_on:%B %Y}"
                    + (f" ({citation})" if citation else ""),
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


def _check_apply_to(lot, apply_to) -> list[int]:
    """Charge ids chosen for a payment: this lot's open charges only."""
    if not apply_to:
        return []
    from ledger import allocation

    open_ids = {c.entry.pk for c in allocation.open_charges(lot.pk)}
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


def _post_payment(lot, *, amount_cents, received_on, method, reference, apply_to, actor):
    entry = LedgerEntry(
        lot=lot,
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
    actor,
    request=None,
) -> LedgerEntry:
    """A payment received by check, cash, ACH... ``apply_to``: charges the
    payer chose, in order; the rest follows the application rule.

    Online (Stripe) payments are never entered here: only the Stripe sync
    records them, from what Stripe itself reports (D32-payments)."""
    _authorize(actor, "ledger.manage")
    if (method or "").strip().lower() in ONLINE_METHODS:
        raise LedgerError("Online payments are recorded automatically from Stripe.")
    if amount_cents <= 0:
        raise LedgerError("A payment must be a positive amount.")
    if _ACCOUNT_NUMBER.search(reference.replace(" ", "").replace("-", "")):
        raise LedgerError("Enter a check number or short reference, never an account number.")
    if _duplicate_reference(lot, reference):
        raise LedgerError(f"Payment {reference[:40]} for this home is already recorded.")
    entry = _post_payment(
        lot,
        amount_cents=amount_cents,
        received_on=received_on,
        method=method,
        reference=reference,
        apply_to=_check_apply_to(lot, apply_to),
        actor=actor,
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
    *, lot: Lot, amount_cents: int, received_on, payment_intent_id: str, apply_to, actor
) -> LedgerEntry:
    """Only for payments.sync, after Stripe confirmed the payment (actor is
    SYSTEM; no form or view reaches this)."""
    if actor is not SYSTEM:
        raise PermissionError("Online payments are recorded only by the Stripe sync.")
    if amount_cents <= 0 or not payment_intent_id:
        raise LedgerError("An online payment needs an amount and its Stripe reference.")
    open_ids = {c.entry.pk for c in _allocation().open_charges(lot.pk)}
    entry = _post_payment(
        lot,
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
    *, lot: Lot, kind: str, amount_cents: int, description: str, posted_on=None, actor, request=None
) -> LedgerEntry:
    _authorize(actor, "ledger.manage")
    if kind not in (EntryKind.OTHER_CHARGE, EntryKind.LATE_FEE, EntryKind.RESALE_FEE):
        raise LedgerError("Use the assessment posting for dues.")
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
    *, lot: Lot, amount_cents: int, description: str, actor, request=None
) -> LedgerEntry:
    _authorize(actor, "ledger.manage")
    if amount_cents <= 0 or not description.strip():
        raise LedgerError("A credit needs a positive amount and a reason.")
    entry = LedgerEntry.objects.create(
        lot=lot,
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


@internal("ledger service")
def reverse_entry(entry: LedgerEntry, *, reason: str, actor, request=None) -> LedgerEntry:
    """Corrections are new, opposite entries (CLAUDE.md rule 5)."""
    _authorize(actor, "ledger.manage")
    if not reason.strip():
        raise LedgerError("Give the reason for the reversal.")
    if entry.kind == EntryKind.REVERSAL:
        raise LedgerError("A reversal cannot itself be reversed; post a new entry instead.")
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


def _open_charges(lot_id: int, as_of: datetime.date) -> tuple[list[list], int]:
    """Charges still open on ``as_of`` as [posted_on, open_cents], oldest
    first, and the credit not applied to any charge. Open amounts come from
    the payment applications (open-item accounting, D32-payments): a payment
    pays the charges it was applied to, a reversal reopens them."""
    return _allocation().open_items_as_of(lot_id, as_of)


def aging(lot_id: int, as_of: datetime.date | None = None) -> dict[str, int]:
    as_of = as_of or timezone.localdate()
    charges, credit = _open_charges(lot_id, as_of)
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


def amount_due(lot_id: int, today: datetime.date | None = None) -> AmountDue:
    """What is owed on a lot and by when ("Due by <date>": the oldest unpaid
    charge's date). Shared by My account and the dashboard's balance to-do."""
    today = today or timezone.localdate()
    owed = balance(lot_id)
    if owed <= 0:
        return AmountDue(0, None, False)
    open_ = _allocation().open_charges(lot_id)
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
    for e in statement.entries:
        running += e.amount_cents
        rows.append(
            StatementRow(
                e.posted_on,
                e.description,
                max(e.amount_cents, 0),
                max(-e.amount_cents, 0),
                running,
            )
        )
    return rows


@internal("ledger service")
def delinquent_lots(min_age_days: int = 30, as_of=None) -> list[tuple[Lot, int, dict]]:
    out = []
    with system_access("delinquency list"):
        lots = {lot.pk: lot for lot in Lot.objects.all()}
    for lot_id, bal in balances().items():
        if bal <= 0:
            continue
        a = aging(lot_id, as_of)
        overdue = a["31_60"] + a["61_90"] + a["over_90"] if min_age_days >= 30 else bal
        if overdue > 0:
            out.append((lots[lot_id], bal, a))
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
    if CollectionCase.objects.filter(lot=lot).exclude(state="closed").exists():
        raise LedgerError("This lot already has an open collection case.")
    try:
        with transaction.atomic():
            case = CollectionCase.objects.create(lot=lot, opened_by=actor, notes=notes)
    except IntegrityError as exc:
        raise LedgerError("This lot already has an open collection case.") from exc
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
                    kind__in=kinds[line.income_source], posted_on__year=year, posted_on__month=month
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
        raise LedgerError("Owners can request a certificate only for their own lot.")
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
    bal = balance(lot.pk, as_of=timezone.localdate())
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
            f'"Association") represents the following with respect to {b(lot.street_address)} '
            f'(lot {b(lot.number)}) (hereinafter "Unit"), as of {b(today.strftime("%B %-d, %Y"))}.',
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

    body += [("pagebreak", ""), ("heading", f"Account of lot {lot.number}")]
    body += open_charges_paragraphs(lot)
    return pdf.render(body, title=f"Resale certificate {obj.pk}")


def issued_certificate(obj: ResaleRequest):
    """The stored PDF given to the buyer, or None before issue."""
    from documents.attachments import attachments_for

    return attachments_for(obj).filter(label=ISSUED_CERTIFICATE_LABEL).order_by("-id").first()


@internal("ledger service")
def save_disclosures(obj: ResaleRequest, text: str, *, actor, request=None) -> None:
    require(actor, "resale.manage")
    with transaction.atomic():
        locked = ResaleRequest.objects.select_for_update().get(pk=obj.pk)
        if locked.state not in ("submitted", "in_progress"):
            raise LedgerError("Disclosures can be changed only before the certificate is issued.")
        ResaleRequest.objects.filter(pk=obj.pk).update(other_disclosures=text[:5000])
        record_event("resale.disclosures_saved", actor=actor, request=request, target=obj)


def set_case_follow_up(case: CollectionCase, on, *, actor, request=None) -> CollectionCase:
    """Board or manager: the date to check on this collection case next."""
    _authorize(actor, "collections.manage")
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
    """Lots past the grace period for this month's dues that still owe
    money and have no late fee for the month yet."""
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
        owed = balances()
        for entry in assessments.select_related("lot"):
            late_from = entry.posted_on + datetime.timedelta(days=int(grace) + 1)
            if on < late_from or entry.lot_id in already:
                continue
            if owed.get(entry.lot_id, 0) <= 0:
                continue  # paid up, or in credit (prepayment covers it)
            out.append(
                {"lot": entry.lot, "balance_cents": owed[entry.lot_id], "late_from": late_from}
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
        subject = f"[{settings.SITE_NAME}] Adopted budget and dues for {budget}"
        body = (
            f"The board adopted the {budget} on {budget.adopted_on:%B %-d, %Y}.\n"
            + (
                f"Monthly dues: {format_cents(dues)}" + (f" ({cite})" if cite else "") + "\n"
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
    data = letters.statement_pdf(lot) if kind == "statement" else letters.notice_pdf(lot, kind)
    record_event(
        "ledger.notice_generated",
        actor=actor,
        request=request,
        target=lot,
        metadata={"kind": kind, "lot_id": lot.pk},
    )
    return data
