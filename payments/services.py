"""Owner-paid convenience fees (decision D21), in integer cents."""

from __future__ import annotations

from community.rules import rule, rule_with_citation

CARD, BANK = "card", "bank"


def convenience_fee(amount_cents: int, method: str) -> int:
    """The fee added to a payment of ``amount_cents`` by ``method``.
    Card fees round up to the next cent; no fee unless the board set one."""
    if amount_cents <= 0:
        return 0
    if method == CARD:
        bp = rule("payments.card_fee_bp") or 0
        return -(-amount_cents * int(bp) // 10_000)  # ceiling division
    if method == BANK:
        return int(rule("payments.bank_fee_cents") or 0)
    raise ValueError("Unknown payment method.")


def payment_options(amount_cents: int) -> list[dict]:
    """What an owner sees before paying: each method with its fee and total."""
    out = []
    for method, label in ((BANK, "Bank transfer"), (CARD, "Card")):
        fee = convenience_fee(amount_cents, method)
        key = "payments.card_fee_bp" if method == CARD else "payments.bank_fee_cents"
        out.append(
            {
                "method": method,
                "label": label,
                "fee_cents": fee,
                "total_cents": amount_cents + fee,
                "citation": rule_with_citation(key)[1],
            }
        )
    return out
