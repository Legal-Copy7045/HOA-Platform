"""Money tools (Milestone 4 part 3): accounting export, deposit batches, bank
reconciliation, 1099 vendor totals, courtesy reminders and the collections
letter sequence.

Everything here is integer cents and append-only. Nothing is sent to owners
without the board: courtesy reminders run only when the board sets the
reminder day, and delinquency notices are generated for printing, never
mailed by the portal.
"""

from __future__ import annotations

import csv
import datetime
import io

from django.conf import settings
from django.db import transaction
from django.db.models import Sum
from django.urls import reverse
from django.utils import timezone

from audit.services import record_event
from comms.notify import send_now
from community.models import Lot
from community.rules import rule
from core.money import format_cents
from core.policy import SYSTEM, require
from core.scoping import system_access
from ledger.models import (
    AccountingSetup,
    BankReconciliation,
    CollectionCase,
    CollectionLetter,
    CourtesyReminder,
    DepositBatch,
    DepositItem,
    EntryKind,
    LedgerEntry,
    LetterKind,
    ReconciledItem,
)


class AccountingError(ValueError):
    pass


def _user(actor):
    return None if actor is SYSTEM else actor


def _cents(value) -> str:
    return f"{value // 100}.{value % 100:02d}" if value >= 0 else f"-{_cents(-value)}"


# --- Accounting setup -------------------------------------------------------------------


def save_setup(*, gl_accounts: str, opening_cash_cents: int, opening_cash_on, actor, request=None):
    require(actor, "accounting.manage")
    setup = AccountingSetup.get()
    setup.gl_accounts = (gl_accounts or "").strip()[:4000]
    setup.opening_cash_cents = int(opening_cash_cents)
    setup.opening_cash_on = opening_cash_on
    setup.updated_by = _user(actor)
    setup.updated_at = timezone.now()
    setup.save()
    record_event("accounting.setup_saved", actor=_user(actor), request=request, metadata={})
    return setup


# --- Accounting export -------------------------------------------------------------------

EXPORT_HEADER = ["Date", "Journal", "Reference", "Account", "Debit", "Credit", "Memo"]


def journal_rows(start: datetime.date, end: datetime.date) -> list[list[str]]:
    """Double-entry journal lines for the period, for import into the
    accounting package (QuickBooks, Xero...). Owner charges and payments are
    identified by lot number only, never by name. Vendor payments are by
    vendor and expense category code."""
    from operations.models import InvoicePayment

    gl = AccountingSetup.get().accounts()
    rows: list[list[str]] = []

    def pair(date, journal, ref, debit_acct, credit_acct, cents, memo):
        if cents < 0:  # a reversal: swap sides
            debit_acct, credit_acct, cents = credit_acct, debit_acct, -cents
        amount = _cents(cents)
        rows.append([date.isoformat(), journal, ref, debit_acct, amount, "", memo])
        rows.append([date.isoformat(), journal, ref, credit_acct, "", amount, memo])

    with system_access("accounting export"):
        entries = list(
            LedgerEntry.objects.filter(posted_on__range=(start, end))
            .select_related("lot", "reverses")
            .order_by("posted_on", "id")
        )
        payments = list(
            InvoicePayment.objects.filter(paid_on__range=(start, end))
            .select_related("invoice__vendor", "invoice__category")
            .order_by("paid_on", "id")
        )
    for e in entries:
        kind = e.reverses.kind if e.kind == EntryKind.REVERSAL and e.reverses else e.kind
        memo = f"Lot {e.lot.number}: {e.get_kind_display()}"
        ref = f"L{e.pk}"
        if kind == EntryKind.PAYMENT:
            # A payment is a negative entry: debit cash, credit receivables.
            pair(
                e.posted_on,
                "Owner payments",
                ref,
                gl["cash"],
                gl["receivable"],
                -e.amount_cents,
                memo,
            )
        elif kind == EntryKind.CREDIT:
            pair(
                e.posted_on,
                "Owner credits",
                ref,
                gl["credit"],
                gl["receivable"],
                -e.amount_cents,
                memo,
            )
        else:
            income = gl.get(kind, gl["other_charge"])
            pair(e.posted_on, "Owner charges", ref, gl["receivable"], income, e.amount_cents, memo)
    for p in payments:
        inv = p.invoice
        expense = f"{inv.category.code} {inv.category.name}".strip()
        memo = f"{inv.vendor.name} invoice {inv.number}"[:120]
        pair(p.paid_on, "Vendor payments", f"P{p.pk}", expense, gl["cash"], p.amount_cents, memo)
    return rows


