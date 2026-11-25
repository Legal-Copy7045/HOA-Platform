from __future__ import annotations

import datetime
import re
from dataclasses import dataclass

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.utils import timezone

from audit.services import record_event
from community.models import CommunityProfile, Lot, Ownership
from community.rules import rule, rule_with_citation
from core import pdf
from core.money import format_cents
from core.policy import SYSTEM, require
from core.scoping import internal, system_access
from core.workflow import Transition, Workflow
from ledger.models import (
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


def upcoming_charges(balance_cents: int, *, months: int = 3, on=None) -> list[UpcomingCharge]:
    """The next monthly dues, from the dues rule. A credit on the account is
    shown being used up month by month (nothing here posts anything)."""
    amount = rule("dues.monthly_cents") or 0
    if not amount:
        return []
    on = on or timezone.localdate()
    day = min(rule("dues.due_day") or 1, 28)
    credit = -balance_cents if balance_cents < 0 else 0
    year, month = on.year, on.month
    if on.day >= day:
        month += 1
    out = []
    for _ in range(months):
        if month > 12:
            year, month = year + 1, 1
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


@internal("ledger service")
def record_payment(
    *,
    lot: Lot,
    amount_cents: int,
    received_on,
    method: str = "check",
    reference: str = "",
    actor,
    request=None,
) -> LedgerEntry:
    _authorize(actor, "ledger.manage")
    if amount_cents <= 0:
        raise LedgerError("A payment must be a positive amount.")
    if _ACCOUNT_NUMBER.search(reference.replace(" ", "").replace("-", "")):
        raise LedgerError("Enter a check number or short reference, never an account number.")
    entry = LedgerEntry.objects.create(
        lot=lot,
        posted_on=received_on,
        kind=EntryKind.PAYMENT,
        amount_cents=-amount_cents,
        description=f"Payment ({method})",
        reference=reference[:40],
        posted_by=_user(actor),
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

BUCKETS = ("current", "31_60", "61_90", "over_90")


@internal("ledger service")
def aging(lot_id: int, as_of: datetime.date | None = None) -> dict[str, int]:
    as_of = as_of or timezone.localdate()
    entries = list(LedgerEntry.objects.filter(lot_id=lot_id, posted_on__lte=as_of))
    # A reversal cancels the entry it reverses (review D2): drop both, so a
    # bounced payment brings back the original charges' ages and a reversed
    # mistaken charge does not age as if paid down the oldest debt.
    cancelled = {e.reverses_id for e in entries if e.reverses_id}
    entries = [e for e in entries if e.pk not in cancelled and not e.reverses_id]
    charges = [[e.posted_on, e.amount_cents] for e in entries if e.amount_cents > 0]
    credit = -sum(e.amount_cents for e in entries if e.amount_cents < 0)
    for charge in sorted(charges):
        applied = min(credit, charge[1])
        charge[1] -= applied
        credit -= applied
    result = dict.fromkeys(BUCKETS, 0)
    for posted_on, open_cents in charges:
        if open_cents <= 0:
            continue
        age = (as_of - posted_on).days
        bucket = (
            "current"
            if age <= 30
            else "31_60"
            if age <= 60
            else "61_90"
            if age <= 90
            else "over_90"
        )
        result[bucket] += open_cents
    result["credit"] = credit
    return result


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
    profile = CommunityProfile.get()
    since = since or datetime.date(2000, 1, 1)
    st = owner_statement(lot.pk, since)
    body = [
        ("heading", profile.legal_name),
        ("normal", "Payment reminder" if reminder else "Account statement"),
        ("normal", timezone.localdate().strftime("%B %-d, %Y")),
        ("normal", f"Lot {lot.number}, {lot.street_address}"),
        ("normal", ""),
    ]
    if st.brought_forward:
        body.append(("normal", f"Balance brought forward: {format_cents(st.brought_forward)}"))
    for e in st.entries:
        body.append(
            (
                "normal",
                f"{e.posted_on:%Y-%m-%d}  {e.description[:60]:<60}  {format_cents(e.amount_cents)}",
            )
        )
    label = "Amount due" if st.balance > 0 else "Credit on account"
    body += [("normal", ""), ("heading", f"{label}: {format_cents(abs(st.balance))}")]
    if reminder:
        body += [
            ("normal", ""),
            (
                "normal",
                "Our records show this balance is overdue. Please "
                "pay, or contact the board to discuss a payment plan. If you have already "
                "paid, thank you, and please let us know.",
            ),
        ]
    return pdf.render(body, title=f"Statement lot {lot.number}")


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
            "payment_plan",
            {"open", "reminder_sent", "second_reminder"},
            "payment_plan",
            "collections.manage",
            "Payment plan agreed",
            note_required=True,
        ),
        Transition(
            "refer",
            {"second_reminder", "payment_plan"},
            "referred",
            "collections.refer",
            "Referred for legal collection (board)",
            note_required=True,
        ),
        Transition(
            "close",
            {"open", "reminder_sent", "second_reminder", "payment_plan", "referred"},
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
    """Draft content; the board must confirm it meets 68 Pa.C.S. § 5407 with counsel."""
    from arc.models import ArcRequest
    from compliance.models import Violation
    from governance.models import Resolution

    profile = CommunityProfile.get()
    with system_access("resale certificate"):
        lot = obj.lot
        owners = [
            o.person.display_name
            for o in Ownership.objects.active().filter(lot=lot).select_related("person")
        ]
        open_violations = (
            Violation.objects.filter(lot=lot)
            .exclude(state__in=["closed", "dismissed", "cured"])
            .count()
        )
        open_arc = (
            ArcRequest.objects.filter(lot=lot)
            .exclude(state__in=["completed", "withdrawn", "denied", "appeal_decided"])
            .count()
        )
    dues, dues_cite = rule_with_citation("dues.monthly_cents")
    bal = balance(lot.pk)
    policies = Resolution.objects.filter(kind="policy", superseded_by__isnull=True).count()
    body = [
        ("heading", profile.legal_name),
        ("normal", "Resale certificate"),
        ("normal", timezone.localdate().strftime("%B %-d, %Y")),
        ("normal", f"Lot {lot.number}, {lot.street_address}"),
        ("normal", f"Record owner(s): {', '.join(owners) or 'not recorded'}"),
        ("normal", f"Requested by: {obj.requester_name}"),
        ("normal", ""),
        ("heading", "Assessments and account"),
        (
            "normal",
            f"Current assessment: {format_cents(dues)} per month"
            + (f" ({dues_cite})" if dues_cite else ""),
        ),
        (
            "normal",
            f"Account balance today: {format_cents(bal)}"
            + (" owed" if bal > 0 else " (credit)" if bal < 0 else ""),
        ),
        ("normal", ""),
        ("heading", "Open matters for this lot"),
        ("normal", f"Open covenant violations: {open_violations}"),
        ("normal", f"Open architectural requests: {open_arc}"),
        ("normal", f"Rules and policies in force: {policies} (copies available on request)"),
        ("normal", ""),
        ("heading", "Other disclosures"),
        ("normal", obj.other_disclosures or "None provided by the board."),
        ("normal", ""),
        (
            "small",
            "Prepared from the association's records. The board must confirm this "
            "certificate contains everything required by the Uniform Planned "
            "Community Act (68 Pa.C.S. § 5407) before it is relied upon.",
        ),
    ]
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
