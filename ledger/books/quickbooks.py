"""Optional: the journal in QuickBooks Online's journal-entry import layout
(D71, item 7). The portal is the association's books; this file is only for
an association that also keeps QuickBooks Online (Accounting → QuickBooks
journal, labelled optional).

Layout of QuickBooks Online's "Import journal entries" template: Journal No,
Journal Date (MM/DD/YYYY), Account Name, Debits, Credits, Description, Name,
Location, Class. Each journal number is one balanced pair; Class carries the
fund (Operating fund, Reserve fund) for class tracking; Name is always empty
(owners are never named; the description has the address, as the generic
export does). Account names must exist in QuickBooks first, under the same
names as Accounting setup.
"""

from __future__ import annotations

import csv
import io

from audit.services import record_event
from core.lists import safe_cell
from core.policy import SYSTEM, require
from ledger.books.funds import FUND_LABELS

HEADER = [
    "Journal No",
    "Journal Date",
    "Account Name",
    "Debits",
    "Credits",
    "Description",
    "Name",
    "Location",
    "Class",
]


def _cents(value: int) -> str:
    return f"{value // 100}.{value % 100:02d}"


def rows(lines) -> list[list[str]]:
    out = []
    for line in lines:
        out.append(
            [
                f"OCF-{line.date:%Y%m%d}-{line.pair}",
                f"{line.date:%m/%d/%Y}",
                line.account,
                _cents(line.debit) if line.debit else "",
                _cents(line.credit) if line.credit else "",
                f"{line.journal}: {line.memo} ({line.ref})"[:200],
                "",
                "",
                FUND_LABELS.get(line.fund, line.fund),
            ]
        )
    return out


def csv_bytes(lines) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(HEADER)
    for row in rows(lines):
        writer.writerow([c if i in (3, 4) else safe_cell(c) for i, c in enumerate(row)])
    return buf.getvalue().encode()


def export(start, end, *, actor, request=None) -> bytes:
    from ledger.accounting import AccountingError, journal_lines

    require(actor, "finance.view")
    if end < start:
        raise AccountingError("The end date is before the start.")
    lines = journal_lines(start, end)
    record_event(
        "accounting.quickbooks_exported",
        actor=None if actor is SYSTEM else actor,
        request=request,
        metadata={"start": start.isoformat(), "end": end.isoformat(), "rows": len(lines)},
    )
    return csv_bytes(lines)
