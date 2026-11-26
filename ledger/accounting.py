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
from dataclasses import dataclass

from django.conf import settings
from django.db import transaction
from django.db.models import Sum
from django.urls import reverse
from django.utils import timezone

from audit.services import record_event
from comms.notify import send_now
from community.models import Lot
from community.rules import rule
from core.lists import safe_cell
from core.money import format_cents
from core.policy import SYSTEM, require
from core.scoping import system_access
from ledger.accounts import CURRENT
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

# One labeled input per account key on the setup form (UX review). Storage
# stays the "key = account" text in AccountingSetup.gl_accounts.
GL_LABELS = {
    "cash": "Bank (operating cash)",
    "receivable": "Owner receivables",
    "assessment": "Dues (assessment) income",
    "late_fee": "Late fee income",
    "resale_fee": "Resale certificate fees",
    "other_charge": "Other owner charges",
    "credit": "Owner credits and adjustments",
    # Online payments (D32-payments)
    "stripe_clearing": "Stripe clearing (balance at Stripe)",
    "card_surcharge": "Credit card surcharge income",
    "processing_fees": "Payment processing fees",
}


def gl_rows(setup: AccountingSetup) -> list[dict]:
    accounts = setup.accounts()
    return [
        {"key": key, "label": label, "value": accounts.get(key, "")}
        for key, label in GL_LABELS.items()
    ]


def gl_text(setup: AccountingSetup, values: dict[str, str]) -> str:
    """The stored text from one value per key. A blank box keeps the default;
    keys the form does not show are kept as they were."""
    accounts = setup.accounts()
    lines = []
    for key in GL_LABELS:
        value = " ".join((values.get(key) or "").replace("=", " ").split())[:80]
        if value:
            lines.append(f"{key} = {value}")
    lines += [f"{k} = {v}" for k, v in accounts.items() if k not in GL_LABELS]
    return "\n".join(lines)


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

# D40: owners appear by address in the memo; "Lot number" (last) is for the
# accountant's matching to the plat and the register.
EXPORT_HEADER = [
    "Date",
    "Journal",
    "Reference",
    "Account",
    "Debit",
    "Credit",
    "Memo",
    "Lot number",
]


def journal_rows(start: datetime.date, end: datetime.date) -> list[list[str]]:
    """Double-entry journal lines for the period, for import into the
    accounting package (QuickBooks, Xero...). Owner charges and payments are
    identified by address and lot number only, never by name. Vendor payments are by
    vendor and expense category code."""
    from operations.models import InvoicePayment

    gl = AccountingSetup.get().accounts()
    rows: list[list[str]] = []

    def pair(date, journal, ref, debit_acct, credit_acct, cents, memo, lot=None):
        if cents < 0:  # a reversal: swap sides
            debit_acct, credit_acct, cents = credit_acct, debit_acct, -cents
        amount = _cents(cents)
        number = str(lot.number) if lot is not None else ""
        rows.append([date.isoformat(), journal, ref, debit_acct, amount, "", memo, number])
        rows.append([date.isoformat(), journal, ref, credit_acct, "", amount, memo, number])

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
    from payments.services import journal_items, online_entry_ids

    online = online_entry_ids()
    for e in entries:
        kind = e.reverses.kind if e.kind == EntryKind.REVERSAL and e.reverses else e.kind
        memo = f"{e.lot.label}: {e.get_kind_display()}"
        ref = f"L{e.pk}"
        if kind == EntryKind.PAYMENT:
            # A payment is a negative entry: debit cash, credit receivables.
            # Online payments sit at Stripe until the payout reaches the bank.
            is_online = e.pk in online or e.reverses_id in online
            pair(
                e.posted_on,
                "Owner payments",
                ref,
                gl["stripe_clearing"] if is_online else gl["cash"],
                gl["receivable"],
                -e.amount_cents,
                memo + (" (online)" if is_online else ""),
                e.lot,
            )
        elif kind == EntryKind.REFUND:
            # Part of an online payment refunded at Stripe (D33-acctfix-5):
            # the owner owes it again; the money left Stripe clearing.
            pair(
                e.posted_on,
                "Owner payments",
                ref,
                gl["receivable"],
                gl["stripe_clearing"],
                e.amount_cents,
                memo + " (online)",
                e.lot,
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
                e.lot,
            )
        else:
            income = gl.get(kind, gl["other_charge"])
            pair(
                e.posted_on,
                "Owner charges",
                ref,
                gl["receivable"],
                income,
                e.amount_cents,
                memo,
                e.lot,
            )
    stripe = journal_items(start, end)
    for day, ref, cents, lot in stripe["surcharges"]:
        memo = f"{lot.label}: credit card surcharge"
        pair(
            day,
            "Online payments",
            ref,
            gl["stripe_clearing"],
            gl["card_surcharge"],
            cents,
            memo,
            lot,
        )
    for day, ref, cents, lot in stripe["surcharge_refunds"]:
        memo = f"{lot.label}: surcharge refunded"
        pair(
            day,
            "Online payments",
            ref,
            gl["card_surcharge"],
            gl["stripe_clearing"],
            cents,
            memo,
            lot,
        )
    for day, ref, cents, kind in stripe["fees"]:
        memo = f"Stripe fee ({kind})"
        pair(day, "Online payments", ref, gl["processing_fees"], gl["stripe_clearing"], cents, memo)
    for day, ref, cents, payout_id in stripe["payouts"]:
        memo = f"Stripe payout {payout_id}"[:120]
        pair(day, "Online payments", ref, gl["cash"], gl["stripe_clearing"], cents, memo)
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
        writer.writerow([c if i in (4, 5) else safe_cell(c) for i, c in enumerate(row)])
    record_event(
        "accounting.exported",
        actor=_user(actor),
        request=request,
        metadata={"start": start.isoformat(), "end": end.isoformat(), "rows": len(rows)},
    )
    return buf.getvalue().encode()