def export_csv(start, end, *, actor, request=None) -> bytes:
    require(actor, "finance.view")
    if end < start:
        raise AccountingError("The end date is before the start.")
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(EXPORT_HEADER)
    rows = journal_rows(start, end)
    for row in rows:
        # Spreadsheet formula injection: text cells never start with = + - @
        # (the debit and credit columns are plain positive numbers).
        writer.writerow(
            [c if i in (4, 5) or str(c)[:1] not in "=+-@" else "'" + c for i, c in enumerate(row)]
        )
    record_event(
        "accounting.exported",
        actor=_user(actor),
        request=request,
        metadata={"start": start.isoformat(), "end": end.isoformat(), "rows": len(rows)},
    )
    return buf.getvalue().encode()


# --- Deposit batches ----------------------------------------------------------------------


def undeposited_payments():
    """Owner payments not in a (non-voided) deposit yet."""
    with system_access("undeposited payments"):
        deposited = (
            DepositItem.objects.values("entry_id")
            .annotate(total=Sum("amount_cents"))
            .exclude(total=0)
            .values_list("entry_id", flat=True)
        )
        return list(
            LedgerEntry.objects.filter(kind=EntryKind.PAYMENT, reversed_by__isnull=True)
            .exclude(pk__in=list(deposited))
            .select_related("lot")
            .order_by("posted_on", "id")
        )


def create_deposit(*, entry_ids, deposited_on, bank_reference="", actor, request=None):
    require(actor, "accounting.manage")
    ids = {int(i) for i in entry_ids}
    if not ids:
        raise AccountingError("Choose the payments in this deposit.")
    if deposited_on > timezone.localdate():
        raise AccountingError("A deposit date cannot be in the future.")
    with transaction.atomic():
        AccountingSetup.objects.select_for_update().get(pk=AccountingSetup.get().pk)
        available = {e.pk: e for e in undeposited_payments() if e.pk in ids}
        if set(available) != ids:
            raise AccountingError("Some of those payments are already deposited or not payments.")
        total = sum(-e.amount_cents for e in available.values())
        batch = DepositBatch.objects.create(
            deposited_on=deposited_on,
            bank_reference=" ".join((bank_reference or "").split())[:40],
            total_cents=total,
            created_by=actor,
        )
        for e in available.values():
            DepositItem.objects.create(batch=batch, entry=e, amount_cents=-e.amount_cents)
    record_event(
        "accounting.deposit_created",
        actor=actor,
        request=request,
        target=batch,
        metadata={"count": len(ids), "total_cents": total},
    )
    return batch


def void_deposit(batch: DepositBatch, *, actor, request=None) -> DepositBatch:
    """Reverse a mistaken deposit: its payments become undeposited again."""
    require(actor, "accounting.manage")
    with transaction.atomic():
        batch = DepositBatch.objects.select_for_update().get(pk=batch.pk)
        if batch.voids_id or DepositBatch.objects.filter(voids=batch).exists():
            raise AccountingError("This deposit is already voided (or is a void).")
        if ReconciledItem.objects.filter(kind="deposit", object_id=batch.pk).exists():
            raise AccountingError("This deposit is on a reconciled statement.")
        void = DepositBatch.objects.create(
            deposited_on=timezone.localdate(),
            bank_reference=f"VOID {batch.pk}",
            total_cents=-batch.total_cents,
            voids=batch,
            created_by=actor,
        )
        with system_access("void deposit items"):
            for item in DepositItem.objects.filter(batch=batch):
                DepositItem.objects.create(
                    batch=void, entry_id=item.entry_id, amount_cents=-item.amount_cents
                )
    record_event(
        "accounting.deposit_voided", actor=actor, request=request, target=batch, metadata={}
    )
    return void


# --- Bank reconciliation ----------------------------------------------------------------


def _cleared_ids(kind: str) -> set[int]:
    return set(ReconciledItem.objects.filter(kind=kind).values_list("object_id", flat=True))


def book_balance(as_of: datetime.date) -> int:
    """Opening cash + deposits - vendor payments, through ``as_of``."""
    from operations.models import InvoicePayment

    setup = AccountingSetup.get()
    since = setup.opening_cash_on or datetime.date(1900, 1, 1)
    deposits = (
        DepositBatch.objects.filter(deposited_on__gt=since, deposited_on__lte=as_of).aggregate(
            t=Sum("total_cents")
        )["t"]
        or 0
    )
    paid = (
        InvoicePayment.objects.filter(paid_on__gt=since, paid_on__lte=as_of).aggregate(
            t=Sum("amount_cents")
        )["t"]
        or 0
    )
    return setup.opening_cash_cents + deposits - paid


