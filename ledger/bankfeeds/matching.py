"""Matching bank lines to the cash book, for the monthly reconciliation.

Suggestions need the exact amount and a nearby date:
- money in: a deposit batch dated within 5 days of the bank line, or a
  Stripe payout arriving within 3 days of it (payments.sync.PAYOUT_BANK_DAYS;
  online payments reach the bank only in payouts, D33-acctfix-6);
- money out: a vendor payment dated up to 5 days after or 45 days before
  the bank line (a mailed check clears weeks later), or a deposited owner
  payment that bounced (returned item), within 5 days.

A person confirms each match. Confirmed matches are ticked in advance on the
reconciliation page, where recording the statement clears them (the existing
ReconciledItem mechanism). Nothing is posted to the ledger from here.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone

from audit.services import record_event
from core.policy import require
from ledger import accounting as acc
from ledger.bankfeeds.base import BankFeedError
from ledger.models import AccountingSetup, BankMatch, BankTransaction, DepositBatch, ReconciledItem

DEPOSIT_DAYS = 5
PAYMENT_DAYS_BEFORE = 45  # paid on, up to this long before the bank line
PAYMENT_DAYS_AFTER = 5
RETURNED_DAYS = 5
SUGGESTIONS_PER_LINE = 3


@dataclass(frozen=True)
class Candidate:
    kind: str  # deposit | payment | returned | payout
    object_id: int
    on: datetime.date
    amount_cents: int
    label: str
    days_apart: int


def active_matches():
    """Matches confirmed and not undone."""
    undone = BankMatch.objects.filter(undoes__isnull=False).values_list("undoes_id", flat=True)
    return BankMatch.objects.filter(undoes__isnull=True).exclude(pk__in=list(undone))


def _taken() -> tuple[set[int], set[tuple[str, int]]]:
    rows = list(active_matches().values_list("transaction_id", "kind", "object_id"))
    return {r[0] for r in rows}, {(r[1], r[2]) for r in rows}


def _open_items(today):
    """Cash-book items not on a reconciled statement and not matched yet."""
    _, taken_items = _taken()
    items = acc.uncleared(today + datetime.timedelta(days=PAYMENT_DAYS_BEFORE))
    voided = set(
        DepositBatch.objects.filter(voids__isnull=False).values_list("voids_id", flat=True)
    )
    out: list[Candidate] = []
    for d in items["deposits"]:
        if d.voids_id or d.pk in voided or ("deposit", d.pk) in taken_items:
            continue
        ref = f" · {d.bank_reference}" if d.bank_reference else ""
        out.append(Candidate("deposit", d.pk, d.deposited_on, d.total_cents, f"Deposit{ref}", 0))
    for p in items["payments"]:
        if ("payment", p.pk) in taken_items:
            continue
        ref = f" · {p.reference}" if p.reference else ""
        label = f"Payment to {p.invoice.vendor.name}{ref}"
        out.append(Candidate("payment", p.pk, p.paid_on, -p.amount_cents, label, 0))
    for p in items["payouts"]:
        if ("payout", p.pk) in taken_items:
            continue
        out.append(
            Candidate(
                "payout", p.pk, p.arrival_date, p.amount_cents, f"Stripe payout {p.payout_id}", 0
            )
        )
    for e in items["returned"]:
        if ("returned", e.pk) in taken_items:
            continue
        out.append(
            Candidate(
                "returned",
                e.pk,
                e.posted_on,
                -e.amount_cents,
                f"Returned payment · Lot {e.lot.number}",
                0,
            )
        )
    return out


def _fits(line: BankTransaction, item: Candidate) -> int | None:
    """Days apart when ``item`` could be ``line``; None when it cannot."""
    if item.amount_cents != line.amount_cents:
        return None
    gap = (line.posted_on - item.on).days  # positive: the bank is later
    if item.kind == "deposit":
        ok = abs(gap) <= DEPOSIT_DAYS
    elif item.kind == "payment":
        ok = -PAYMENT_DAYS_AFTER <= gap <= PAYMENT_DAYS_BEFORE
    elif item.kind == "payout":
        from payments.sync import PAYOUT_BANK_DAYS

        ok = abs(gap) <= PAYOUT_BANK_DAYS
    else:
        ok = abs(gap) <= RETURNED_DAYS
    return abs(gap) if ok else None


def unmatched_lines(connection=None, limit: int = 200):
    taken_lines, _ = _taken()
    qs = BankTransaction.objects.select_related("connection").exclude(pk__in=list(taken_lines))
    if connection is not None:
        qs = qs.filter(connection=connection)
    return list(qs.order_by("-posted_on", "-id")[:limit])


def suggestions(lines=None) -> list[dict]:
    """Each unmatched line with its best candidates (closest date first)."""
    lines = unmatched_lines() if lines is None else lines
    items = _open_items(timezone.localdate())
    out = []
    for line in lines:
        found = []
        for item in items:
            gap = _fits(line, item)
            if gap is not None:
                found.append(
                    Candidate(
                        item.kind, item.object_id, item.on, item.amount_cents, item.label, gap
                    )
                )
        found.sort(key=lambda c: (c.days_apart, c.kind, c.object_id))
        out.append({"line": line, "candidates": found[:SUGGESTIONS_PER_LINE]})
    return out


def confirm(line: BankTransaction, *, kind: str, object_id: int, actor, request=None) -> BankMatch:
    require(actor, "bank_feed.match")
    if kind not in ("deposit", "payment", "returned", "payout"):
        raise BankFeedError("Choose what this bank line is.")
    with transaction.atomic():
        # One lock for matching and reconciling (the same row reconcile() takes).
        AccountingSetup.objects.select_for_update().get(pk=AccountingSetup.get().pk)
        taken_lines, taken_items = _taken()
        if line.pk in taken_lines:
            raise BankFeedError("This bank line is already matched.")
        if (kind, int(object_id)) in taken_items:
            raise BankFeedError("That item is already matched to another bank line.")
        if ReconciledItem.objects.filter(kind=kind, object_id=int(object_id)).exists():
            raise BankFeedError("That item is already on a reconciled statement.")
        item = next(
            (
                c
                for c in _open_items(timezone.localdate())
                if c.kind == kind and c.object_id == int(object_id)
            ),
            None,
        )
        if item is None or _fits(line, item) is None:
            raise BankFeedError("That item does not match this bank line's amount and date.")
        match = BankMatch.objects.create(
            transaction=line, kind=kind, object_id=int(object_id), confirmed_by=actor
        )
    record_event(
        "bank_feed.match_confirmed",
        actor=actor,
        request=request,
        target=match,
        metadata={"connection_id": line.connection_id, "kind": kind, "object_id": int(object_id)},
    )
    return match


def undo(match: BankMatch, *, actor, request=None) -> BankMatch:
    require(actor, "bank_feed.match")
    with transaction.atomic():
        AccountingSetup.objects.select_for_update().get(pk=AccountingSetup.get().pk)
        if match.undoes_id or not active_matches().filter(pk=match.pk).exists():
            raise BankFeedError("This match is already undone.")
        if ReconciledItem.objects.filter(kind=match.kind, object_id=match.object_id).exists():
            raise BankFeedError("This item is on a reconciled statement; the match stays.")
        reversal = BankMatch.objects.create(
            transaction_id=match.transaction_id,
            kind=match.kind,
            object_id=match.object_id,
            undoes=match,
            confirmed_by=actor,
        )
    record_event(
        "bank_feed.match_undone",
        actor=actor,
        request=request,
        target=match,
        metadata={"kind": match.kind, "object_id": match.object_id},
    )
    return reversal


def matched_for_statement(statement_date: datetime.date) -> dict[str, set[int]]:
    """Cash-book items whose bank line posted by the statement date: ticked
    in advance on the reconciliation page."""
    out: dict[str, set[int]] = {k: set() for k in ("deposit", "payment", "returned", "payout")}
    for kind, object_id in (
        active_matches()
        .filter(transaction__posted_on__lte=statement_date)
        .values_list("kind", "object_id")
    ):
        out[kind].add(object_id)
    return out


def recent_matches(limit: int = 30):
    return list(
        active_matches()
        .select_related("transaction__connection")
        .order_by("-confirmed_at", "-id")[:limit]
    )
