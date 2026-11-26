"""Asking Stripe what happened (decision D32-payments): the poller that
records payments, notices failures, returns, refunds and disputes, copies
the Stripe balance and payouts, and the daily ledger-versus-Stripe
reconciliation.

Nothing here edits a recorded payment. A returned bank payment becomes a
reversing ledger entry; a refund or dispute becomes a board My item (the
board records it); the portal never refunds (its key cannot).
"""

from __future__ import annotations

import datetime
import logging

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from audit.services import record_event
from core.money import format_cents
from core.policy import SYSTEM
from core.scoping import internal, system_access
from payments import stripe_api
from payments.checkout import _alert, check
from payments.models import (
    AlertKind,
    CheckoutState,
    EventKind,
    PaymentAlert,
    PendingCheckout,
    StripeBalanceTransaction,
    StripeEvent,
    StripePayment,
    StripePayout,
    StripePayoutItem,
    StripeReconciliation,
)
from payments.providers import config

logger = logging.getLogger("payments")

OPEN_HOURS = 24
UNANSWERED_DAYS = 3  # an open checkout Stripe has not answered about for this long
PROCESSING_DAYS = 14
WATCH_DAYS = 120  # refunds, disputes and bank returns after a payment
UNMATCHED_HOURS = 24
PAYMENT_TYPES = {"charge", "payment"}
REFUND_TYPES = {"refund", "payment_refund", "payment_failure_refund"}


# --- Checkouts -----------------------------------------------------------------------------


@internal("payments poll")
def poll_open_checkouts(now=None) -> int:
    """Every 15 minutes: checkouts still open. A checkout is marked expired
    only from Stripe's answer (the session expired, or is past its time and
    not paid): while Stripe cannot be asked (network, rate limit), it stays
    open and is asked again, so a payment made near the end is never lost
    (R2-004). One Stripe has not answered about for days becomes a board
    item."""
    if not config().available:
        return 0
    now = now or timezone.now()
    count = 0
    for checkout in PendingCheckout.objects.filter(
        state=CheckoutState.OPEN, session_id__isnull=False
    ):
        check(checkout, now=now)
        count += 1
        if checkout.state == CheckoutState.OPEN and checkout.created_at < now - (
            datetime.timedelta(hours=OPEN_HOURS) + datetime.timedelta(days=UNANSWERED_DAYS)
        ):
            _alert(
                AlertKind.MISMATCH,
                f"unanswered-{checkout.pk}",
                lot_id=checkout.lot_id,
                amount=checkout.total_cents,
                ref=checkout.session_id or "",
                detail=f"Stripe has not answered about online payment #{checkout.pk} for over "
                f"{UNANSWERED_DAYS} days. Check it in the Stripe dashboard.",
            )
    return count


@internal("payments poll")
def poll_processing(now=None) -> int:
    """Daily: bank payments still processing (ACH takes a few business days)."""
    if not config().available:
        return 0
    now = now or timezone.now()
    count = 0
    for checkout in PendingCheckout.objects.filter(state=CheckoutState.PROCESSING):
        check(checkout)
        count += 1
        if checkout.state == CheckoutState.PROCESSING and checkout.created_at < now - (
            datetime.timedelta(days=PROCESSING_DAYS)
        ):
            _alert(
                AlertKind.MISMATCH,
                f"stuck-{checkout.pk}",
                lot_id=checkout.lot_id,
                amount=checkout.total_cents,
                ref=checkout.payment_intent_id or "",
                detail=f"Bank payment #{checkout.pk} has been processing for over "
                f"{PROCESSING_DAYS} days. Check it in the Stripe dashboard.",
            )
    return count


# --- After a payment: returns, refunds, disputes -------------------------------------------


def _event(payment, kind, stripe_id, amount=0) -> bool:
    """Record what we saw once; False if already seen."""
    try:
        with transaction.atomic():
            StripeEvent.objects.create(
                payment=payment, kind=kind, stripe_id=stripe_id[:255], amount_cents=amount
            )
        return True
    except IntegrityError:
        return False


@internal("payments poll")
def poll_recorded(now=None) -> int:
    """Daily: recorded payments of the last 120 days."""
    if not config().available:
        return 0
    now = now or timezone.now()
    count = 0
    for payment in StripePayment.objects.filter(
        recorded_at__gte=now - datetime.timedelta(days=WATCH_DAYS)
    ).select_related("lot", "entry", "checkout"):
        try:
            _watch(payment)
        except stripe_api.StripeError:
            continue
        except Exception as exc:  # one payment's problem never stops the others (BUG-004)
            # The error's type only: its message may carry data.
            logger.warning(
                "payments.watch_failed",
                extra={"payment": payment.pk, "error": type(exc).__name__},
            )
            continue
        count += 1
    return count


