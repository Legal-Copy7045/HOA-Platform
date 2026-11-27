"""The year-end package and treasurer handover (D71).

A self-contained .zip for the fiscal year: the financial statements, the
journal (owner ledger and GL), each account's month-end reconciliations
with the exceptions and how each was resolved, the owner subledger by home
(addresses, no names), disbursements and checks, invoices with their
approvals, an approval trail per transaction, the supporting documents as
they were at the time (invoice files uploaded by the payment date that pass
the malware scan rule), the treasurer handover sheet, and ``manifest.json``:
the SHA-256 of every file and an HMAC-SHA256 over the manifest keyed by a
server secret (``BOOKS_PACKAGE_KEY``, else a key derived from the Django
secret). Built on demand and automatically when a year closes; stored
privately under a random key as an append-only ``YearEndPackage``; streamed
only through the app after an authorisation check. Builds and downloads are
audited with counts only.

Verifying a package (README.txt in it says the same): every file's SHA-256
matches the manifest, and the manifest's ``hmac_sha256`` is
HMAC-SHA256(key, the manifest's JSON without that field, keys sorted,
separators "," and ":"). ``python manage.py verify_books_package <zip>``
does both on the server.
"""

from __future__ import annotations

import csv
import datetime
import hashlib
import hmac
import io
import json
import zipfile

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db.models import Sum
from django.utils import timezone

from audit.services import record_event
from core import pdf
from core.lists import safe_cell
from core.money import format_cents
from core.policy import SYSTEM, require
from core.scoping import internal, system_access
from ledger.books import funds, periods, statements
from ledger.books.models import PeriodKind, YearEndPackage

FORMAT = "hoa-books-package-v1"
MAX_DOCUMENT_BYTES = 120 * 1024 * 1024
_DERIVE_LABEL = b"oak-creek-portal year-end package key v1"


class PackageError(ValueError):
    pass


def _user(actor):
    return None if actor is SYSTEM else actor


def signing_key() -> bytes:
    """The server secret the manifest is signed with."""
    configured = (getattr(settings, "BOOKS_PACKAGE_KEY", "") or "").encode()
    if configured:
        return configured
    return hmac.new(settings.SECRET_KEY.encode(), _DERIVE_LABEL, hashlib.sha256).digest()


def key_is_configured() -> bool:
    return bool(getattr(settings, "BOOKS_PACKAGE_KEY", ""))


