"""What an owner pays online (decision D32-payments, replacing the fee part
of D21): a surcharge on CREDIT cards only. Bank payments (ACH) and debit or
prepaid cards carry no fee: the HOA absorbs their cost (card-network rules
forbid surcharging debit and prepaid cards, and cap credit surcharges at 3%).
Integer cents throughout."""

from __future__ import annotations

from community.rules import rule, rule_with_citation
from core.money import format_cents

BANK, DEBIT, CREDIT = "bank", "debit", "credit"
SURCHARGE_RULE = "payments.credit_card_surcharge_bp"
MAX_SURCHARGE_BP = 300  # 3%: hard cap in code, whatever the rule says


def surcharge_bp() -> int:
    """The credit card surcharge in hundredths of a percent, capped at 3%."""
    value = rule(SURCHARGE_RULE)
    return max(0, min(int(value or 0), MAX_SURCHARGE_BP))


def surcharge(amount_cents: int, method: str) -> int:
    """The surcharge added to a payment of ``amount_cents``: credit cards
    only, rounded up to the next cent; zero for bank and debit."""
    if method not in (BANK, DEBIT, CREDIT):
        raise ValueError("Unknown payment method.")
    if amount_cents <= 0 or method != CREDIT:
        return 0
    return -(-amount_cents * surcharge_bp() // 10_000)  # ceiling division


def percent_text(bp: int) -> str:
    whole, rest = divmod(bp, 100)
    return f"{whole}%" if not rest else f"{whole}.{rest:02d}".rstrip("0") + "%"


def disclosure() -> str:
    """The surcharge notice shown before an owner pays (counsel to confirm the
    wording before live payments; runbook go-live checklist)."""
    bp = surcharge_bp()
    if not bp:
        return "No surcharge applies to any online payment."
    return (
        f"A {percent_text(bp)} surcharge applies to credit card payments; no surcharge on "
        "debit cards or bank payments. The surcharge is not greater than our cost of "
        "accepting credit cards."
    )


def payment_options(amount_cents: int) -> list[dict]:
    """Each way to pay with its fee and total, shown before the redirect."""
    bp = surcharge_bp()
    citation = rule_with_citation(SURCHARGE_RULE)[1]
    out = []
    for method, label in (
        (BANK, "Bank account (no fee)"),
        (DEBIT, "Debit card (no fee)"),
        (CREDIT, f"Credit card (+{percent_text(bp)} surcharge)" if bp else "Credit card"),
    ):
        fee = surcharge(amount_cents, method)
        out.append(
            {
                "method": method,
                "label": label,
                "fee_cents": fee,
                "total_cents": amount_cents + fee,
                "total_text": format_cents(amount_cents + fee),
                "citation": citation if method == CREDIT else "",
            }
        )
    return out


def online_payments_enabled(request=None) -> bool:
    """True only when the board switched the feature on AND a provider is
    set up with a key this server may use, so pages never offer a button
    that cannot work."""
    from core import features
    from payments.providers import config

    return features.enabled("online_payments", request) and config().available


# --- For the accounting export and deposits (ledger.accounting) ----------------------------


def online_entry_ids() -> set[int]:
    """Ledger payment entries that came from Stripe (they reach the bank in
    Stripe payouts, not in deposit batches)."""
    from payments.models import StripePayment

    return set(StripePayment.objects.unscoped("accounting").values_list("entry_id", flat=True))


def journal_items(start, end) -> dict[str, list[tuple]]:
    """Stripe-side journal lines for the period, so the books balance:
    surcharges (income), the surcharge part of every refund (refund_portions),
    Stripe's processing fees (expense) and payouts (Stripe clearing to the
    bank). The payment part of a refund is booked from the owner's account,
    when the board records the refund or reverses the payment."""
    from django.utils import timezone

    from payments.models import StripeBalanceTransaction, StripePayment, StripePayout

    out: dict[str, list[tuple]] = {
        "surcharges": [],
        "surcharge_refunds": [],
        "fees": [],
        "payouts": [],
    }
    for p in StripePayment.objects.unscoped("accounting").select_related("lot"):
        day = timezone.localdate(p.recorded_at)
        if p.surcharge_cents and start <= day <= end:
            out["surcharges"].append((day, f"S{p.pk}", p.surcharge_cents, p.lot))
    for ev, surcharge_part, _base in refund_portions():
        day = timezone.localdate(ev.seen_at)
        if surcharge_part and start <= day <= end:
            out["surcharge_refunds"].append((day, f"E{ev.pk}", surcharge_part, ev.payment.lot))
    for bt in StripeBalanceTransaction.objects.exclude(fee_cents=0):
        day = timezone.localdate(bt.created)
        if start <= day <= end:
            out["fees"].append((day, f"B{bt.pk}", bt.fee_cents, bt.type))
    for po in StripePayout.objects.filter(arrival_date__range=(start, end)):
        out["payouts"].append((po.arrival_date, f"O{po.pk}", po.amount_cents, po.payout_id))
    return out


# --- Refunds (D33-acctfix-5) -----------------------------------------------------------------


def _round_half_up(numerator: int, denominator: int) -> int:
    return (2 * numerator + denominator) // (2 * denominator)


def refund_portions(payment=None) -> list[tuple]:
    """Every refund Stripe reported, split into (event, surcharge part,
    payment part), oldest first per payment.

    The surcharge is refunded in proportion (card-network rules require a
    partial refund to return the surcharge pro rata): after refunds totalling
    R of a payment of T (base B + surcharge S), round(R x S / T) of the
    surcharge has been refunded, and all of it once R reaches T. Each refund
    takes the difference, so the parts always add up and never exceed S. The
    one exception is the debit-card safety net: a refund of exactly the
    surcharge, owed because a non-credit card paid one, is all surcharge."""
    from payments.models import AlertKind, EventKind, PaymentAlert, StripeEvent

    surcharge_due = set(
        PaymentAlert.objects.unscoped("refunds")
        .filter(kind=AlertKind.SURCHARGE_REFUND)
        .values_list("key", flat=True)
    )
    events = (
        StripeEvent.objects.unscoped("refunds")
        .filter(kind=EventKind.REFUND_SEEN)
        .select_related("payment__lot")
        .order_by("payment_id", "seen_at", "id")
    )
    if payment is not None:
        events = events.filter(payment=payment)
    out = []
    totals: dict[int, list[int]] = {}
    for ev in events:
        p = ev.payment
        refunded, surcharge_refunded = totals.setdefault(p.pk, [0, 0])
        total = p.base_cents + p.surcharge_cents
        amount = ev.amount_cents
        if (
            p.payment_intent_id in surcharge_due
            and amount == p.surcharge_cents
            and not surcharge_refunded
        ):
            part = amount
        elif not total or not p.surcharge_cents:
            part = 0
        elif refunded + amount >= total:
            part = p.surcharge_cents - surcharge_refunded
        else:
            cumulative = _round_half_up((refunded + amount) * p.surcharge_cents, total)
            part = max(0, min(cumulative, p.surcharge_cents) - surcharge_refunded)
        part = min(part, amount)
        totals[p.pk] = [refunded + amount, surcharge_refunded + part]
        out.append((ev, part, amount - part))
    return out


def record_refund(alert, *, actor, request=None):
    """The board records a refund Stripe reported (a "Refund to record" item)
    on the owner's account: the payment's share of it becomes a refund entry
    (the surcharge share is booked in the journal by itself), and the item
    closes. Returns the ledger entry, or None when the refund was all
    surcharge."""
    from django.db import transaction
    from django.utils import timezone

    from audit.services import record_event
    from core.money import format_cents
    from core.policy import require
    from ledger import services as ledger
    from payments.models import AlertKind, EventKind, PaymentAlert, StripeEvent

    require(actor, "payments.manage")
    with transaction.atomic():
        alert = PaymentAlert.objects.unscoped("record refund").select_for_update().get(pk=alert.pk)
        if alert.kind != AlertKind.REFUND or alert.resolved_at is not None:
            raise ledger.LedgerError("This item is not a refund waiting to be recorded.")
        event = (
            StripeEvent.objects.unscoped("record refund")
            .filter(kind=EventKind.REFUND_SEEN, stripe_id=alert.key)
            .select_related("payment__entry")
            .first()
        )
        if event is None:
            raise ledger.LedgerError("Stripe's record of this refund was not found.")
        base = next(b for ev, _s, b in refund_portions(event.payment) if ev.pk == event.pk)
        entry = None
        if base:
            entry = ledger.record_online_refund(
                payment=event.payment.entry,
                amount_cents=base,
                refund_id=event.stripe_id,
                actor=actor,
                request=request,
            )
        PaymentAlert.objects.unscoped("record refund").filter(pk=alert.pk).update(
            resolved_at=timezone.now(),
            resolved_by=actor,
            resolution=(
                f"Recorded on the account: {format_cents(base)} of the payment refunded"
                f" ({format_cents(event.amount_cents - base)} surcharge)."
                if base
                else "The refund was all surcharge; nothing to record on the account."
            ),
        )
    record_event(
        "payments.refund_recorded",
        actor=actor,
        request=request,
        target=alert,
        metadata={"alert_id": alert.pk, "amount_cents": base},
    )
    return entry
