"""Mask account numbers the first masking (0016) let through in stored
bank-line descriptions (fixres BUG-004 / SEC-003 / R2-004).

0016 joined digit groups only across spaces and dashes, kept 8-digit runs
that read as dates and runs followed by cents, and masked after the
description had been cut to 200 characters. The importer now also joins
groups across dots, slashes and commas, masks any 8+ digit run written
without separators, keeps cents only on small amounts, and masks before it
cuts. This migration applies the same masking to what is already stored:
the masking is frozen here (a copy of ``ledger.bankfeeds.base.redact_numbers``
as of this migration; 0016 is not changed) and a test checks the two agree.

A stored description of ``CUT_AT`` characters or more may have been cut in
the middle of a number by the old order (cut, then mask), so a digit run at
its very end is masked whole.

Bank lines are append-only: the row trigger is lifted for this one UPDATE
only, run as the owner role that applies migrations; only ``description``
changes, and only where masking changes it.
"""

import datetime
import re

from django.db import migrations

DESCRIPTION_MAX = 200
CUT_AT = DESCRIPTION_MAX - 10

MASK = "\u2022\u2022\u2022\u2022"  # "••••"
# A run of digit groups joined by single spaces, dashes, dots, slashes or
# commas (account and routing numbers are written "0001 2345 6789",
# "1234.5678.9012", "WIRE,000123456789,01"), never starting right after a
# digit or a mask dot (so masking twice changes nothing).
_RUN = r"(?<![\d\u2022])\d+(?:[ ./,-]\d+)*"
# A date written with one kind of separator: 2026-11-15, 11/15/2026, 11.15.26.
_DATE = (
    r"(?<![\d\u2022])(?<!\d[./,-])"
    r"(?:\d{4}(?P<s1>[-/.])\d{1,2}(?P=s1)\d{1,2}|\d{1,2}(?P<s2>[-/.])\d{1,2}(?P=s2)(?:\d{4}|\d{2}))"
    r"(?!\d)(?![./,-]\d)"
)
_RUN_RE = re.compile(_RUN)
_DATE_RE = re.compile(_DATE)
# What may stay after a "$": dollars (thousands commas allowed), optional cents.
_DOLLARS = re.compile(r"(?:\d{1,3}(?:,\d{3})+|\d{1,9})(?:\.\d{2})?")
# An amount with no "$": at most five digits of dollars, then exactly two of cents.
_SMALL_AMOUNT = re.compile(r"(?:\d{1,2},\d{3}|\d{1,5})[.,]\d{2}")


def _real_date(text: str) -> bool:
    groups = [int(g) for g in re.split(r"[-/.]", text)]
    if len(re.split(r"[-/.]", text)[0]) == 4:
        candidates = [(groups[0], groups[1], groups[2])]
    else:
        year = groups[2] + (2000 if groups[2] < 100 else 0)
        candidates = [(year, groups[0], groups[1]), (year, groups[1], groups[0])]
    for y, m, d in candidates:
        if 1900 <= y <= 2100:
            try:
                datetime.date(y, m, d)
            except ValueError:
                continue
            return True
    return False


def _plain_six_digit_date(digits: str) -> bool:
    """YYMMDD or MMDDYY written without separators (six digits only)."""
    a, b, c = int(digits[:2]), int(digits[2:4]), int(digits[4:])
    for y, m, d in ((2000 + a, b, c), (2000 + c, a, b)):
        try:
            datetime.date(y, m, d)
        except ValueError:
            continue
        return True
    return False


def redact_numbers(text: str, limit: int = DESCRIPTION_MAX, *, cut: bool = False) -> str:
    """Mask long digit runs (account and routing numbers) down to the last four.

    The portal never stores bank account numbers (SEC-004, fixres BUG-004):
    any run of six or more digits, grouped with spaces, dashes, dots,
    slashes or commas or not, becomes "••••1234". Kept: a date written with
    separators that is a real date (2026-11-15, 11/15/2026), a six-digit
    run that reads as a date, an amount after a "$", and a small amount with
    cents (at most five digits of dollars). Eight or more digits without
    separators are masked even when they read as a date. With ``cut`` (the
    text was cut at a length cap) a digit run at the very end is masked
    whole: it may be the start of a longer number. Short numbers such as
    check numbers (five digits or fewer) are kept. Over-masking is preferred
    to keeping a number. The text is masked first and only then cut to
    ``limit``.
    """
    text = text or ""
    # Real dates are kept whole; the text between them is masked piece by
    # piece, so a date never joins the number next to it.
    pieces, pos = [], 0
    for match in _DATE_RE.finditer(text):
        if _real_date(match.group(0)):
            pieces.append(_mask_runs(text[pos : match.start()], cut=False))
            pieces.append(match.group(0))
            pos = match.end()
    pieces.append(_mask_runs(text[pos:], cut=cut))
    return "".join(pieces)[:limit]


def _mask_runs(text: str, *, cut: bool) -> str:
    def replace(match: re.Match) -> str:
        raw = match.group(0)
        digits = "".join(c for c in raw if c.isdigit())
        if cut and match.end() == len(text) and len(digits) >= 3:
            return MASK
        if len(digits) < 6:
            return raw
        before = text[max(0, match.start() - 2) : match.start()]
        if (before.endswith("$") or before == "$ ") and _DOLLARS.fullmatch(raw):
            return raw
        if _SMALL_AMOUNT.fullmatch(raw):
            return raw
        if raw.isdigit() and len(raw) == 6 and _plain_six_digit_date(raw):
            return raw
        return MASK + digits[-4:]

    return _RUN_RE.sub(replace, text)


UPDATE = """
UPDATE ledger_banktransaction t SET description = v.description
  FROM unnest(%s::bigint[], %s::text[]) AS v(id, description)
  WHERE t.id = v.id
"""


def redact(apps, schema_editor):
    BankTransaction = apps.get_model("ledger", "BankTransaction")
    ids, texts = [], []
    for pk, description in BankTransaction.objects.values_list("id", "description").iterator():
        masked = redact_numbers(description, cut=len(description) >= CUT_AT)
        if masked != description:
            ids.append(pk)
            texts.append(masked)
    if not ids:
        return
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
        cursor.execute(
            "ALTER TABLE ledger_banktransaction "
            "DISABLE TRIGGER ledger_banktransaction_append_only_row"
        )
        cursor.execute(UPDATE, [ids, texts])
        cursor.execute(
            "ALTER TABLE ledger_banktransaction "
            "ENABLE TRIGGER ledger_banktransaction_append_only_row"
        )


class Migration(migrations.Migration):
    dependencies = [("ledger", "0019_entry_move_notices")]

    operations = [migrations.RunPython(redact, reverse_code=migrations.RunPython.noop)]