def _returned(payment: StripePayment) -> None:
    """A bank payment returned after it succeeded: reverse it on the account
    and tell the board, all or nothing, with the "seen" mark in the same
    transaction (BUG-004), so a failure is tried again on the next run. If
    part of it is recorded as refunded, the portal does not guess (did the
    refund go out before the return?): the board gets an item to reverse it
    by hand."""
    from ledger import services as ledger
    from ledger.models import LedgerEntry

    with transaction.atomic():
        if not _event(payment, EventKind.RETURNED, payment.charge_id, payment.base_cents):
            return
        done = LedgerEntry.objects.filter(reverses=payment.entry).exists()
        if not done:
            try:
                with transaction.atomic():
                    ledger.reverse_entry(
                        payment.entry, reason="Bank payment returned (Stripe)", actor=SYSTEM
                    )
                done = True
            except ledger.LedgerError:
                done = False
        amount = format_cents(payment.base_cents)
        if done:
            detail = (
                f"Bank payment of {amount} on lot {payment.lot.number} was returned; the "
                "payment was reversed on the account."
            )
        else:
            detail = (
                f"Bank payment of {amount} on lot {payment.lot.number} was returned, but part "
                "of it is recorded as refunded. Check at Stripe whether that refund went out; "
                "reverse the refund entry if it did not, then reverse the payment."
            )
        _alert(
            AlertKind.RETURNED,
            payment.charge_id,
            lot_id=payment.lot_id,
            amount=payment.base_cents,
            ref=payment.payment_intent_id,
            detail=detail,
        )
        record_event(
            "payments.returned" if done else "payments.return_needs_board",
            actor=None,
            target=payment.checkout,
            metadata={"lot_id": payment.lot_id, "checkout_id": payment.checkout_id},
        )


def _watch(payment: StripePayment) -> None:
    charge = stripe_api.retrieve_charge(payment.charge_id)
    if bool(charge.get("livemode")) != payment.livemode:
        return
    if charge.get("status") == "failed":
        _returned(payment)
    for refund in stripe_api.list_refunds(payment.charge_id).get("data") or []:
        if not isinstance(refund, dict) or refund.get("status") not in ("succeeded", "pending"):
            continue
        refund_id, amount = str(refund.get("id") or ""), int(refund.get("amount") or 0)
        if not refund_id or not _event(payment, EventKind.REFUND_SEEN, refund_id, amount):
            continue
        record_event(
            "payments.refund_seen",
            actor=None,
            target=payment.checkout,
            metadata={"checkout_id": payment.checkout_id, "amount_cents": amount},
        )
        due = PaymentAlert.objects.filter(
            kind=AlertKind.SURCHARGE_REFUND,
            key=payment.payment_intent_id,
            resolved_at__isnull=True,
        ).first()
        if due is not None and amount == payment.surcharge_cents:
            PaymentAlert.objects.filter(pk=due.pk).update(
                resolved_at=timezone.now(), resolution="Surcharge refund seen at Stripe."
            )
            continue
        _alert(
            AlertKind.REFUND,
            refund_id,
            lot_id=payment.lot_id,
            amount=amount,
            ref=payment.payment_intent_id,
            detail=f"A refund of {format_cents(amount)} was made at Stripe for lot "
            f"{payment.lot.number}. Record it on the lot's account (reverse or adjust).",
        )
    for dispute in stripe_api.list_disputes(payment.charge_id).get("data") or []:
        if not isinstance(dispute, dict):
            continue
        dispute_id, amount = str(dispute.get("id") or ""), int(dispute.get("amount") or 0)
        if not dispute_id or not _event(payment, EventKind.DISPUTE_SEEN, dispute_id, amount):
            continue
        record_event(
            "payments.dispute_seen",
            actor=None,
            target=payment.checkout,
            metadata={"checkout_id": payment.checkout_id},
        )
        _alert(
            AlertKind.DISPUTE,
            dispute_id,
            lot_id=payment.lot_id,
            amount=amount,
            ref=payment.payment_intent_id,
            detail=f"The payer disputed {format_cents(amount)} on lot {payment.lot.number}. "
            "Answer it in the Stripe dashboard, then record the outcome on the account.",
        )


# --- Balance and payouts -------------------------------------------------------------------


def _paged(fetch, params: dict, limit_pages: int = 20):
    params = {**params, "limit": 100}
    for _ in range(limit_pages):
        page = fetch(params)
        data = [d for d in (page.get("data") or []) if isinstance(d, dict)]
        yield from data
        if not page.get("has_more") or not data:
            return
        params = {**params, "starting_after": data[-1].get("id")}


