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
    surcharges (income), surcharge refunds, Stripe's processing fees
    (expense) and payouts (Stripe clearing to the bank)."""
    from django.utils import timezone

    from payments.models import (
        AlertKind,
        EventKind,
        PaymentAlert,
        StripeBalanceTransaction,
        StripeEvent,
        StripePayment,
        StripePayout,
    )

    out: dict[str, list[tuple]] = {
        "surcharges": [],
        "surcharge_refunds": [],
        "fees": [],
        "payouts": [],
    }
    for p in StripePayment.objects.unscoped("accounting").select_related("lot"):
        day = timezone.localdate(p.recorded_at)
        if p.surcharge_cents and start <= day <= end:
            out["surcharges"].append((day, f"S{p.pk}", p.surcharge_cents, p.lot.number))
    surcharge_due = set(
        PaymentAlert.objects.unscoped("accounting")
        .filter(kind=AlertKind.SURCHARGE_REFUND)
        .values_list("key", flat=True)
    )
    events = StripeEvent.objects.unscoped("accounting").filter(kind=EventKind.REFUND_SEEN)
    for ev in events.select_related("payment__lot"):
        day = timezone.localdate(ev.seen_at)
        payment = ev.payment
        if (
            start <= day <= end
            and payment.payment_intent_id in surcharge_due
            and ev.amount_cents == payment.surcharge_cents
        ):
            out["surcharge_refunds"].append((day, f"E{ev.pk}", ev.amount_cents, payment.lot.number))
    for bt in StripeBalanceTransaction.objects.exclude(fee_cents=0):
        day = timezone.localdate(bt.created)
        if start <= day <= end:
            out["fees"].append((day, f"B{bt.pk}", bt.fee_cents, bt.type))
    for po in StripePayout.objects.filter(arrival_date__range=(start, end)):
        out["payouts"].append((po.arrival_date, f"O{po.pk}", po.amount_cents, po.payout_id))
    return out
