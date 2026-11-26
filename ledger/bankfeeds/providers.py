"""The two providers: "file" (an uploaded statement) and "teller"."""

from __future__ import annotations

import datetime

from ledger.bankfeeds import tokens
from ledger.bankfeeds.base import AccountInfo, BalanceInfo, BankFeedError, Provider, TransactionInfo
from ledger.bankfeeds.teller import TellerProvider


class FileProvider(Provider):
    """One uploaded statement file: its accounts are the statements in it."""

    name = "file"

    def __init__(self, accounts: list[tuple[AccountInfo, list[TransactionInfo], BalanceInfo]]):
        self._accounts = accounts

    @classmethod
    def from_ofx(cls, statement) -> FileProvider:
        return cls(
            [
                (
                    AccountInfo(
                        external_id=str(i),
                        name="Credit card" if a.kind == "credit" else "Bank account",
                        institution_name="",
                        last_four=a.last_four,
                        kind=a.kind,
                    ),
                    a.transactions,
                    a.balance,
                )
                for i, a in enumerate(statement.accounts)
            ]
        )

    @classmethod
    def from_lines(cls, lines: list[TransactionInfo]) -> FileProvider:
        return cls([(AccountInfo("0", "Bank account", "", ""), lines, None)])

    def list_accounts(self) -> list[AccountInfo]:
        return [a for a, _, _ in self._accounts]

    def _find(self, account_id: str):
        for account, lines, balance in self._accounts:
            if account.external_id == account_id:
                return lines, balance
        raise BankFeedError("That account is not in the file.")

    def transactions(self, account_id: str, since: datetime.date) -> list[TransactionInfo]:
        lines, _ = self._find(account_id)
        return [t for t in lines if t.posted_on >= since]

    def balance(self, account_id: str) -> BalanceInfo | None:
        return self._find(account_id)[1]


def for_connection(connection, *, opener=None) -> Provider:
    """The live provider for a stored connection (Teller only; files are uploaded)."""
    if connection.provider != "teller":
        raise BankFeedError("This account is updated by importing statement files.")
    if not connection.token_encrypted:
        raise BankFeedError("This account is disconnected.")
    return TellerProvider(tokens.decrypt(connection.token_encrypted), opener=opener)
