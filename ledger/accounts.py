"""Which ownership account a ledger entry belongs to (D33-acctfix-1).

A lot's money is kept per ``community.Account`` (one ownership period):
every entry carries the account it was posted under, and payments and
credits are applied only to charges of the same account. After a sale the
seller's credit (or debt) stays on the old, dormant account and the buyer
starts clean. The lot's balance is the sum over its accounts.

An entry belongs to the account whose period covers its posting date
(``posted_on``, which for a charge is its due date), so a check dated before
the sale is the seller's even when it is entered later. A reversal (or a
recorded refund) belongs to the account of the entry it corrects; a late
fee to the account of the month's dues it penalises; an online payment to
the account of the person who paid (stored on its checkout). Entries dated
before the lot's first account belong to that first account; entries dated
in a gap between two owners belong to the next owner's account. A lot that
has never had an owner keeps its entries without an account (``None``), as a
group of their own.

When the account periods change afterwards (a sale recorded with an earlier
date), entries posted by the rule on the old periods that now belong to
another account are moved (``moves_after_change``, D34-fixmoney-1): each is
reversed on its old account and posted again, unchanged, on the right one
(``LedgerEntry.moved_from``). Nothing is edited; owner pages leave such a
pair out (``MOVED_AWAY``), the board sees both.
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


# An entry moved to another account, and the reversal that took it off this
# one (D34-fixmoney-1): they cancel out, and were never this account's
# owners' money, so owner pages leave them out.
MOVED_AWAY = Q(moved_to__isnull=False) | Q(reverses__moved_to__isnull=False)


@internal("ledger accounts")
def periods(lot_id: int) -> list[tuple[int, datetime.date, datetime.date | None]]:
    """The lot's account periods as (id, opened_on, closed_on), oldest first."""
    from community.models import Account

    return list(
        Account.objects.filter(lot_id=lot_id)
        .order_by("opened_on", "id")
        .values_list("pk", "opened_on", "closed_on")
    )


def account_on(lot_id: int, on: datetime.date) -> int | None:
    """The id of the lot's account for an entry posted on ``on``."""
    return pick(periods(lot_id), on)


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
    if entry.kind == "late_fee" and entry.period:
        dues = _dues_account(entry.lot_id, entry.period)
        if dues is not _NONE:
            return dues
    return account_on(entry.lot_id, entry.posted_on)


_NONE = object()


def _dues_account(lot_id: int, period: str):
    """The account of the month's dues still standing (_NONE: no such dues)."""
    from ledger.models import LedgerEntry

    rows = list(
        LedgerEntry.objects.filter(
            lot_id=lot_id, kind="assessment", period=period, reversed_by__isnull=True
        )
        .order_by("-id")
        .values_list("account_id", flat=True)[:1]
    )
    return rows[0] if rows else _NONE


@internal("ledger accounts")
def moves_after_change(lot_id: int, before: list) -> list[tuple]:
    """The entries to move after the lot's account periods changed from
    ``before`` to what they are now, as (entry, target account id), oldest
    first (D34-fixmoney-1).

    Only entries the rule had put where they are (their account is the one
    the old periods gave their date) move, and only to an account (never to
    "no account"): an account the board chose by hand for an entry, such as
    a refund check for a former owner's credit, is kept. Reversals and
    recorded refunds follow their entry, so a reversed pair stays together
    on its account; online payments stay with the person who paid; a late
    fee moves with the month's dues it penalises."""
    from ledger.models import LedgerEntry
    from payments.services import online_entry_ids

    after = periods(lot_id)
    if after == before:
        return []
    entries = list(LedgerEntry.objects.filter(lot_id=lot_id).order_by("id"))
    reversed_ids = {e.reverses_id for e in entries if e.reverses_id}
    online = online_entry_ids()

    def fixed(e) -> bool:
        return e.kind in ("reversal", "refund") or e.pk in reversed_ids or e.pk in online

    def by_date(e) -> int | None:
        new = pick(after, e.posted_on)
        if e.account_id == pick(before, e.posted_on) and new not in (None, e.account_id):
            return new
        return None

    plan: dict[int, int] = {}
    for e in entries:
        if fixed(e) or (e.kind == "late_fee" and e.period):
            continue
        target = by_date(e)
        if target is not None:
            plan[e.pk] = target
    dues = {
        (e.period, e.account_id): e.pk
        for e in entries
        if e.kind == "assessment" and e.pk not in reversed_ids
    }
    for e in entries:
        if fixed(e) or e.kind != "late_fee" or not e.period:
            continue
        month = dues.get((e.period, e.account_id))
        if month in plan:
            plan[e.pk] = plan[month]
        elif month is None:  # no dues for the month on its account: by its date
            target = by_date(e)
            if target is not None:
                plan[e.pk] = target
    by_pk = {e.pk: e for e in entries}
    return [(by_pk[pk], plan[pk]) for pk in sorted(plan)]


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
