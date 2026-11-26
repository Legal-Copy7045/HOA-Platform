"""Starting a Stripe hosted Checkout and recording what Stripe confirms
(decision D32-payments).

Pull, never push: there is no webhook (no anonymous endpoint, D24). The
owner's return to the success page is only a hint to check now; the truth is
always a server-side GET to api.stripe.com with our key. The only thing read
from the browser is the opaque session id, which must be the one stored on a
PendingCheckout this signed-in person started.

A payment is recorded only when every check below holds on the objects
fetched from Stripe; any mismatch records nothing, is audited
("payments.mismatch", ids and codes only) and becomes a board My item.
"""

from __future__ import annotations

import datetime
import logging
import secrets
from dataclasses import dataclass

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone

from audit.services import record_event
from core import policy
from core.money import format_cents
from core.policy import SYSTEM
from core.scoping import internal, system_access
from ledger.accounts import CURRENT
from payments import services as fees
from payments import stripe_api
from payments.models import (
    AlertKind,
    CheckoutState,
    Method,
    PaymentAlert,
    PendingCheckout,
    StripePayment,
)
from payments.providers import config

logger = logging.getLogger("payments")

CHECKOUT_HOURS = 24
MIN_CENTS = 100  # $1.00
MAX_STARTS_PER_HOUR = 10


class PaymentError(ValueError):
    pass


# --- Who may pay ---------------------------------------------------------------------------


def payable_lot_ids(user) -> frozenset[int]:
    """Lots this login may pay for: owners on the deed and authorized
    contacts. Not view-only contacts, tenants, former owners or managers, and
    never in the homeowner view (a board member looking as an owner)."""
    if not policy.can(user, "payments.pay"):
        return frozenset()
    roles = policy.roles_for(user)
    if roles.user_id is None or roles.user_id != getattr(user, "pk", None):
        return frozenset()
    return roles.owned_lot_ids | roles.authorized_lot_ids


def require_can_pay(user, lot) -> None:
    if lot.pk not in payable_lot_ids(user):
        raise PermissionDenied("payments.pay")


# --- Choosing what to pay ------------------------------------------------------------------


@dataclass
class Quote:
    base_cents: int
    charge_ids: list[int]
    options: list[dict]


def _own_open_charges(user, lot):
    from ledger import allocation

    since = policy.roles_for(user).lot_since.get(lot.pk)
    return allocation.open_charges(lot.pk, since=since, account=CURRENT)


def payable_cents(lot_id: int) -> int:
    """What today's owners owe on their own account (a former owner's
    balance on the lot is never theirs to pay, D33-acctfix-1)."""
    from ledger.services import balance

    return max(balance(lot_id, account=CURRENT), 0)


def quote(user, lot, *, charge_ids=None, amount_cents=None) -> Quote:
    """What will be paid: the chosen open charges, or an amount up to the
    balance (applied by the rule)."""
    require_can_pay(user, lot)
    due = payable_cents(lot.pk)
    if due <= 0:
        raise PaymentError("Nothing is due on this account.")
    if amount_cents:
        if amount_cents < MIN_CENTS:
            raise PaymentError(f"The smallest online payment is {format_cents(MIN_CENTS)}.")
        if amount_cents > due:
            raise PaymentError(f"You can pay up to the balance due, {format_cents(due)}.")
        return Quote(amount_cents, [], fees.payment_options(amount_cents))
    open_ = {c.entry.pk: c for c in _own_open_charges(user, lot)}
    chosen = []
    for value in charge_ids or []:
        try:
            cid = int(value)
        except (TypeError, ValueError) as exc:
            raise PaymentError("Choose charges from your open charges.") from exc
        if cid not in open_:
            raise PaymentError("Choose charges from your open charges.")
        if cid not in chosen:
            chosen.append(cid)
    if not chosen:
        raise PaymentError("Choose at least one charge, or type an amount.")
    base = min(sum(open_[c].open_cents for c in chosen), due)
    if base < MIN_CENTS:
        raise PaymentError(f"The smallest online payment is {format_cents(MIN_CENTS)}.")
    return Quote(base, chosen, fees.payment_options(base))


