"""A small, defensive OFX/QFX statement reader (OFX 1.x SGML and 2.x XML).

Hand-written on purpose: no XML library, so no DTDs, no external entities and
no entity expansion. A file declaring a DOCTYPE, ENTITY or CDATA section is
refused. Only the five XML entities and numeric character references are
decoded; anything else stays as written. Sizes are capped before parsing.

From the account number (ACCTID) only the last four digits are kept; the
routing number (BANKID) is never read.
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime
import hashlib
import html
import re
from dataclasses import dataclass, field

from ledger.bankfeeds.base import (
    BANK_ID_MAX,
    KIND_MAX,
    RAW_MAX,
    BalanceInfo,
    BankFeedError,
    TransactionInfo,
    clean_description,
    clean_text,
    last_four,
    to_cents,
)

MAX_BYTES = 4 * 1024 * 1024
MAX_TOKENS = 400_000
MAX_TRANSACTIONS = 20_000
MAX_VALUE = 1024

_FORBIDDEN = re.compile(rb"<!\s*(doctype|entity|\[cdata\[|element|attlist)", re.IGNORECASE)
_TOKEN = re.compile(r"<(/?)([A-Za-z0-9_.]{1,40})\s*>([^<]{0,4096})")  # text capped at 4 KB
_ENTITY = re.compile(r"&(amp|lt|gt|quot|apos|#[0-9]{1,7}|#x[0-9A-Fa-f]{1,6});")
_STATEMENTS = {"STMTRS": "bank", "CCSTMTRS": "credit"}
_TXN_FIELDS = {"TRNTYPE", "DTPOSTED", "TRNAMT", "FITID", "NAME", "MEMO", "CHECKNUM", "PAYEE"}


@dataclass
class StatementAccount:
    kind: str = "bank"
    last_four: str = ""
    transactions: list[TransactionInfo] = field(default_factory=list)
    balance: BalanceInfo | None = None


@dataclass
class Statement:
    accounts: list[StatementAccount] = field(default_factory=list)
    skipped: int = 0


def _decode_entities(text: str) -> str:
    return _ENTITY.sub(lambda m: html.unescape(m.group(0)), text)


def _text(data: bytes) -> str:
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    for encoding in ("utf-8", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def ofx_date(value: str) -> datetime.date:
    """'20261115', '20261115120000.000[-5:EST]' -> a date."""
    digits = (value or "").strip()[:8]
    if not re.fullmatch(r"\d{8}", digits):
        raise BankFeedError("A date in the file is not valid.")
    try:
        return datetime.date(int(digits[:4]), int(digits[4:6]), int(digits[6:8]))
    except ValueError as exc:
        raise BankFeedError("A date in the file is not valid.") from exc


def _transaction(values: dict[str, str]) -> TransactionInfo:
    posted = ofx_date(values.get("DTPOSTED", ""))
    amount = to_cents(values.get("TRNAMT", ""))
    # Read in full and masked before the cut (fixres BUG-004).
    name = clean_text(values.get("NAME") or values.get("PAYEE"), RAW_MAX)
    memo = clean_text(values.get("MEMO"), RAW_MAX)
    description = name
    if memo and memo.casefold() != name.casefold():
        description = f"{name} · {memo}" if name else memo
    if values.get("CHECKNUM") and not description:
        description = f"Check {clean_text(values['CHECKNUM'], 20)}"
    description = clean_description(description)  # never keep account numbers
    fitid = clean_text(values.get("FITID"), BANK_ID_MAX - 4)
    # FITID is required by the standard; without one, parse() keys the line by
    # (date, amount, description) and how often it already appeared in the file.
    bank_id = f"ofx:{fitid}" if fitid else ""
    return TransactionInfo(
        bank_id=bank_id,
        posted_on=posted,
        amount_cents=amount,
        description=description,
        kind=clean_text(values.get("TRNTYPE"), KIND_MAX).lower(),
    )


def _line_key(info: TransactionInfo, seen: dict[str, int]) -> str:
    """A stable id for a line without FITID: two identical same-day lines in one
    file both count (occurrence 0 and 1), and re-importing the file adds nothing."""
    key = f"{info.posted_on}|{info.amount_cents}|{info.description.casefold()}"
    n = seen.get(key, 0)
    seen[key] = n + 1
    suffix = f"|{n}" if n else ""  # the first occurrence keeps its earlier id
    return f"ofxh:{hashlib.sha256((key + suffix).encode()).hexdigest()[:48]}"


def parse(data: bytes) -> Statement:
    if len(data) > MAX_BYTES:
        raise BankFeedError("The file is too large for a bank statement.")
    if _FORBIDDEN.search(data):
        raise BankFeedError("The file uses features that are not allowed in a bank statement.")
    text = _text(data)
    start = text.upper().find("<OFX>")
    if start < 0:
        raise BankFeedError("This is not an OFX or QFX bank statement.")
    statement = Statement()
    stack: list[str] = []
    account: StatementAccount | None = None
    txn: dict[str, str] | None = None
    balance: dict[str, str] | None = None
    seen_ids: set[str] = set()
    occurrences: dict[str, int] = {}  # per statement account, see _line_key
    tokens = 0
    for match in _TOKEN.finditer(text, start):
        tokens += 1
        if tokens > MAX_TOKENS:
            raise BankFeedError("The file is too large for a bank statement.")
        closing, name, value = match.group(1) == "/", match.group(2).upper(), match.group(3)
        value = _decode_entities(value.strip())[:MAX_VALUE]
        if closing:
            if name in stack:
                while stack and stack.pop() != name:
                    pass
            if name == "STMTTRN" and txn is not None and account is not None:
                try:
                    info = _transaction(txn)
                except BankFeedError:
                    statement.skipped += 1
                else:
                    if not info.bank_id:
                        info = dataclasses.replace(info, bank_id=_line_key(info, occurrences))
                    if info.bank_id not in seen_ids:
                        seen_ids.add(info.bank_id)
                        account.transactions.append(info)
                        if len(account.transactions) > MAX_TRANSACTIONS:
                            raise BankFeedError("The file has too many transactions.")
                txn = None
            elif name == "LEDGERBAL" and balance is not None and account is not None:
                with contextlib.suppress(BankFeedError):
                    account.balance = BalanceInfo(
                        as_of=ofx_date(balance.get("DTASOF", "")),
                        balance_cents=to_cents(balance.get("BALAMT", "")),
                    )
                balance = None
            elif name in _STATEMENTS and account is not None:
                statement.accounts.append(account)
                account = None
            continue
        if value:  # a leaf element: <TAG>value (SGML) or <TAG>value</TAG> (XML)
            parent = stack[-1] if stack else ""
            if txn is not None and name in _TXN_FIELDS:
                txn[name] = value
            elif balance is not None and name in ("BALAMT", "DTASOF"):
                balance[name] = value
            elif account is not None and name == "ACCTID" and parent.endswith("ACCTFROM"):
                account.last_four = last_four(value)  # the rest of the number is dropped
            continue
        stack.append(name)
        if name in _STATEMENTS:
            account = StatementAccount(kind=_STATEMENTS[name])
            occurrences = {}
        elif name == "STMTTRN":
            txn = {}
        elif name == "LEDGERBAL":
            balance = {}
    if account is not None:  # unclosed SGML at the end of the file
        statement.accounts.append(account)
    if not statement.accounts:
        raise BankFeedError("No account statement was found in the file.")
    return statement
