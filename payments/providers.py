"""Which payment provider is set up, and whether its key may be used here
(decision D32-payments).

Mode safety:

* Test keys (``sk_test_``/``rk_test_``) are refused when
  ``DEPLOYMENT_STAGE=production``: otherwise anyone could "pay" a real
  account with Stripe's test cards.
* Live keys (``sk_live_``/``rk_live_``) are refused unless
  ``DEPLOYMENT_STAGE=production`` and ``PAYMENTS_LIVE_ALLOWED=true``.
* Every object fetched from Stripe must carry the same ``livemode`` (checked
  in payments.checkout).

The key itself is never logged, shown or stored anywhere but the server's
environment. A restricted key (``rk_``) is recommended (runbook).
"""

from __future__ import annotations

from dataclasses import dataclass

from django.conf import settings


class PaymentsNotConfigured(RuntimeError):
    pass


@dataclass(frozen=True)
class Config:
    available: bool
    provider: str
    livemode: bool
    restricted: bool
    problem: str  # why it is not available (no secrets)

    @property
    def mode_label(self) -> str:
        return "Live" if self.livemode else "Test mode"


def config() -> Config:
    if settings.PAYMENTS_PROVIDER != "stripe":
        return Config(False, "none", False, False, "Online payments are not set up.")
    key = settings.STRIPE_SECRET_KEY or ""
    stage = getattr(settings, "DEPLOYMENT_STAGE", "local")
    restricted = key.startswith("rk_")
    if key.startswith(("sk_test_", "rk_test_")):
        if stage == "production":
            return Config(
                False, "stripe", False, restricted, "Test keys are refused in production."
            )
        return Config(True, "stripe", False, restricted, "")
    if key.startswith(("sk_live_", "rk_live_")):
        if stage != "production" or not settings.PAYMENTS_LIVE_ALLOWED:
            return Config(
                False,
                "stripe",
                True,
                restricted,
                "Live keys need DEPLOYMENT_STAGE=production and PAYMENTS_LIVE_ALLOWED=true.",
            )
        return Config(True, "stripe", True, restricted, "")
    if not key:
        return Config(False, "stripe", False, False, "STRIPE_SECRET_KEY is not set.")
    return Config(False, "stripe", False, False, "STRIPE_SECRET_KEY is not a Stripe secret key.")


def secret_key() -> str:
    """The key, only when this server may use it."""
    cfg = config()
    if not cfg.available:
        raise PaymentsNotConfigured(cfg.problem)
    return settings.STRIPE_SECRET_KEY