# --- Starting a checkout ---------------------------------------------------------------------


def _person_and_account(user, lot):
    from community.models import Account, Person

    with system_access("checkout owner"):
        person = Person.objects.filter(user=user).first()
        account = Account.objects.filter(lot=lot, closed_on__isnull=True).first()
    if person is None:
        raise PermissionDenied("payments.pay")
    return person, account


def _metadata(checkout: PendingCheckout) -> dict:
    return {
        "pending_checkout": str(checkout.pk),
        "nonce": checkout.nonce,
        "lot_id": str(checkout.lot_id),
        "account_id": str(checkout.account_id or ""),
        "person_id": str(checkout.person_id),
    }


def session_params(checkout: PendingCheckout, lot_number: str) -> dict:
    """The Checkout Session we ask Stripe for (hosted page; no card or bank
    numbers ever reach the portal)."""
    base = settings.PORTAL_BASE_URL
    line_items = [
        {
            "quantity": 1,
            "price_data": {
                "currency": "usd",
                "unit_amount": checkout.base_cents,
                "product_data": {"name": f"Payment on account, lot {lot_number}"},
            },
        }
    ]
    if checkout.surcharge_cents:
        line_items.append(
            {
                "quantity": 1,
                "price_data": {
                    "currency": "usd",
                    "unit_amount": checkout.surcharge_cents,
                    "product_data": {
                        "name": "Credit card surcharge "
                        f"({fees.percent_text(checkout.surcharge_bp)})"
                    },
                },
            }
        )
    params: dict = {
        "mode": "payment",
        "submit_type": "pay",
        "client_reference_id": checkout.client_reference,
        "metadata": _metadata(checkout),
        "payment_intent_data": {
            "metadata": _metadata(checkout),
            "description": f"HOA payment, lot {lot_number}",
        },
        "line_items": line_items,
        "success_url": base
        + reverse("pay_return", args=[checkout.pk])
        + "?session_id={CHECKOUT_SESSION_ID}",
        "cancel_url": base + reverse("pay_return", args=[checkout.pk]) + "?cancelled=1",
        "expires_at": int(checkout.expires_at.timestamp()),
    }
    if checkout.method == Method.BANK:
        params["payment_method_types"] = ["us_bank_account"]
        params["payment_method_options"] = {
            "us_bank_account": {
                # Instant verification through Financial Connections where the
                # bank supports it, micro-deposits otherwise.
                "verification_method": "automatic",
                "financial_connections": {"permissions": ["payment_method"]},
            }
        }
    else:
        blocked = ["credit"] if checkout.method == Method.DEBIT else ["debit", "prepaid"]
        params["payment_method_types"] = ["card"]
        params["payment_method_options"] = {
            "card": {"restrictions": {"funding_types_blocked": blocked}}
        }
        # Link (Stripe's saved-payment wallet) can pay by bank or any card;
        # keep each session to the method the owner chose.
        params["wallet_options"] = {"link": {"display": "never"}}
    return params


