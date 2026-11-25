"""Online payments: a placeholder until the board decides (CLAUDE.md: Stripe
hosted Checkout only; the app never handles card or bank numbers).

To add Stripe later: implement ``StripeCheckoutProvider.start_checkout`` to
create a hosted Checkout Session (amount in cents, the lot's account id in
metadata) and return its URL; add a webhook view that verifies the Stripe
signature and calls ledger.services.record_payment(..., method="online",
reference=<checkout session id>). Keys come from the environment only.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.conf import settings


class PaymentsNotConfigured(RuntimeError):
    pass


@dataclass(frozen=True)
class CheckoutStart:
    redirect_url: str


class PaymentProvider:
    name = "none"
    available = False

    def start_checkout(self, *, lot_id: int, amount_cents: int, user) -> CheckoutStart:
        raise PaymentsNotConfigured("Online payments are not set up yet.")


class StripeCheckoutProvider(PaymentProvider):
    name = "stripe"
    available = False  # becomes True once implemented and keys are configured


def provider() -> PaymentProvider:
    if settings.PAYMENTS_PROVIDER == "stripe":
        return StripeCheckoutProvider()
    return PaymentProvider()