# --- Deposit batches ----------------------------------------------------------------------


def _deposited_ids() -> set[int]:
    """Payments in a (non-voided) deposit. A payment moved to another
    ownership account (D34-fixmoney-1) is the same check: its copy counts as
    deposited when the original was."""
    with system_access("deposited payments"):
        deposited = set(
            DepositItem.objects.values("entry_id")
            .annotate(total=Sum("amount_cents"))
            .exclude(total=0)
            .values_list("entry_id", flat=True)
        )
        moved = dict(
            LedgerEntry.objects.filter(
                kind=EntryKind.PAYMENT, moved_from__isnull=False
            ).values_list("moved_from_id", "pk")
        )
    frontier = list(deposited)
    while frontier:
        copy = moved.get(frontier.pop())
        if copy is not None and copy not in deposited:
            deposited.add(copy)
            frontier.append(copy)
    return deposited


def undeposited_payments():
    """Owner payments not in a (non-voided) deposit yet."""
    with system_access("undeposited payments"):
        deposited = _deposited_ids()
        from payments.services import online_entry_ids

        return list(
            LedgerEntry.objects.filter(kind=EntryKind.PAYMENT, reversed_by__isnull=True)
            .exclude(pk__in=list(deposited))
            .exclude(pk__in=list(online_entry_ids()))
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


def _returned_items(since, as_of):
    """Owner payments that were deposited and then bounced: the reversal of a
    deposited payment is money the bank takes back (BUG-016). Moving a
    payment to another ownership account is not a return (D34-fixmoney-1)."""
    with system_access("returned items"):
        deposited = _deposited_ids()
        return list(
            LedgerEntry.objects.filter(
                kind=EntryKind.REVERSAL,
                reverses_id__in=deposited,
                posted_on__gt=since,
                posted_on__lte=as_of,
            )
            .exclude(reverses__moved_to__isnull=False)
            .select_related("lot")
            .order_by("posted_on", "id")
        )


@dataclass(frozen=True)
class PayoutItem:
    """A Stripe payout as a cash-book deposit: online payments reach the bank
    only in payouts, never in deposit batches, so each payout counts once,
    on its arrival date (D33-acctfix-6)."""

    pk: int
    payout_id: str
    arrival_date: datetime.date
    amount_cents: int
    status: str

    @property
    def stripe_says_arrived(self) -> bool:
        return self.status == "paid"


def _payouts(since, as_of) -> list[PayoutItem]:
    """Stripe payouts arriving after ``since`` through ``as_of``."""
    from payments.sync import payouts_for_matching

    if as_of <= since:
        return []
    return [
        PayoutItem(p["id"], p["payout_id"], p["arrival_date"], p["amount_cents"], p["status"])
        for p in payouts_for_matching(since + datetime.timedelta(days=1), as_of)
        if p["status"] not in ("failed", "canceled")
    ]


def book_balance(as_of: datetime.date) -> int:
    """Opening cash + deposits + Stripe payouts - vendor payments - bounced
    deposited payments, through ``as_of``."""
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
    returned = sum(e.amount_cents for e in _returned_items(since, as_of))
    payouts = sum(p.amount_cents for p in _payouts(since, as_of))
    return setup.opening_cash_cents + deposits + payouts - paid - returned


def uncleared(as_of: datetime.date) -> dict:
    """Deposits, Stripe payouts and vendor payments through ``as_of`` not yet
    on a statement."""
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
    returned = [e for e in _returned_items(since, as_of) if e.pk not in _cleared_ids("returned")]
    cleared_payouts = _cleared_ids("payout")
    payouts = [p for p in _payouts(since, as_of) if p.pk not in cleared_payouts]
    return {"deposits": deposits, "payments": payments, "returned": returned, "payouts": payouts}


def reconcile(
    *,
    statement_date,
    statement_balance_cents: int,
    cleared_deposit_ids,
    cleared_payment_ids,
    cleared_returned_ids=(),
    cleared_payout_ids=None,
    note: str = "",
    actor,
    request=None,
) -> BankReconciliation:
    """Record a statement: the ticked items clear; the rest are outstanding.
    The difference should be zero; a non-zero one is recorded as found.

    ``cleared_payout_ids``: the Stripe payouts ticked. Left out (None), the
    payouts Stripe reports as paid (arrived at the bank) clear and any
    others stay outstanding; the reconciliation page always sends the
    board's ticks."""
    require(actor, "accounting.manage")
    with transaction.atomic():
        AccountingSetup.objects.select_for_update().get(pk=AccountingSetup.get().pk)
        open_items = uncleared(statement_date)
        deposits = {d.pk: d for d in open_items["deposits"]}
        payments = {p.pk: p for p in open_items["payments"]}
        returned = {e.pk: e for e in open_items["returned"]}
        payouts = {p.pk: p for p in open_items["payouts"]}
        dep_ids = {int(i) for i in cleared_deposit_ids}
        pay_ids = {int(i) for i in cleared_payment_ids}
        ret_ids = {int(i) for i in cleared_returned_ids}
        if cleared_payout_ids is None:
            po_ids = {k for k, p in payouts.items() if p.stripe_says_arrived}
        else:
            po_ids = {int(i) for i in cleared_payout_ids}
        if not (
            dep_ids <= set(deposits)
            and pay_ids <= set(payments)
            and ret_ids <= set(returned)
            and po_ids <= set(payouts)
        ):
            raise AccountingError("Some ticked items are not open on this date.")
        outstanding_dep = sum(d.total_cents for k, d in deposits.items() if k not in dep_ids)
        outstanding_dep += sum(p.amount_cents for k, p in payouts.items() if k not in po_ids)
        outstanding_pay = sum(p.amount_cents for k, p in payments.items() if k not in pay_ids)
        outstanding_pay += sum(e.amount_cents for k, e in returned.items() if k not in ret_ids)
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
        for k in ret_ids:
            ReconciledItem.objects.create(
                reconciliation=rec,
                kind="returned",
                object_id=k,
                amount_cents=returned[k].amount_cents,
            )
        for k in pay_ids:
            ReconciledItem.objects.create(
                reconciliation=rec,
                kind="payment",
                object_id=k,
                amount_cents=payments[k].amount_cents,
            )
        for k in po_ids:
            ReconciledItem.objects.create(
                reconciliation=rec,
                kind="payout",
                object_id=k,
                amount_cents=payouts[k].amount_cents,
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
        name = safe_cell(row["vendor"].name)
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
    from ledger.services import past_due

    day = rule("collections.courtesy_reminder_day") or 0
    today = today or timezone.localdate()
    if not day or today.day < int(day):
        return 0
    period = today.strftime("%Y-%m")
    sent = 0
    link = settings.PORTAL_BASE_URL + reverse("my_account")
    with system_access("courtesy reminders"):
        for lot in Lot.objects.order_by("number"):
            owed = past_due(lot.pk, today, account=CURRENT)
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
        return timezone.localdate(case.opened_at)
    days = int(rule("collections.notice_days") or 10)
    return last.generated_on + datetime.timedelta(days=days)


def generate_case_letter(case: CollectionCase, *, actor, request=None) -> tuple[bytes, str]:
    """The next notice in the sequence, as a PDF to print and mail; logged,
    and the case moves to the matching step."""
    from ledger import letters
    from ledger.services import COLLECTION, past_due

    require(actor, "collections.manage")
    if case.state in ("closed", "referred"):
        raise AccountingError("This case is closed or referred.")
    with transaction.atomic():
        # Locked so a double-click cannot record the same letter twice (BUG-013).
        case = CollectionCase.objects.unscoped("letter").select_for_update().get(pk=case.pk)
        kind = next_letter(case)
        if kind is None:
            raise AccountingError("All three notices have been generated; the board decides next.")
        due = letter_due_on(case)
        if kind != LetterKind.FIRST and due and due > timezone.localdate():
            raise AccountingError(
                f"The notice period from the last letter runs until {due:%B %-d}; "
                "the next letter can go out then."
            )
        owed = past_due(case.lot_id, account=CURRENT)  # the current owners' account
        if owed <= 0:
            raise AccountingError("This account is not past due.")
        data = letters.notice_pdf(case.lot, kind)
        CollectionLetter.objects.create(
            case=case,
            kind=kind,
            amount_cents=owed,
            generated_by=actor,
            generated_on=timezone.localdate(),
        )
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