def start(user, lot, *, method: str, charge_ids=None, amount_cents=None, request=None) -> str:
    """Create the PendingCheckout, then the Stripe session; returns the
    Stripe-hosted page to send the owner to."""
    from community.services import _refuse_read_only

    _refuse_read_only(user)
    require_can_pay(user, lot)
    cfg = config()
    if not cfg.available:
        raise PaymentError("Online payments are not available right now.")
    if method not in Method.values:
        raise PaymentError("Choose how to pay.")
    q = quote(user, lot, charge_ids=charge_ids, amount_cents=amount_cents)
    recent = PendingCheckout.objects.unscoped("rate limit").filter(
        user=user, created_at__gte=timezone.now() - datetime.timedelta(hours=1)
    )
    if recent.count() >= MAX_STARTS_PER_HOUR:
        raise PaymentError("Too many payment attempts. Please try again in an hour.")
    person, account = _person_and_account(user, lot)
    surcharge = fees.surcharge(q.base_cents, method)
    now = timezone.now()
    checkout = PendingCheckout.objects.create(
        nonce=secrets.token_hex(32),
        lot=lot,
        account=account,
        person=person,
        user=user,
        method=method,
        charge_ids=q.charge_ids,
        base_cents=q.base_cents,
        surcharge_cents=surcharge,
        surcharge_bp=fees.surcharge_bp() if method == Method.CREDIT else 0,
        total_cents=q.base_cents + surcharge,
        livemode=cfg.livemode,
        expires_at=now + datetime.timedelta(hours=CHECKOUT_HOURS) - datetime.timedelta(minutes=5),
    )
    try:
        session = stripe_api.create_checkout_session(
            session_params(checkout, lot.number),
            idempotency_key=f"checkout-{checkout.pk}-{checkout.nonce[:24]}",
        )
    except stripe_api.StripeError as exc:
        _set_state(checkout, CheckoutState.FAILED)
        raise PaymentError("Stripe could not start the payment. Please try again later.") from exc
    url = str(session.get("url") or "")
    session_id = str(session.get("id") or "")
    if (
        not url.startswith("https://checkout.stripe.com/")
        or not session_id.startswith("cs_")
        or bool(session.get("livemode")) != cfg.livemode
    ):
        _set_state(checkout, CheckoutState.FAILED)
        raise PaymentError("Stripe could not start the payment. Please try again later.")
    PendingCheckout.objects.unscoped("checkout session").filter(pk=checkout.pk).update(
        session_id=session_id
    )
    record_event(
        "payments.checkout_started",
        actor=user,
        request=request,
        target=checkout,
        metadata={
            "lot_id": lot.pk,
            "checkout_id": checkout.pk,
            "method": method,
            "amount_cents": checkout.total_cents,
        },
    )
    return url


def _set_state(checkout: PendingCheckout, state: str, **extra) -> None:
    PendingCheckout.objects.unscoped("checkout state").filter(pk=checkout.pk).update(
        state=state, state_changed_at=timezone.now(), **extra
    )
    checkout.state = state


# --- Checking with Stripe -------------------------------------------------------------------