def _store_bt(bt: dict, livemode: bool) -> None:
    source = bt.get("source")
    source_id = source.get("id") if isinstance(source, dict) else (source or "")
    try:
        with transaction.atomic():
            StripeBalanceTransaction.objects.create(
                bt_id=str(bt["id"])[:255],
                type=str(bt.get("type") or "")[:40],
                source_id=str(source_id)[:255],
                amount_cents=int(bt.get("amount") or 0),
                fee_cents=int(bt.get("fee") or 0),
                net_cents=int(bt.get("net") or 0),
                created=datetime.datetime.fromtimestamp(
                    int(bt.get("created") or 0), tz=datetime.UTC
                ),
                livemode=livemode,
            )
    except IntegrityError:
        pass


def sync_balance(now=None, days: int = 7) -> dict:
    """Daily: copy the Stripe balance lines and paid-out payouts."""
    cfg = config()
    if not cfg.available:
        return {"balance": 0, "payouts": 0}
    now = now or timezone.now()
    since = int((now - datetime.timedelta(days=days)).timestamp())
    seen = 0
    for bt in _paged(stripe_api.list_balance_transactions, {"created": {"gte": since}}):
        if bool(bt.get("livemode", cfg.livemode)) != cfg.livemode or not bt.get("id"):
            continue
        _store_bt(bt, cfg.livemode)
        seen += 1
    payouts = 0
    for po in _paged(stripe_api.list_payouts, {"created": {"gte": since}}):
        if po.get("status") != "paid" or bool(po.get("livemode")) != cfg.livemode:
            continue
        payout_id = str(po.get("id") or "")
        if not payout_id or StripePayout.objects.filter(payout_id=payout_id).exists():
            continue
        items = list(_paged(stripe_api.list_balance_transactions, {"payout": payout_id}))
        gross = refunds = fees = other = 0
        for bt in items:
            if bt.get("type") == "payout":
                continue
            _store_bt(bt, cfg.livemode)
            amount, fee = int(bt.get("amount") or 0), int(bt.get("fee") or 0)
            fees += fee
            if bt.get("type") in PAYMENT_TYPES:
                gross += amount
            elif bt.get("type") in REFUND_TYPES:
                refunds += -amount
            else:
                other += amount
        with transaction.atomic():
            payout = StripePayout.objects.create(
                payout_id=payout_id[:255],
                amount_cents=int(po.get("amount") or 0),
                gross_cents=gross,
                refunds_cents=refunds,
                fees_cents=fees,
                other_cents=other,
                arrival_date=datetime.datetime.fromtimestamp(
                    int(po.get("arrival_date") or 0), tz=datetime.UTC
                ).date(),
                status="paid",
                livemode=cfg.livemode,
            )
            for bt in items:
                if bt.get("type") != "payout" and bt.get("id"):
                    StripePayoutItem.objects.create(payout=payout, bt_id=str(bt["id"])[:255])
        payouts += 1
    return {"balance": seen, "payouts": payouts}


def payouts_for_matching(start: datetime.date, end: datetime.date) -> list[dict]:
    """Stripe payouts arriving in the bank between ``start`` and ``end``
    (inclusive), for bank reconciliation: match each ``amount_cents`` (the
    net deposit) to a bank line on ``arrival_date``. Gross payments, refunds
    and fees explain the difference from the online payments recorded. The
    cash book counts each as a deposit on its arrival date (bank
    reconciliation, D33-acctfix-6)."""
    return [
        {
            "id": p.pk,
            "status": p.status,
            "payout_id": p.payout_id,
            "arrival_date": p.arrival_date,
            "amount_cents": p.amount_cents,
            "gross_cents": p.gross_cents,
            "refunds_cents": p.refunds_cents,
            "fees_cents": p.fees_cents,
            "other_cents": p.other_cents,
            "adds_up": p.adds_up,
        }
        for p in StripePayout.objects.filter(
            arrival_date__gte=start, arrival_date__lte=end
        ).order_by("arrival_date", "id")
    ]


# --- Three-way reconciliation ---------------------------------------------------------------


PAYOUT_BANK_DAYS = 3  # a payout lands within this many days of its arrival date


def check_payouts_against_bank(payouts: list[StripePayout]) -> list[dict]:
    """Check (c) of the reconciliation: every paid-out payout must appear as a
    deposit of exactly its net amount in the bank feed, within
    PAYOUT_BANK_DAYS of its arrival date. Only dates the bank feed already
    covers are judged (no feed, or not imported that far yet: nothing to
    say). Each bank line matches one payout at most. Returns problems as
    {"code", "id"}."""
    from ledger.bankfeeds import services as bank

    through = bank.lines_through()
    if through is None:
        return []
    due = sorted(
        (
            p
            for p in payouts
            if p.status == "paid"
            and p.arrival_date + datetime.timedelta(days=PAYOUT_BANK_DAYS) <= through
        ),
        key=lambda p: (p.arrival_date, p.id),
    )
    if not due:
        return []
    window = datetime.timedelta(days=PAYOUT_BANK_DAYS)
    deposits = bank.deposits_between(due[0].arrival_date - window, due[-1].arrival_date + window)
    used: set[int] = set()
    problems = []
    for p in due:
        match = next(
            (
                line_id
                for line_id, posted_on, cents in deposits
                if line_id not in used
                and cents == p.amount_cents
                and abs((posted_on - p.arrival_date).days) <= PAYOUT_BANK_DAYS
            ),
            None,
        )
        if match is None:
            problems.append({"code": "payout_not_in_bank", "id": p.payout_id})
        else:
            used.add(match)
    return problems


