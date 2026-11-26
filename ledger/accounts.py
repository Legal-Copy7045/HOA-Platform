"""Which ownership account a ledger entry belongs to (D33-acctfix-1).

A lot's money is kept per ``community.Account`` (one ownership period):
every entry carries the account it was posted under, and payments and
credits are applied only to charges of the same account. After a sale the
seller's credit (or debt) stays on the old, dormant account and the buyer
starts clean. The lot's balance is the sum over its accounts.

An entry belongs to the account whose period covers its posting date
(``posted_on``), so a check dated before the sale is the seller's even when
it is entered later. A reversal (or a recorded refund) belongs to the account
of the entry it corrects. Entries dated before the lot's first account
belong to that first account; entries dated in a gap between two owners
belong to the next owner's account. A lot that has never had an owner keeps
its entries without an account (``None``), as a group of their own.
"""

from __future__ import annotations

import datetime

from django.db.models import Q
from django.utils import timezone

from core.scoping import internal

# Readers take ``account=ALL`` (the whole lot, the default), ``CURRENT`` (the
# account open today: what current owners and contacts see), an account id,
# or None (entries with no account).
ALL = "all"
CURRENT = "current"


@internal("ledger accounts")
def account_on(lot_id: int, on: datetime.date) -> int | None:
    """The id of the lot's account for an entry posted on ``on``."""
    from community.models import Account

    accounts = list(
        Account.objects.filter(lot_id=lot_id)
        .order_by("opened_on", "id")
        .values_list("pk", "opened_on", "closed_on")
    )
    return pick(accounts, on)


def pick(accounts: list[tuple[int, datetime.date, datetime.date | None]], on) -> int | None:
    """The rule on plain (id, opened_on, closed_on) rows, oldest first (also
    used by migration 0015 to assign the entries posted before it)."""
    covering = [
        pk for pk, opened, closed in accounts if opened <= on and (closed is None or closed > on)
    ]
    if covering:
        return covering[-1]
    later = [pk for pk, opened, _closed in accounts if opened > on]
    if later:
        return later[0]  # before the first owner, or between two owners
    return None  # no account yet, or after the last one closed with no owner since


def current_account_id(lot_id: int) -> int | None:
    """The account current owners and contacts see: the one open today."""
    return account_on(lot_id, timezone.localdate())


def resolve(lot_id: int, account):
    """``account`` as an id (or None), or ALL."""
    if account == CURRENT:
        return current_account_id(lot_id)
    return account


def account_q(lot_id: int, account, field: str = "account_id") -> Q:
    """A filter for ``account`` (ALL: no filter)."""
    account = resolve(lot_id, account)
    if account == ALL:
        return Q()
    if account is None:
        return Q(**{f"{field}__isnull": True})
    return Q(**{field: account})


@internal("ledger accounts")
def for_new_entry(entry) -> int | None:
    """The account a new entry belongs to (set when it is first saved)."""
    from ledger.models import LedgerEntry

    if entry.reverses_id:
        return (
            LedgerEntry.objects.filter(pk=entry.reverses_id)
            .values_list("account_id", flat=True)
            .first()
        )
    return account_on(entry.lot_id, entry.posted_on)


@internal("ledger accounts")
def lot_accounts(lot_id: int) -> list:
    """The lot's accounts, newest first, each with its ``balance_cents``."""
    from django.db.models import Sum

    from community.models import Account
    from ledger.models import LedgerEntry

    sums = dict(
        LedgerEntry.objects.filter(lot_id=lot_id)
        .values_list("account_id")
        .annotate(s=Sum("amount_cents"))
        .values_list("account_id", "s")
    )
    out = list(Account.objects.filter(lot_id=lot_id).order_by("-opened_on", "-id"))
    for account in out:
        account.balance_cents = sums.get(account.pk) or 0
    return out


@internal("ledger accounts")
def check_account(lot_id: int, account_id) -> int | None:
    """An account chosen for a new entry: one of this lot's, else an error."""
    from community.models import Account
    from ledger.services import LedgerError

    if account_id in (None, ""):
        return None
    try:
        account_id = int(account_id)
    except (TypeError, ValueError) as exc:
        raise LedgerError("Choose one of this home's accounts.") from exc
    if not Account.objects.filter(lot_id=lot_id, pk=account_id).exists():
        raise LedgerError("Choose one of this home's accounts.")
    return account_id