class Mismatch(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _obj(value) -> dict:
    return value if isinstance(value, dict) else {}


def _verify_session(checkout: PendingCheckout, session: dict) -> None:
    cfg = config()
    if session.get("id") != checkout.session_id:
        raise Mismatch("session_id")
    if session.get("client_reference_id") != checkout.client_reference:
        raise Mismatch("client_reference")
    if _obj(session.get("metadata")) != _metadata(checkout):
        raise Mismatch("metadata")
    if bool(session.get("livemode")) != checkout.livemode or checkout.livemode != cfg.livemode:
        raise Mismatch("livemode")
    if session.get("currency") != "usd":
        raise Mismatch("currency")
    if session.get("amount_total") != checkout.total_cents:
        raise Mismatch("amount")


def _verify_paid(checkout: PendingCheckout, pi: dict) -> dict:
    """The PaymentIntent and its charge, as succeeded and captured money for
    exactly what this checkout expected. Returns the charge."""
    if not pi.get("id") or (
        checkout.payment_intent_id and pi.get("id") != checkout.payment_intent_id
    ):
        raise Mismatch("payment_intent_id")
    if bool(pi.get("livemode")) != checkout.livemode:
        raise Mismatch("livemode")
    meta = _obj(pi.get("metadata"))
    if meta.get("pending_checkout") != str(checkout.pk) or meta.get("nonce") != checkout.nonce:
        raise Mismatch("metadata")
    if pi.get("currency") != "usd":
        raise Mismatch("currency")
    if pi.get("amount") != checkout.total_cents or pi.get("amount_received") != (
        checkout.total_cents
    ):
        raise Mismatch("amount")
    if pi.get("status") != "succeeded":
        raise Mismatch("not_succeeded")
    charge = _obj(pi.get("latest_charge"))
    if not charge:
        charge_id = pi.get("latest_charge")
        if not isinstance(charge_id, str):
            raise Mismatch("no_charge")
        charge = stripe_api.retrieve_charge(charge_id)
    if (
        charge.get("captured") is not True
        or charge.get("paid") is not True
        or charge.get("status") != "succeeded"
        or bool(charge.get("livemode")) != checkout.livemode
        or charge.get("amount") != checkout.total_cents
        or charge.get("currency") != "usd"
        or not str(charge.get("id") or "").startswith(("ch_", "py_"))
    ):
        raise Mismatch("charge")
    return charge


def _mismatch(checkout: PendingCheckout, code: str) -> None:
    _set_state(checkout, CheckoutState.MISMATCH)
    record_event(
        "payments.mismatch",
        actor=None,
        target=checkout,
        metadata={"checkout_id": checkout.pk, "code": code},
    )
    _alert(
        AlertKind.MISMATCH,
        f"checkout-{checkout.pk}",
        lot_id=checkout.lot_id,
        amount=checkout.total_cents,
        ref=checkout.session_id or "",
        detail=f"Online payment #{checkout.pk} was not recorded: Stripe's record did not "
        f"match ({code}). Check it in the Stripe dashboard.",
    )


def _alert(kind, key, *, lot_id=None, amount=0, ref="", detail="") -> PaymentAlert | None:
    try:
        with transaction.atomic():
            return PaymentAlert.objects.create(
                kind=kind,
                key=key[:255],
                lot_id=lot_id,
                amount_cents=amount,
                stripe_ref=ref[:255],
                detail=detail[:300],
            )
    except IntegrityError:
        return None


@internal("payments check")
def check(checkout: PendingCheckout) -> str:
    """Ask Stripe about this checkout now and record the payment if it is
    paid and everything matches. Idempotent. Returns the checkout's state."""
    if checkout.state in (CheckoutState.PAID, CheckoutState.MISMATCH, CheckoutState.FAILED):
        return checkout.state
    if not checkout.session_id:
        return checkout.state
    PendingCheckout.objects.filter(pk=checkout.pk).update(last_checked_at=timezone.now())
    try:
        if checkout.state == CheckoutState.PROCESSING and checkout.payment_intent_id:
            pi = stripe_api.retrieve_payment_intent(checkout.payment_intent_id)
            return _from_payment_intent(checkout, pi)
        session = stripe_api.retrieve_checkout_session(checkout.session_id)
    except stripe_api.StripeError:
        return checkout.state  # try again later
    try:
        _verify_session(checkout, session)
    except Mismatch as exc:
        _mismatch(checkout, exc.code)
        return checkout.state
    pi = _obj(session.get("payment_intent"))
    if session.get("status") == "expired":
        _set_state(checkout, CheckoutState.EXPIRED)
        return checkout.state
    if session.get("status") != "complete":
        if timezone.now() > checkout.expires_at:
            _set_state(checkout, CheckoutState.EXPIRED)
        return checkout.state
    if not pi:
        _mismatch(checkout, "no_payment_intent")
        return checkout.state
    if not checkout.payment_intent_id:
        try:
            with transaction.atomic():
                PendingCheckout.objects.filter(pk=checkout.pk).update(
                    payment_intent_id=pi.get("id")
                )
        except IntegrityError:
            _mismatch(checkout, "payment_intent_reused")
            return checkout.state
        checkout.payment_intent_id = pi.get("id")
    if session.get("payment_status") == "paid":
        return _from_payment_intent(checkout, pi)
    if pi.get("status") == "processing":
        _set_state(checkout, CheckoutState.PROCESSING)
        return checkout.state
    return _from_payment_intent(checkout, pi)


def _from_payment_intent(checkout: PendingCheckout, pi: dict) -> str:
    status = pi.get("status")
    if status == "processing":
        if checkout.state != CheckoutState.PROCESSING:
            _set_state(checkout, CheckoutState.PROCESSING)
        return checkout.state
    if status in ("requires_payment_method", "canceled"):
        _set_state(checkout, CheckoutState.FAILED)
        record_event(
            "payments.failed",
            actor=None,
            target=checkout,
            metadata={"checkout_id": checkout.pk, "code": str(status)},
        )
        return checkout.state
    try:
        charge = _verify_paid(checkout, pi)
    except Mismatch as exc:
        _mismatch(checkout, exc.code)
        return checkout.state
    except stripe_api.StripeError:
        return checkout.state
    _record(checkout, pi, charge)
    return CheckoutState.PAID


def _funding(charge: dict) -> str:
    details = _obj(charge.get("payment_method_details"))
    if details.get("type") == "us_bank_account":
        return "bank"
    funding = _obj(details.get("card")).get("funding")
    return str(funding) if funding in ("credit", "debit", "prepaid") else "unknown"


@internal("payments record")
def _record(checkout: PendingCheckout, pi: dict, charge: dict) -> StripePayment | None:
    from community.models import Lot
    from ledger import services as ledger

    bt = charge.get("balance_transaction")
    bt_id = bt.get("id") if isinstance(bt, dict) else (bt if isinstance(bt, str) else "")
    fee = bt.get("fee") if isinstance(bt, dict) else None
    funding = _funding(charge)
    with transaction.atomic():
        locked = PendingCheckout.objects.select_for_update().get(pk=checkout.pk)
        existing = StripePayment.objects.filter(payment_intent_id=pi["id"]).first()
        if existing is not None or locked.state == CheckoutState.PAID:
            return existing
        if locked.state in (CheckoutState.MISMATCH, CheckoutState.FAILED):
            return None
        lot = Lot.objects.get(pk=locked.lot_id)
        entry = ledger.record_online_payment(
            lot=lot,
            amount_cents=locked.base_cents,
            received_on=timezone.localdate(),
            payment_intent_id=pi["id"],
            apply_to=locked.charge_ids,
            actor=SYSTEM,
        )
        payment = StripePayment.objects.create(
            checkout=locked,
            lot=lot,
            entry=entry,
            payment_intent_id=pi["id"],
            charge_id=charge["id"],
            balance_transaction_id=str(bt_id or "")[:255],
            method=locked.method,
            funding=funding,
            base_cents=locked.base_cents,
            surcharge_cents=locked.surcharge_cents,
            fee_cents=fee if isinstance(fee, int) else None,
            livemode=locked.livemode,
        )
        PendingCheckout.objects.filter(pk=locked.pk).update(
            state=CheckoutState.PAID, state_changed_at=timezone.now()
        )
        checkout.state = CheckoutState.PAID
        record_event(
            "payments.recorded",
            actor=None,
            target=locked,
            metadata={
                "lot_id": lot.pk,
                "checkout_id": locked.pk,
                "amount_cents": locked.base_cents,
                "surcharge_cents": locked.surcharge_cents,
                "method": locked.method,
            },
        )
        _safety_net(payment, lot)
        from payments.receipts import email_receipt

        transaction.on_commit(lambda: email_receipt(payment))
    return payment


def _safety_net(payment: StripePayment, lot) -> None:
    """A surcharge may only be paid with a credit card. The portal cannot
    refund (its Stripe key has no refund rights): it asks the board to refund
    the surcharge, and the poller closes the item when the refund appears."""
    if payment.method == Method.CREDIT and payment.surcharge_cents and payment.funding != "credit":
        _alert(
            AlertKind.SURCHARGE_REFUND,
            payment.payment_intent_id,
            lot_id=lot.pk,
            amount=payment.surcharge_cents,
            ref=payment.payment_intent_id,
            detail=f"Refund {format_cents(payment.surcharge_cents)} surcharge to lot {lot.number} "
            f"({payment.funding} card) in the Stripe dashboard",
        )
        record_event(
            "payments.surcharge_refund_due",
            actor=None,
            target=payment.checkout,
            metadata={"checkout_id": payment.checkout_id, "amount_cents": payment.surcharge_cents},
        )
    elif payment.method in (Method.BANK, Method.DEBIT) and payment.funding == "credit":
        # The HOA absorbs the cost; counted, nothing about the card is logged.
        logger.info("payments.credit_card_on_no_fee_session", extra={"count": 1})