def uncleared(as_of: datetime.date) -> dict:
    """Deposits and vendor payments through ``as_of`` not yet on a statement."""
    from operations.models import InvoicePayment

    setup = AccountingSetup.get()
    since = setup.opening_cash_on or datetime.date(1900, 1, 1)
    deposits = [
        d
        for d in DepositBatch.objects.filter(deposited_on__gt=since, deposited_on__lte=as_of)
        if d.pk not in _cleared_ids("deposit")
    ]
    payments = [
        p
        for p in InvoicePayment.objects.filter(
            paid_on__gt=since, paid_on__lte=as_of
        ).select_related("invoice__vendor")
        if p.pk not in _cleared_ids("payment")
    ]
    return {"deposits": deposits, "payments": payments}


def reconcile(
    *,
    statement_date,
    statement_balance_cents: int,
    cleared_deposit_ids,
    cleared_payment_ids,
    note: str = "",
    actor,
    request=None,
) -> BankReconciliation:
    """Record a statement: the ticked items clear; the rest are outstanding.
    The difference should be zero; a non-zero one is recorded as found."""
    require(actor, "accounting.manage")
    with transaction.atomic():
        AccountingSetup.objects.select_for_update().get(pk=AccountingSetup.get().pk)
        open_items = uncleared(statement_date)
        deposits = {d.pk: d for d in open_items["deposits"]}
        payments = {p.pk: p for p in open_items["payments"]}
        dep_ids = {int(i) for i in cleared_deposit_ids}
        pay_ids = {int(i) for i in cleared_payment_ids}
        if not dep_ids <= set(deposits) or not pay_ids <= set(payments):
            raise AccountingError("Some ticked items are not open on this date.")
        outstanding_dep = sum(d.total_cents for k, d in deposits.items() if k not in dep_ids)
        outstanding_pay = sum(p.amount_cents for k, p in payments.items() if k not in pay_ids)
        book = book_balance(statement_date)
        adjusted_bank = statement_balance_cents + outstanding_dep - outstanding_pay
        rec = BankReconciliation.objects.create(
            statement_date=statement_date,
            statement_balance_cents=statement_balance_cents,
            book_balance_cents=book,
            outstanding_deposits_cents=outstanding_dep,
            outstanding_payments_cents=outstanding_pay,
            difference_cents=adjusted_bank - book,
            note=(note or "").strip()[:2000],
            reconciled_by=actor,
        )
        for k in dep_ids:
            ReconciledItem.objects.create(
                reconciliation=rec,
                kind="deposit",
                object_id=k,
                amount_cents=deposits[k].total_cents,
            )
        for k in pay_ids:
            ReconciledItem.objects.create(
                reconciliation=rec,
                kind="payment",
                object_id=k,
                amount_cents=payments[k].amount_cents,
            )
    record_event(
        "accounting.reconciled",
        actor=actor,
        request=request,
        target=rec,
        metadata={"difference_cents": rec.difference_cents},
    )
    return rec


# --- 1099 vendor totals -------------------------------------------------------------------


def vendor_1099_totals(year: int) -> list[dict]:
    """Payments to each vendor in a calendar year, flagged at the threshold
    (a rule, since the IRS changes it). Corporations are generally exempt:
    the treasurer decides; this is a worksheet, not a filing."""
    from operations.models import InvoicePayment, Vendor

    threshold = rule("accounting.1099_threshold_cents") or 0
    totals = dict(
        InvoicePayment.objects.filter(paid_on__year=year)
        .values("invoice__vendor_id")
        .annotate(t=Sum("amount_cents"))
        .values_list("invoice__vendor_id", "t")
    )
    out = []
    for vendor in Vendor.objects.filter(pk__in=totals).order_by("name"):
        total = totals[vendor.pk] or 0
        out.append(
            {
                "vendor": vendor,
                "total_cents": total,
                "over": bool(threshold) and total >= threshold,
                "w9": vendor.w9_on_file,
            }
        )
    return out


def vendor_1099_csv(year: int, *, actor, request=None) -> bytes:
    require(actor, "finance.view")
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Vendor", "Paid in year", "At or over threshold", "W-9 on file"])
    for row in vendor_1099_totals(year):
        name = row["vendor"].name
        if name[:1] in "=+-@":
            name = "'" + name
        writer.writerow(
            [
                name,
                _cents(row["total_cents"]),
                "yes" if row["over"] else "",
                "yes" if row["w9"] else "no",
            ]
        )
    record_event(
        "accounting.1099_exported", actor=_user(actor), request=request, metadata={"year": year}
    )
    return buf.getvalue().encode()