@internal("payments reconcile")
def reconcile(now=None) -> StripeReconciliation | None:
    """Daily, after sync_balance: (a) online payments in the ledger against
    (b) Stripe's balance lines and payouts, and (c) the bank (hook above).
    Any difference fails the "Online payments" health check, emails the
    board and sysadmins, and gives the treasurer a My item."""
    cfg = config()
    if not cfg.available:
        return None
    now = now or timezone.now()
    cutoff = now - datetime.timedelta(hours=UNMATCHED_HOURS)
    problems: list[dict] = []
    stripe_charges = set(
        StripeBalanceTransaction.objects.filter(
            type__in=PAYMENT_TYPES, livemode=cfg.livemode
        ).values_list("source_id", flat=True)
    )
    payments = list(StripePayment.objects.filter(livemode=cfg.livemode))
    recorded_charges = {p.charge_id for p in payments}
    for p in payments:
        if p.recorded_at < cutoff and p.charge_id not in stripe_charges:
            problems.append({"code": "ledger_without_stripe", "id": p.payment_intent_id})
    for bt in StripeBalanceTransaction.objects.filter(
        type__in=PAYMENT_TYPES, livemode=cfg.livemode, created__lt=cutoff
    ):
        if bt.source_id not in recorded_charges:
            problems.append({"code": "stripe_without_ledger", "id": bt.source_id})
    payouts = list(StripePayout.objects.filter(livemode=cfg.livemode))
    for po in payouts:
        if not po.adds_up:
            problems.append({"code": "payout_does_not_add_up", "id": po.payout_id})
    problems += check_payouts_against_bank(payouts)
    run = StripeReconciliation.objects.create(
        ran_at=now,
        ok=not problems,
        problems=problems[:200],
        checked_payments=len(payments),
        checked_payouts=len(payouts),
    )
    record_event(
        "payments.reconciled",
        actor=None,
        metadata={"ok": run.ok, "problems": len(problems)},
    )
    if problems:
        _alert(
            AlertKind.RECONCILIATION,
            f"reconcile-{timezone.localdate(now).isoformat()}",
            detail=f"{len(problems)} difference(s) between the ledger and Stripe "
            f"({', '.join(sorted({p['code'] for p in problems}))}). See Online payments.",
        )
        _email_board_and_sysadmins(len(problems))
    return run


def _email_board_and_sysadmins(count: int) -> None:
    from comms.notify import send_now
    from core.health import sysadmin_emails

    with system_access("reconciliation alert"):
        from community.models import BoardSeat

        board = [
            seat.person.user.email
            for seat in BoardSeat.objects.active().select_related("person__user")
            if seat.person.user_id and seat.person.user.is_active
        ]
    send_now(
        set(board) | set(sysadmin_emails()),
        subject=f"[{settings.SITE_NAME}] Online payments: records do not match Stripe",
        body=(
            f"The daily check found {count} difference(s) between online payments in the "
            "portal and Stripe's records. Nothing was changed.\n\nSee Finance -> Online "
            f"payments:\n{settings.PORTAL_BASE_URL}/finance/online-payments/\n\n-- \n"
            f"{settings.SITE_NAME}"
        ),
    )


def health() -> tuple[bool, str]:
    """For core.health: the latest reconciliation and open mismatches."""
    cfg = config()
    if not cfg.available:
        return True, "Not set up" if cfg.provider == "none" else f"Off: {cfg.problem}"
    with system_access("payments health"):
        mismatches = PaymentAlert.objects.filter(
            kind__in=[AlertKind.MISMATCH, AlertKind.RECONCILIATION], resolved_at__isnull=True
        ).count()
    last = StripeReconciliation.objects.first()
    if last is None:
        if mismatches:
            return False, f"{mismatches} payment problem(s) waiting for the board"
        return True, f"{cfg.mode_label}; not reconciled yet"
    hours = int((timezone.now() - last.ran_at).total_seconds() // 3600)
    if not last.ok:
        return False, f"{len(last.problems)} difference(s) with Stripe ({hours} h ago)"
    if mismatches:
        return False, f"{mismatches} payment problem(s) waiting for the board"
    if hours > 36:
        return False, f"No reconciliation for {hours} h"
    return True, f"{cfg.mode_label}; reconciled {hours} h ago"
