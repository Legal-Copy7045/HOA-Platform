"""The provider interface and the shapes every source produces.

A provider lists the bank accounts it can read and fetches an account's
posted transactions since a date. Amounts are signed integer cents, positive
for money into the account. Text from the bank is hostile: descriptions are
capped and stripped of control characters before anything else sees them.
"""

from __future__ import annotations

import datetime
import re
import unicodedata
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation

DESCRIPTION_MAX = 200
BANK_ID_MAX = 255
KIND_MAX = 40
MAX_ABS_CENTS = 10**11  # one billion dollars: anything bigger is a broken file


class BankFeedError(ValueError):
    """A refusal or failure, with a message safe to show to the board."""


@dataclass(frozen=True)
class AccountInfo:
    external_id: str  # the provider's opaque id ("" for files)
    name: str
    institution_name: str
    last_four: str  # digits only, at most four; never more of the number
    kind: str = ""  # depository, credit...


@dataclass(frozen=True)
class TransactionInfo:
    bank_id: str
    posted_on: datetime.date
    amount_cents: int
    description: str
    kind: str = ""


@dataclass(frozen=True)
class BalanceInfo:
    as_of: datetime.date
    balance_cents: int


class Provider:
    """What every bank source offers."""

    name = ""

    def list_accounts(self) -> list[AccountInfo]:
        raise NotImplementedError

    def transactions(self, account_id: str, since: datetime.date) -> list[TransactionInfo]:
        raise NotImplementedError

    def balance(self, account_id: str) -> BalanceInfo | None:
        return None


_SPACES = re.compile(r"\s+")


def clean_text(value, limit: int = DESCRIPTION_MAX) -> str:
    """Printable text only (no control or format characters), one line, capped."""
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = "".join(" " if unicodedata.category(c).startswith("C") else c for c in text)
    return _SPACES.sub(" ", text).strip()[:limit]


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


# How much of a bank's text is read before masking (masking comes before the
# cut to DESCRIPTION_MAX, so a number at the cap is never half kept).
RAW_MAX = 4096


def clean_description(value, limit: int = DESCRIPTION_MAX) -> str:
    """A bank line's description: clean_text, long numbers masked, then cut."""
    text = clean_text(value, RAW_MAX)
    return redact_numbers(text, limit, cut=len(text) >= RAW_MAX)


def last_four(value) -> str:
    """The last four digits of an account number, and nothing more of it."""
    digits = "".join(c for c in str(value or "") if c.isdigit())
    return digits[-4:]


def to_cents(raw) -> int:
    """'1,234.56', '-12.5', '(40.00)', '$7' -> cents. Raises BankFeedError."""
    text = str(raw or "").strip().replace("$", "").replace(" ", "")
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1]
    if "," in text and "." not in text and re.fullmatch(r"-?\d+,\d{1,2}", text):
        text = text.replace(",", ".")  # decimal comma
    text = text.replace(",", "")
    if not re.fullmatch(r"[+-]?\d{1,12}(\.\d{0,4})?", text):
        raise BankFeedError("An amount in the file is not a number.")
    try:
        cents = int((Decimal(text) * 100).quantize(Decimal(1), rounding=ROUND_HALF_EVEN))
    except InvalidOperation as exc:
        raise BankFeedError("An amount in the file is not a number.") from exc
    if negative:
        cents = -abs(cents)
    if abs(cents) >= MAX_ABS_CENTS:
        raise BankFeedError("An amount in the file is too large.")
    return cents
