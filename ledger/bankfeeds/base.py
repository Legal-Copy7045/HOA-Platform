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
