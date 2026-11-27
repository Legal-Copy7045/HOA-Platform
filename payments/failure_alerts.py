"""Payment processing failures, for the sysadmins (D74).

Distinct from the "Online payments" health check (reconciliation and the
board's open payment problems): this one says, in counts and plain words,
when payments are failing in a way that points at the system rather than
at one payer:

* online payments that did not match Stripe's record (checkout or automatic
  payment set-up verification), which are never recorded;
* automatic payments that could not be made for a technical reason (no
  saved payment method, Stripe errors), as opposed to a bank's decline;
* Stripe not answering or refusing the key (counted per hour by
  ``payments.stripe_api``);
* more declines in a day than usual (a burst suggests a problem with the
  account or the payment page rather than one card).

A single decline is normal: the payer is emailed and, for automatic
payments, the board has a My item. No names, homes, amounts or Stripe ids
here.
"""

from __future__ import annotations

import datetime

from django.utils import timezone

from core.scoping import system_access

WINDOW_HOURS = 24
STRIPE_HOURS = 3
# Stripe trouble this many times within STRIPE_HOURS: most polls failed.
STRIPE_ERRORS_ALERT = 6
# This many declines (one-off and automatic) within a day is "more than usual".
DECLINE_BURST = 3
# Autopay failure codes that are the system's, not the payer's bank.
TECHNICAL_CODES = frozenset(
    {"no_payment_method", "mismatch", "network", "retries", "bad_id", "bad_host", "api_error"}
)


def _technical(code: str) -> bool:
    return code in TECHNICAL_CODES or code.startswith("http_")


def figures(now=None) -> dict:
    from core.ops_timeline import tally
    from payments.models import (
        AutopayEvent,
        AutopayEventKind,
        AutopaySetup,
        CheckoutState,
        PendingCheckout,
        SetupState,
    )

    now = now or timezone.now()
    since = now - datetime.timedelta(hours=WINDOW_HOURS)
    with system_access("payment failure figures"):
        checkouts = PendingCheckout.objects.filter(state_changed_at__gte=since)
        one_off_failed = checkouts.filter(
            state=CheckoutState.FAILED, autopay_attempt__isnull=True
        ).count()
        mismatched = checkouts.filter(state=CheckoutState.MISMATCH).count()
        mismatched += AutopaySetup.objects.filter(
            state=SetupState.MISMATCH, state_changed_at__gte=since
        ).count()
        codes = list(
            AutopayEvent.objects.filter(
                kind=AutopayEventKind.FAILED, created_at__gte=since
            ).values_list("code", flat=True)
        )
    technical = sum(1 for c in codes if _technical(c or ""))
    stripe, last_code = tally("stripe", hours=STRIPE_HOURS, now=now)
    return {
        "one_off_declined": one_off_failed,
        "autopay_declined": len(codes) - technical,
        "autopay_technical": technical,
        "mismatched": mismatched,
        "stripe_errors": stripe,
        "stripe_code": last_code,
    }


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def problems(f: dict) -> list[str]:
    out = []
    if f["stripe_errors"] >= STRIPE_ERRORS_ALERT:
        out.append(
            f"Stripe did not answer or refused the portal's key {f['stripe_errors']} times in "
            f"the last {STRIPE_HOURS} hours ({f['stripe_code'] or 'no code'}), so payments are "
            "not being checked or recorded. On the server run manage.py check_stripe; if the "
            "key was changed or revoked, run set-stripe-key.sh again."
        )
    if f["mismatched"]:
        out.append(
            f"{_plural(f['mismatched'], 'online payment')} in the last day did not match "
            "Stripe's record and were not recorded (the board has them on My items). If "
            "several appear at once, check that the Stripe key belongs to the association's "
            "account."
        )
    if f["autopay_technical"]:
        out.append(
            f"{_plural(f['autopay_technical'], 'automatic payment')} in the last day could not "
            "be made for a technical reason (no saved payment method or a Stripe error), not "
            "a bank's decline. The payers were emailed; tell the portal's maintainer if it "
            "repeats."
        )
    declined = f["one_off_declined"] + f["autopay_declined"]
    if declined >= DECLINE_BURST:
        out.append(
            f"{_plural(declined, 'payment')} were declined in the last day, more than usual. "
            "Each payer was told; check the Stripe dashboard for a pattern (one bank, one "
            "card type, the payment page)."
        )
    return out


def health():
    """For core.health: hidden until Stripe is set up."""
    from payments.providers import config

    if not config().available:
        return None
    f = figures()
    found = problems(f)
    if found:
        more = len(found) - 1
        return False, found[0] + (f" (and {more} more: see the alert email)" if more else "")
    declined = f["one_off_declined"] + f["autopay_declined"]
    if declined:
        return True, (
            f"{_plural(declined, 'payment')} declined by the payer's bank in the last day "
            "(each payer was told); nothing points at the portal"
        )
    return True, "No failed payments in the last day"


def alert_lines() -> list[str]:
    f = figures()
    lines = [f"  • {p}" for p in problems(f)]
    lines.append(
        "      Counts, last day: "
        f"{f['one_off_declined']} one-off declined, {f['autopay_declined']} automatic "
        f"declined, {f['autopay_technical']} automatic technical, {f['mismatched']} not "
        f"matched; Stripe trouble {f['stripe_errors']} in {STRIPE_HOURS} h"
    )
    return lines
