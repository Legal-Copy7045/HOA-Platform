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


# A run of six or more digits, single spaces or dashes allowed between them
# (account and routing numbers are often written "0001 2345 6789").
_DIGIT_RUN = re.compile(r"(?<!\d)\d(?:[ -]?\d){5,}(?!\d)")
_AMOUNT_TAIL = re.compile(r"[.,]\d{1,2}(?!\d)")
_DATE_SHAPES = ([6], [8], [4, 2, 2], [2, 2, 4], [2, 2, 2])
MASK = "\u2022\u2022\u2022\u2022"  # "••••"


def _plausible_date(digits: str) -> bool:
    """YYYYMMDD / MMDDYYYY (8 digits) or YYMMDD / MMDDYY (6 digits)."""

    def ok(y, m, d):
        try:
            datetime.date(y, m, d)
        except ValueError:
            return False
        return True

    if len(digits) == 8:
        y1, y2 = int(digits[:4]), int(digits[4:])
        return (1900 <= y1 <= 2100 and ok(y1, int(digits[4:6]), int(digits[6:]))) or (
            1900 <= y2 <= 2100 and ok(y2, int(digits[:2]), int(digits[2:4]))
        )
    if len(digits) == 6:
        a, b, c = int(digits[:2]), int(digits[2:4]), int(digits[4:])
        return ok(2000 + a, b, c) or ok(2000 + c, a, b)
    return False


def redact_numbers(text: str, limit: int = DESCRIPTION_MAX) -> str:
    """Mask long digit runs (account and routing numbers) down to the last four.

    The portal never stores bank account numbers (SEC-004): any run of six or
    more digits becomes "••••1234", unless it is plainly an amount (followed by
    cents, or after a "$") or a date (6 or 8 digits that read as one, written
    without separators or with them). Short numbers such as check numbers
    (five digits or fewer) are kept. Over-masking is preferred to keeping a
    number.
    """

    def replace(match: re.Match) -> str:
        raw = match.group(0)
        digits = "".join(c for c in raw if c.isdigit())
        plain = raw.isdigit()
        before = text[max(0, match.start() - 1) : match.start()]
        if plain and (before == "$" or _AMOUNT_TAIL.match(text, match.end())):
            return raw
        shape = [len(group) for group in re.split(r"[ -]", raw)]
        if shape in _DATE_SHAPES and _plausible_date(digits):
            return raw
        return MASK + digits[-4:]

    return _DIGIT_RUN.sub(replace, text or "")[:limit]


def clean_description(value, limit: int = DESCRIPTION_MAX) -> str:
    """A bank line's description: clean_text, then long numbers masked."""
    return redact_numbers(clean_text(value, limit), limit)


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