def canonical(manifest: dict) -> bytes:
    body = {k: v for k, v in manifest.items() if k != "hmac_sha256"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def sign(manifest: dict, key: bytes | None = None) -> str:
    return hmac.new(key or signing_key(), canonical(manifest), hashlib.sha256).hexdigest()


def verify(data: bytes, key: bytes | None = None) -> list[str]:
    """Problems with a package's bytes (empty when it verifies)."""
    problems = []
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
        manifest = json.loads(archive.read("manifest.json"))
    except (zipfile.BadZipFile, KeyError, ValueError):
        return ["Not a year-end package: manifest.json is missing or unreadable."]
    if not hmac.compare_digest(sign(manifest, key), str(manifest.get("hmac_sha256", ""))):
        problems.append("The manifest's signature does not match.")
    names = set(archive.namelist()) - {"manifest.json"}
    listed = {f["path"]: f for f in manifest.get("files", [])}
    for path in sorted(names - set(listed)):
        problems.append(f"{path}: not in the manifest.")
    for path, f in listed.items():
        if path not in names:
            problems.append(f"{path}: missing.")
            continue
        if hashlib.sha256(archive.read(path)).hexdigest() != f["sha256"]:
            problems.append(f"{path}: SHA-256 does not match.")
    return problems


# --- People and money as text ---------------------------------------------------------------


class _Who:
    """Staff names for the approval trail (board members and managers act for
    the association; owners appear only by address)."""

    def __init__(self):
        from community.models import Person

        with system_access("books package: staff names"):
            self._names = dict(
                Person.objects.filter(user__isnull=False).values_list("user_id", "display_name")
            )

    def __call__(self, user_id) -> str:
        if user_id is None:
            return "Automatic (portal)"
        return self._names.get(user_id, f"Login #{user_id}")


def _c(cents: int | None) -> str:
    if cents is None:
        return ""
    return f"{cents // 100}.{cents % 100:02d}" if cents >= 0 else f"-{_c(-cents)}"


def _csv(header: list[str], rows: list[list]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    for row in rows:
        w.writerow([c if isinstance(c, int) else safe_cell(str(c)) for c in row])
    return buf.getvalue().encode()


def _d(value) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime.datetime):
        return timezone.localtime(value).strftime("%Y-%m-%d %H:%M")
    return value.isoformat()


# --- The files ----------------------------------------------------------------------------------


def _journal_files(start, end) -> dict[str, bytes]:
    from ledger.accounting import EXPORT_HEADER, journal_lines
    from ledger.books import quickbooks

    lines = journal_lines(start, end)
    rows = [[*line.row(), funds.FUND_LABELS.get(line.fund, line.fund)] for line in lines]
    return {
        "journal/journal.csv": _csv([*EXPORT_HEADER, "Fund"], rows),
        "journal/quickbooks-online-journal.csv": quickbooks.csv_bytes(lines),
    }


def _reconciliation_files(fiscal_year: int, who) -> dict[str, bytes]:
    from ledger.books import close as books_close
    from ledger.books.models import CloseRun, ExceptionNote, PeriodClose

    standing = periods.standing_closes()
    rows = []
    for key in periods.year_months(fiscal_year):
        close = standing.get(key)
        if close is not None:
            accounts = close.summary.get("accounts", [])
            how = f"{close.get_how_display()} {_d(close.closed_at)} by {who(close.closed_by_id)}"
        else:
            _start, end = periods.month_bounds(key)
            if funds.books_start() is None or end <= funds.books_start():
                continue
            accounts = [a.summary() for a in books_close.evaluate_month(key).accounts]
            how = "Not closed"
        for a in accounts:
            setup = funds.setup_for(a["connection_id"])
            rows.append(
                [
                    key,
                    setup.connection.label if setup else f"Account #{a['connection_id']}",
                    funds.FUND_LABELS.get(a["fund"], a["fund"]),
                    _c(a["bank_cents"]),
                    a["bank_source"],
                    _c(a["book_cents"]),
                    _c(a["outstanding_in"]),
                    _c(a["outstanding_out"]),
                    _c(a["bank_ahead"]),
                    _c(a["difference"]),
                    how,
                ]
            )
    recs = _csv(
        [
            "Month",
            "Bank account",
            "Fund",
            "Bank balance",
            "Balance from",
            "Books",
            "Deposits in transit",
            "Payments outstanding",
            "On the bank before the books",
            "Difference",
            "Closed",
        ],
        rows,
    )
    keys = set(periods.year_months(fiscal_year)) | {periods.year_key(fiscal_year)}
    notes = []
    for n in ExceptionNote.objects.filter(period__in=keys).order_by("recorded_at"):
        agreement = getattr(n, "agreement", None)
        notes.append(
            [
                n.period,
                n.check_key,
                n.item,
                _c(n.amount_cents),
                n.reason,
                who(n.recorded_by_id),
                _d(n.recorded_at),
                "yes" if n.needs_agreement else "no",
                who(agreement.agreed_by_id) if agreement else "",
                _d(agreement.agreed_at) if agreement else "",
            ]
        )
    runs = []
    for r in CloseRun.objects.filter(period__in=keys).order_by("ran_at"):
        for c in r.results:
            if c.get("open"):
                runs.append(
                    [
                        r.period,
                        _d(r.ran_at),
                        c["key"],
                        c["status"],
                        c["open"],
                        "closed" if r.closed else "",
                    ]
                )
    closes = []
    for c in PeriodClose.objects.filter(period__in=keys).order_by("id"):
        closes.append([c.period, c.get_how_display(), _d(c.closed_at), who(c.closed_by_id), "", ""])
        for req in c.reopen_requests.order_by("id"):
            dec = getattr(req, "decision", None)
            closes.append(
                [
                    c.period,
                    "Reopen asked",
                    _d(req.requested_at),
                    who(req.requested_by_id),
                    req.reason,
                    (
                        f"{'Approved' if dec.approved else 'Declined'} by {who(dec.decided_by_id)} "
                        f"{_d(dec.decided_at)}"
                    )
                    if dec
                    else "Waiting",
                ]
            )
    return {
        "reconciliations/month-end-reconciliations.csv": recs,
        "reconciliations/exceptions-explained.csv": _csv(
            [
                "Period",
                "Check",
                "Item",
                "Amount",
                "Reason",
                "Noted by",
                "Noted at",
                "Needs a second board member",
                "Agreed by",
                "Agreed at",
            ],
            notes,
        ),
        "reconciliations/exceptions-found.csv": _csv(
            ["Period", "Checked at", "Check", "Status", "Open items", "Result"], runs
        ),
        "reconciliations/closes-and-reopens.csv": _csv(
            ["Period", "What", "When", "Who", "Reason", "Decision"], closes
        ),
    }


def _bank_lines(start, end, who) -> bytes:
    from ledger.bankfeeds.matching import active_matches
    from ledger.books.models import GLEntry
    from ledger.models import BankTransaction

    matches = {
        m.transaction_id: m
        for m in active_matches().filter(transaction__posted_on__range=(start, end))
    }
    coded: dict[int, list] = {}
    for g in (
        GLEntry.objects.filter(transaction__posted_on__range=(start, end))
        .select_related("category", "rule")
        .order_by("id")
    ):
        coded.setdefault(g.transaction_id, []).append(g)
    rows = []
    for line in (
        BankTransaction.objects.filter(posted_on__range=(start, end))
        .select_related("connection")
        .order_by("posted_on", "id")
    ):
        how, by, when = "Not matched or coded", "", ""
        m = matches.get(line.pk)
        if m is not None:
            how = f"Matched: {m.get_kind_display()} #{m.object_id}"
            by, when = who(m.confirmed_by_id), _d(m.confirmed_at)
        for g in coded.get(line.pk, []):
            if g.reverses_id:
                how += f"; coding undone {g.posted_on} ({g.memo})"
                by = who(g.coded_by_id)
            else:
                how = f"Coded: {g.category.name}" + (f" ({g.reference})" if g.reference else "")
                by = f"Rule #{g.rule_id}: {g.rule.name}" if g.rule_id else who(g.coded_by_id)
                when = _d(g.created_at)
        rows.append(
            [
                line.posted_on.isoformat(),
                line.connection.label,
                _c(line.amount_cents),
                line.description,
                line.get_source_display(),
                how,
                by,
                when,
            ]
        )
    return _csv(
        [
            "Date",
            "Bank account",
            "Amount",
            "Description",
            "From",
            "How it is in the books",
            "By",
            "When",
        ],
        rows,
    )


def _owner_subledger(start, end) -> bytes:
    from community.models import Account, Lot, home_sort_key
    from ledger.models import LedgerEntry

    rows = []
    with system_access("books package: owner subledger"):
        lots = sorted(Lot.objects.all(), key=home_sort_key)
        numbers = dict(Account.objects.values_list("pk", "number"))
        for lot in lots:
            opening = (
                LedgerEntry.objects.filter(lot=lot, posted_on__lt=start).aggregate(
                    s=Sum("amount_cents")
                )["s"]
                or 0
            )
            running = opening
            rows.append(
                [
                    lot.label,
                    lot.number,
                    "",
                    start.isoformat(),
                    "Opening balance",
                    "",
                    "",
                    _c(opening),
                ]
            )
            for e in LedgerEntry.objects.filter(lot=lot, posted_on__range=(start, end)).order_by(
                "posted_on", "id"
            ):
                running += e.amount_cents
                rows.append(
                    [
                        lot.label,
                        lot.number,
                        numbers.get(e.account_id, ""),
                        e.posted_on.isoformat(),
                        e.get_kind_display(),
                        e.description,
                        _c(e.amount_cents),
                        _c(running),
                    ]
                )
    return _csv(
        ["Address", "Lot number", "Account", "Date", "Entry", "Description", "Amount", "Balance"],
        rows,
    )


def _disbursement_files(start, end, who) -> dict[str, bytes]:
    from ledger.models import DisbursementBatch, DisbursementCheck, DisbursementCheckEvent

    batches = []
    for b in DisbursementBatch.objects.filter(created_at__date__range=(start, end)).order_by("id"):
        decisions = "; ".join(
            f"{d.get_decision_display()} by {who(d.approver_id)} {_d(d.created_at)}"
            for d in b.decisions.order_by("id")
        )
        batches.append(
            [
                b.pk,
                b.get_kind_display(),
                b.title,
                _c(b.total_cents),
                who(b.proposed_by_id),
                _d(b.created_at),
                decisions,
            ]
        )
    checks = []
    events = {}
    for ev in DisbursementCheckEvent.objects.all():
        events.setdefault(ev.check_record_id, []).append(ev)
    with system_access("books package: checks"):
        for c in DisbursementCheck.objects.filter(issued_on__range=(start, end)).select_related(
            "line__lot"
        ):
            ev = "; ".join(f"{e.get_kind_display()} {e.on}" for e in events.get(c.pk, []))
            checks.append(
                [
                    c.number,
                    c.issued_on.isoformat(),
                    _c(c.amount_cents),
                    c.line.lot.label,
                    c.line.batch_id,
                    who(c.issued_by_id),
                    ev,
                ]
            )
    return {
        "disbursements/disbursements.csv": _csv(
            ["Batch", "Kind", "Title", "Total", "Proposed by", "Proposed at", "Decisions"], batches
        ),
        "disbursements/checks.csv": _csv(
            ["Check", "Issued", "Amount", "Home", "Batch", "Recorded by", "Cleared or voided"],
            checks,
        ),
    }


def _invoice_files(start, end, who) -> tuple[dict[str, bytes], list, list]:
    """invoices.csv, and the invoice files as they were on the payment date."""
    from documents.attachments import attachments_for, current_scan
    from documents.services import scan_allows_download
    from documents.validation import safe_filename
    from operations.models import Invoice, InvoicePayment

    paid = InvoicePayment.objects.filter(paid_on__range=(start, end)).select_related("invoice")
    invoice_ids = set(paid.values_list("invoice_id", flat=True)) | set(
        Invoice.objects.filter(invoice_date__range=(start, end)).values_list("pk", flat=True)
    )
    rows, docs, skipped = [], [], []
    total_bytes = 0
    for inv in Invoice.objects.filter(pk__in=invoice_ids).select_related("vendor", "category"):
        approvals = "; ".join(
            f"{a.get_decision_display()} by {who(a.approver_id)} {_d(a.created_at)}"
            for a in inv.approvals.order_by("id")
        )
        payments = list(inv.payments.order_by("paid_on", "id"))
        rows.append(
            [
                inv.number,
                inv.vendor.name,
                inv.invoice_date.isoformat(),
                _c(inv.amount_cents),
                f"{inv.category.code} {inv.category.name}",
                inv.state,
                who(inv.entered_by_id),
                approvals,
                "; ".join(
                    f"{p.paid_on} {p.get_method_display()} {_c(p.amount_cents)}" for p in payments
                ),
            ]
        )
        # The files as they were when it was paid (or at the year's end).
        as_of = max((p.paid_on for p in payments), default=end)
        n = 0
        for att in attachments_for(inv).order_by("uploaded_at", "id"):
            if timezone.localdate(att.uploaded_at) > as_of:
                continue
            n += 1
            ext = (att.original_filename.rsplit(".", 1)[-1] or "bin").lower()[:8]
            name = f"documents/invoice-{safe_filename(inv.number, 'x')[:-2]}-{n}-" + safe_filename(
                att.original_filename, ext
            )
            if not scan_allows_download(current_scan(att)):
                skipped.append({"path": name, "reason": "did not pass the malware scan rule"})
                continue
            if total_bytes + att.size > MAX_DOCUMENT_BYTES:
                skipped.append({"path": name, "reason": "the package size limit was reached"})
                continue
            try:
                with att.file.open("rb") as fh:
                    data = fh.read()
            except (OSError, ValueError):
                skipped.append({"path": name, "reason": "the stored file could not be read"})
                continue
            total_bytes += len(data)
            docs.append((name, data))
    files = {
        "invoices/invoices.csv": _csv(
            [
                "Invoice",
                "Vendor",
                "Date",
                "Amount",
                "Category",
                "State",
                "Entered by",
                "Approvals",
                "Payments",
            ],
            rows,
        )
    }
    return files, docs, skipped


def _approval_trail(start, end, who) -> bytes:
    """Per transaction: who initiated, who approved, when, and what later
    reversed or corrected it."""
    from ledger.books.models import GLEntry
    from ledger.models import LedgerEntry
    from operations.models import InvoicePayment

    rows = []
    for p in InvoicePayment.objects.filter(paid_on__range=(start, end)).select_related("invoice"):
        approvals = "; ".join(
            f"{who(a.approver_id)} {_d(a.created_at)}"
            for a in p.invoice.approvals.filter(decision="approve").order_by("id")
        )
        rows.append(
            [
                "Vendor payment",
                f"P{p.pk}",
                p.paid_on.isoformat(),
                _c(-p.amount_cents),
                f"Invoice {p.invoice.number}",
                who(p.invoice.entered_by_id),
                who(p.recorded_by_id),
                approvals,
                "",
            ]
        )
    with system_access("books package: approval trail"):
        for e in (
            LedgerEntry.objects.filter(posted_on__range=(start, end))
            .select_related("lot", "reversed_by", "moved_to")
            .order_by("posted_on", "id")
        ):
            if e.posted_by_id is None and e.kind in ("assessment", "late_fee"):
                continue  # posted by the portal on schedule: in the subledger
            later = []
            rev = getattr(e, "reversed_by", None)
            if rev is not None:
                later.append(f"reversed by L{rev.pk} on {rev.posted_on} ({who(rev.posted_by_id)})")
            moved = getattr(e, "moved_to", None)
            if moved is not None:
                later.append(f"moved to account as L{moved.pk}")
            rows.append(
                [
                    "Owner ledger",
                    f"L{e.pk}",
                    e.posted_on.isoformat(),
                    _c(e.amount_cents),
                    f"{e.lot.label}: {e.get_kind_display()}",
                    who(e.posted_by_id),
                    "",
                    "",
                    "; ".join(later),
                ]
            )
    for g in GLEntry.objects.filter(posted_on__range=(start, end)).select_related(
        "category", "rule", "reversed_by"
    ):
        rev = getattr(g, "reversed_by", None)
        by = f"Rule #{g.rule_id}: {g.rule.name}" if g.rule_id else who(g.coded_by_id)
        rows.append(
            [
                "Bank line coded",
                f"B{g.transaction_id}",
                g.posted_on.isoformat(),
                _c(g.amount_cents),
                g.category.name,
                by,
                "",
                "",
                f"undone on {rev.posted_on} by {who(rev.coded_by_id)}" if rev else "",
            ]
        )
    return _csv(
        [
            "Kind",
            "Reference",
            "Date",
            "Amount",
            "What",
            "Initiated by",
            "Recorded by",
            "Approved by",
            "Later reversed or corrected",
        ],
        rows,
    )


@internal("books: handover_paragraphs")
def handover_paragraphs(fiscal_year: int | None = None) -> list[tuple[str, str]]:
    """The treasurer handover sheet: where the money is, accounts and funds,
    rules in force, open exceptions, recurring obligations."""
    from community.models import CommunityProfile
    from community.rules import REGISTRY, rule_with_citation
    from ledger.books import close as books_close
    from ledger.books import journal
    from ledger.books.coding import active_rules
    from operations.models import ComplianceItem, Contract

    today = timezone.localdate()
    p: list[tuple[str, str]] = [
        ("small", pdf.plain(CommunityProfile.get().legal_name)),
        ("heading", "Treasurer handover"),
        ("small", f"Prepared {today:%B %-d, %Y}."),
        ("normal", ""),
        ("subheading", "Where the money is"),
    ]
    items = journal.cash_items(today)
    for s in funds.accounts():
        bank, source = journal.bank_balance(s.connection, today)
        rec = journal.reconcile_account(s, today, items)
        p.append(
            (
                "row",
                pdf.plain(
                    f"{s.connection.label[:30]:30} {funds.FUND_LABELS[s.fund][:15]:15} "
                    f"books {format_cents(rec.book_cents):>12}  bank "
                    f"{format_cents(bank) if bank is not None else 'unknown':>12}"
                ),
            )
        )
        p.append(
            (
                "note",
                pdf.plain(
                    f"  {s.connection.institution_name or 'Bank'}; chart account {s.gl_account}"
                    + ("; primary operating account" if s.is_primary else "")
                    + (f"; balance from {source}" if source else "")
                ),
            )
        )
    if not funds.accounts():
        p.append(("normal", "No bank account is in the books yet."))
    for fund, pos in books_close.fund_positions(today).items():
        p.append(
            (
                "row",
                pdf.plain(
                    f"{funds.FUND_LABELS[fund]:30} cash {format_cents(pos['cash']):>12}  balance "
                    f"{format_cents(pos['balance']):>12}"
                ),
            )
        )
    p += [("normal", ""), ("subheading", "Rules in force")]
    for key, spec in REGISTRY.items():
        if not key.startswith(
            (
                "books.",
                "dues.",
                "late_fee.",
                "disbursement.",
                "accounting.",
                "payments.",
                "invoice.",
            )
        ):
            continue
        value, citation = rule_with_citation(key)
        p.append(
            (
                "note",
                pdf.plain(
                    f"{spec.shown_label}: {value if value is not None else 'not set'}"
                    + (f" ({citation})" if citation else "")
                ),
            )
        )
    rules = active_rules()
    if rules:
        p += [("normal", ""), ("subheading", "Bank coding rules")]
        for r in rules:
            p.append(
                (
                    "note",
                    pdf.plain(
                        f'#{r.pk} "{r.contains}" ({r.get_sign_display().lower()}) -> '
                        f"{r.category.name}"
                    ),
                )
            )
    p += [("normal", ""), ("subheading", "Open exceptions")]
    open_runs = books_close.open_exception_lines()
    p += [("note", pdf.plain(t)) for t in open_runs] or [
        ("note", "None: every period is closed or balanced.")
    ]
    p += [("normal", ""), ("subheading", "Recurring obligations")]
    for c in Contract.objects.filter(state="active").select_related("vendor"):
        p.append(
            (
                "note",
                pdf.plain(
                    f"{c.vendor.name}: {c.title} ({c.get_schedule_display().lower()}, up to "
                    f"{format_cents(c.amount_cap_cents)})"
                ),
            )
        )
    for item in ComplianceItem.objects.filter(
        active=True, category__in=["tax", "insurance", "corporate", "reserve"]
    ):
        p.append(
            (
                "note",
                pdf.plain(
                    f"{item.title}: due {item.due_on:%B %-d, %Y} "
                    f"({item.get_recurrence_display().lower()})"
                ),
            )
        )
    reserve = books_close.monthly_reserve_cents()
    if reserve:
        p.append(("note", f"Reserve contribution: {format_cents(reserve)} a month."))
    p += [
        ("normal", ""),
        ("subheading", "What happens by itself"),
        (
            "note",
            "Each day the portal codes new bank lines by the rules above, confirms "
            "unambiguous matches, and from the close day closes last month if everything "
            "balances. Anything else is on My items and Finance > Month and year close.",
        ),
    ]
    return p


# --- Building ------------------------------------------------------------------------------------


@internal("books: build")
def build(fiscal_year: int, *, actor, automatic: bool = False, request=None) -> YearEndPackage:
    if actor is not SYSTEM:
        require(actor, "books.package")
    start, end = periods.year_bounds(fiscal_year)
    if start > timezone.localdate():
        raise PackageError("That fiscal year has not started.")
    closed = periods.is_closed_period(periods.year_key(fiscal_year), PeriodKind.YEAR)
    who = _Who()
    period = statements.period_for(year=fiscal_year)
    s = statements.statement_set(period)
    files: dict[str, bytes] = {}
    files["statements/financial-statements.pdf"] = pdf.render(
        statements.pdf_paragraphs(s), title=f"Financial statements FY{fiscal_year}"
    )
    stmt_csv = io.StringIO()
    w = csv.writer(stmt_csv)
    for row in statements.csv_rows(s):
        w.writerow(row)
    files["statements/financial-statements.csv"] = stmt_csv.getvalue().encode()
    a = s["annual"]
    files["statements/annual-accounting.csv"] = _csv(
        ["Fiscal year", "Income", "Expenditures", "Reserves set aside", "Net excess (deficit)"],
        [
            [
                fiscal_year,
                _c(a["income_cents"]),
                _c(a["expenditures_cents"]),
                _c(a["reserves_cents"]),
                _c(a["net_cents"]),
            ]
        ],
    )
    t = s["tax"]
    files["statements/tax-worksheet.csv"] = _csv(
        ["Line", "Amount"],
        [["Exempt-function income", _c(t["exempt_function_cents"])]]
        + [[f"Other income: {acct}", _c(c)] for acct, c in t["other_income_rows"]]
        + [
            ["Other income", _c(t["other_income_cents"])],
            ["Gross income", _c(t["gross_income_cents"])],
            ["Expenses", _c(t["expenses_cents"])],
            ["Note", t["note"]],
        ],
    )
    files.update(_journal_files(start, end))
    files.update(_reconciliation_files(fiscal_year, who))
    files["bank/bank-lines.csv"] = _bank_lines(start, end, who)
    files["owners/owner-subledger.csv"] = _owner_subledger(start, end)
    files.update(_disbursement_files(start, end, who))
    invoice_files, docs, skipped = _invoice_files(start, end, who)
    files.update(invoice_files)
    files["approvals/approval-trail.csv"] = _approval_trail(start, end, who)
    from ledger.books import tax

    files.update(tax.package_files(fiscal_year))
    files["handover/treasurer-handover.pdf"] = pdf.render(
        handover_paragraphs(fiscal_year), title="Treasurer handover"
    )
    for name, data in docs:
        files[name] = data
    files["README.txt"] = _readme(fiscal_year, closed).encode()
    built_at = timezone.now()
    manifest = {
        "format": FORMAT,
        "fiscal_year": fiscal_year,
        "period": {"start": start.isoformat(), "end": end.isoformat()},
        "year_closed": closed,
        "built_at": built_at.isoformat(),
        "release": getattr(settings, "APP_RELEASE", ""),
        "files": [
            {"path": name, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
            for name, data in sorted(files.items())
        ],
        "skipped": skipped,
    }
    manifest["hmac_sha256"] = sign(manifest)
    buf = io.BytesIO()
    stamp = timezone.localtime(built_at).timetuple()[:6]
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(files.items()):
            archive.writestr(
                zipfile.ZipInfo(name, date_time=stamp), data, compress_type=zipfile.ZIP_DEFLATED
            )
        archive.writestr(
            zipfile.ZipInfo("manifest.json", date_time=stamp),
            json.dumps(manifest, indent=2, sort_keys=True),
            compress_type=zipfile.ZIP_DEFLATED,
        )
    data = buf.getvalue()
    stored = default_storage.save(f"books/packages/{_random_key()}", ContentFile(data))
    pkg = YearEndPackage.objects.create(
        fiscal_year=fiscal_year,
        year_closed=closed,
        file=stored,
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        manifest=manifest,
        signature=manifest["hmac_sha256"],
        file_count=len(files),
        skipped_count=len(skipped),
        automatic=automatic,
        built_by=_user(actor),
        built_at=built_at,
    )
    record_event(
        "books.package_built",
        actor=_user(actor),
        request=request,
        target=pkg,
        metadata={
            "fiscal_year": fiscal_year,
            "files": len(files),
            "documents": len(docs),
            "skipped": len(skipped),
            "automatic": automatic,
            "closed": closed,
        },
    )
    return pkg


def _random_key() -> str:
    import secrets

    return secrets.token_hex(16)


def _readme(fiscal_year: int, closed: bool) -> str:
    status = "closed" if closed else "NOT CLOSED: figures may still change"
    return (
        f"Year-end package, fiscal year {fiscal_year} ({status}).\r\n\r\n"
        "statements/   financial statements (PDF and CSV), the annual accounting figure and the\r\n"
        "              tax worksheet for the preparer\r\n"
        "journal/      the journal (owner ledger and coded bank lines), with each line's fund,\r\n"
        "              and the same in QuickBooks Online journal-import layout\r\n"
        "reconciliations/  each bank account's month-end reconciliation, the exceptions found\r\n"
        "              and how each was explained, closes and reopens\r\n"
        "bank/         every bank line and how it is in the books (matched, coded, by whom)\r\n"
        "owners/       the owner subledger by home (addresses only)\r\n"
        "disbursements/, invoices/, approvals/  checks, invoices with their approvals, and the\r\n"
        "              approval trail per transaction\r\n"
        "documents/    invoice files as they were when paid (only files passing the scan rule)\r\n"
        "handover/     the treasurer handover sheet\r\n"
        "tax/          the Form 1120-H worksheet, the 1099 IRIS upload file and recipient\r\n"
        "              copies (no taxpayer numbers: the portal keeps none), filing deadlines.\r\n"
        "              Worksheets only: nothing was filed; confirm with your tax preparer.\r\n\r\n"
        "Verifying: manifest.json lists the SHA-256 of every file. Its hmac_sha256 is\r\n"
        "HMAC-SHA256(key, the manifest's JSON without hmac_sha256, keys sorted, separators\r\n"
        "',' and ':'). The key is the server's BOOKS_PACKAGE_KEY; ask the sysadmin.\r\n"
        "In Python:\r\n\r\n"
        "  import hashlib, hmac, json, zipfile\r\n"
        "  z = zipfile.ZipFile('package.zip'); m = json.loads(z.read('manifest.json'))\r\n"
        "  body = json.dumps({k: v for k, v in m.items() if k != 'hmac_sha256'},\r\n"
        "                    sort_keys=True, separators=(',', ':')).encode()\r\n"
        "  key = open('key.txt', 'rb').read().strip()\r\n"
        "  assert hmac.compare_digest(hmac.new(key, body, hashlib.sha256).hexdigest(),\r\n"
        "                             m['hmac_sha256'])\r\n"
        "  for f in m['files']:\r\n"
        "      assert hashlib.sha256(z.read(f['path'])).hexdigest() == f['sha256'], f['path']\r\n"
    )


def open_for_download(pkg: YearEndPackage, *, actor, request=None):
    """The stored .zip as a file object, after the permission check (audited)."""
    require(actor, "books.package")
    try:
        fh = pkg.file.open("rb")
    except (OSError, ValueError) as exc:
        raise PackageError("The stored package could not be read; build it again.") from exc
    record_event(
        "books.package_downloaded",
        actor=actor,
        request=request,
        target=pkg,
        metadata={"fiscal_year": pkg.fiscal_year, "files": pkg.file_count, "size": pkg.size},
    )
    return fh