# --- Courtesy payment reminders ------------------------------------------------------------


def send_courtesy_reminders(*, today=None) -> int:
    """On the board's chosen day of the month, a courtesy email to owners with
    a past-due balance. Off unless the board sets the day. Once per lot per
    month; never to the board's legal-notice standard (that is the letters)."""
    from community.models import Ownership
    from ledger.services import balance

    day = rule("collections.courtesy_reminder_day") or 0
    today = today or timezone.localdate()
    if not day or today.day < int(day):
        return 0
    period = today.strftime("%Y-%m")
    sent = 0
    link = settings.PORTAL_BASE_URL + reverse("my_account")
    with system_access("courtesy reminders"):
        for lot in Lot.objects.order_by("number"):
            owed = balance(lot.pk)
            if owed <= 0 or CourtesyReminder.objects.filter(lot=lot, period=period).exists():
                continue
            emails = {
                o.person.user.email
                for o in Ownership.objects.active().filter(lot=lot).select_related("person__user")
                if o.person.user_id and o.person.user.is_active
            }
            if not emails:
                continue
            CourtesyReminder.objects.create(lot=lot, period=period, amount_cents=owed)
            send_now(
                emails,
                subject=f"[{settings.SITE_NAME}] A friendly reminder about your account",
                body=(
                    f"Our records show {format_cents(owed)} past due on your account. If you have "
                    "already paid, thank you, and please disregard this note.\n\n"
                    f"See your account and pay online here:\n{link}\n\n-- \n{settings.SITE_NAME}"
                ),
            )
            sent += 1
    if sent:
        record_event("collections.courtesy_sent", actor=None, metadata={"count": sent})
    return sent


# --- Collections letter sequence ----------------------------------------------------------

SEQUENCE = [LetterKind.FIRST, LetterKind.SECOND, LetterKind.FINAL]
_TRANSITION_FOR = {
    LetterKind.FIRST: "send_reminder",
    LetterKind.SECOND: "send_second_reminder",
    LetterKind.FINAL: "send_final_notice",
}


def next_letter(case: CollectionCase) -> str | None:
    sent = set(
        CollectionLetter.objects.unscoped("case letters")
        .filter(case=case)
        .values_list("kind", flat=True)
    )
    for kind in SEQUENCE:
        if kind not in sent:
            return kind
    return None


def letter_due_on(case: CollectionCase) -> datetime.date | None:
    """When the next letter is due: at once for the first, then the notice
    period after the previous one."""
    last = (
        CollectionLetter.objects.unscoped("case letters").filter(case=case).order_by("-id").first()
    )
    if last is None:
        return case.opened_at.date()
    days = int(rule("collections.notice_days") or 10)
    return last.generated_on + datetime.timedelta(days=days)


def generate_case_letter(case: CollectionCase, *, actor, request=None) -> tuple[bytes, str]:
    """The next notice in the sequence, as a PDF to print and mail; logged,
    and the case moves to the matching step."""
    from ledger import letters
    from ledger.services import COLLECTION, balance

    require(actor, "collections.manage")
    if case.state in ("closed", "referred"):
        raise AccountingError("This case is closed or referred.")
    kind = next_letter(case)
    if kind is None:
        raise AccountingError("All three notices have been generated; the board decides next.")
    owed = balance(case.lot_id)
    if owed <= 0:
        raise AccountingError("This account is not past due.")
    data = letters.notice_pdf(case.lot, kind)
    with transaction.atomic():
        CollectionLetter.objects.create(case=case, kind=kind, amount_cents=owed, generated_by=actor)
        action = _TRANSITION_FOR[kind]
        if action in [t.action for t in COLLECTION.available(case, actor)]:
            COLLECTION.apply(case, action, actor=actor, request=request)
    record_event(
        "ledger.notice_generated",
        actor=actor,
        request=request,
        target=case.lot,
        metadata={"kind": kind, "lot_id": case.lot_id},
    )
    return data, kind


def letters_due(today=None) -> list[dict]:
    """Open cases whose next notice is due (for the collections page and My items)."""
    today = today or timezone.localdate()
    out = []
    with system_access("letters due"):
        cases = list(
            CollectionCase.objects.exclude(
                state__in=("closed", "referred", "payment_plan")
            ).select_related("lot")
        )
    for case in cases:
        kind = next_letter(case)
        due = letter_due_on(case)
        if kind and due and due <= today:
            out.append({"case": case, "kind": kind, "due_on": due})
    return out
