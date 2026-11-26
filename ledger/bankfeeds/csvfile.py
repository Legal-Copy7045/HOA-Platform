"""Bank CSV exports: every bank's columns differ, so a person maps them once
per file (date, description, and either one signed amount or separate debit
and credit columns).

Lines are deduplicated by a hash of (date, amount, description) plus how many
times that same line already appeared in the file, so re-importing a file (or
an overlapping one) adds nothing twice while two identical deposits on one
day both count.
"""

from __future__ import annotations

import csv
import datetime
import hashlib
import io
from dataclasses import dataclass

from ledger.bankfeeds.base import BankFeedError, TransactionInfo, clean_text, to_cents

# Under Django's 2.5 MB form-field limit: the mapping step posts the text back.
MAX_BYTES = 2 * 1024 * 1024
MAX_ROWS = 20_000
MAX_COLUMNS = 60
DATE_FORMATS = (
    "%Y-%m-%d",
    "%m/%d/%Y",
    "%m/%d/%y",
    "%m-%d-%Y",
    "%Y/%m/%d",
    "%b %d, %Y",
    "%d %b %Y",
    "%Y%m%d",
)


@dataclass(frozen=True)
class Mapping:
    date: int
    description: int
    amount: int | None = None
    debit: int | None = None
    credit: int | None = None
    flip: bool = False  # the bank shows money out as positive numbers

    def check(self, columns: int) -> None:
        used = [self.date, self.description]
        if self.amount is not None:
            used.append(self.amount)
        elif self.debit is not None and self.credit is not None:
            used += [self.debit, self.credit]
        else:
            raise BankFeedError("Choose the amount column, or both the debit and credit columns.")
        if any(c < 0 or c >= columns for c in used) or len(set(used)) != len(used):
            raise BankFeedError("Choose a different column for each part.")


def _rows(text: str) -> list[list[str]]:
    if len(text) > MAX_BYTES:
        raise BankFeedError("The file is too large for a bank statement.")
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = []
    try:
        for row in csv.reader(io.StringIO(text), dialect):
            if len(rows) > MAX_ROWS:
                raise BankFeedError("The file has too many lines.")
            if any(cell.strip() for cell in row):
                rows.append([cell[:500] for cell in row[:MAX_COLUMNS]])
    except csv.Error as exc:
        raise BankFeedError("The file is not a readable CSV file.") from exc
    if len(rows) < 2:
        raise BankFeedError("The file needs a header row and at least one line.")
    return rows


def decode(data: bytes) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise BankFeedError("The CSV file must be UTF-8.") from exc


def head(text: str, sample: int = 5) -> tuple[list[str], list[list[str]]]:
    """The header names (cleaned for display) and the first few lines."""
    rows = _rows(text)
    headers = [clean_text(h, 60) or f"Column {i + 1}" for i, h in enumerate(rows[0])]
    return headers, [[clean_text(c, 60) for c in r] for r in rows[1 : 1 + sample]]


def guess(headers: list[str]) -> dict[str, int | None]:
    """A first guess at the mapping from common header names."""
    lowered = [h.casefold() for h in headers]

    def find(*words):
        for word in words:
            for i, h in enumerate(lowered):
                if word in h:
                    return i
        return None

    debit, credit = find("debit", "withdrawal"), find("credit", "deposit")
    split = debit is not None and credit is not None and debit != credit
    return {
        "date": find("posted", "date"),
        "description": find("description", "payee", "memo", "name", "details"),
        "amount": None if split else find("amount"),
        "debit": debit if split else None,
        "credit": credit if split else None,
    }


def parse_date(value: str) -> datetime.date:
    text = " ".join((value or "").split())[:30]
    for fmt in DATE_FORMATS:
        try:
            return datetime.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise BankFeedError("A date in the file is not valid.")


def parse(text: str, mapping: Mapping) -> tuple[list[TransactionInfo], int]:
    """(transactions, lines skipped). Lines without a valid date or amount
    (totals, notes) are skipped and counted."""
    rows = _rows(text)
    mapping.check(max(len(r) for r in rows))
    out: list[TransactionInfo] = []
    seen: dict[str, int] = {}
    skipped = 0
    for row in rows[1:]:
        cells = row + [""] * (MAX_COLUMNS - len(row))
        try:
            posted = parse_date(cells[mapping.date])
            if mapping.amount is not None:
                amount = to_cents(cells[mapping.amount])
            else:
                debit = cells[mapping.debit].strip()
                credit = cells[mapping.credit].strip()
                amount = (to_cents(credit) if credit else 0) - (
                    abs(to_cents(debit)) if debit else 0
                )
                if not debit and not credit:
                    raise BankFeedError("No amount.")
        except BankFeedError:
            skipped += 1
            continue
        if mapping.flip:
            amount = -amount
        description = clean_text(cells[mapping.description])
        key = f"{posted.isoformat()}|{amount}|{description.casefold()}"
        n = seen.get(key, 0)
        seen[key] = n + 1
        digest = hashlib.sha256(f"{key}|{n}".encode()).hexdigest()[:48]
        out.append(
            TransactionInfo(
                bank_id=f"csv:{digest}",
                posted_on=posted,
                amount_cents=amount,
                description=description,
                kind="",
            )
        )
    if not out:
        raise BankFeedError("No lines with a date and an amount were found with those columns.")
    return out, skipped
