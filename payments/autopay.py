"""Automatic payment of the monthly assessment through Stripe (decision
D46; builds on D32-payments).

Enrolment. A payer (an owner on the deed or an authorized contact, after a
fresh password or passkey check) chooses a bank account (no fee) or a card
(credit cards carry the configured surcharge, debit cards none). The portal
starts a Stripe Checkout Session in *setup* mode for the account's Stripe
Customer: Stripe's hosted page collects the card or bank details and, for a
bank account, the ACH debit authorization (mandate). The portal never sees a
card or account number. As with payments, the return to the portal is only a
hint: the server reads the session and its SetupIntent from Stripe (pull,
never push: no webhook) and accepts them only when they match the
``AutopaySetup`` row this person started (ids, nonce, customer, mode,
payment method type). Only Stripe ids and the display details Stripe returns
(brand or bank name and the last four digits) are kept, on append-only
``AutopayEvent`` rows; the current state (on, paused, off) is derived from
them.

Charging. On the due day (rule ``dues.due_day``, site time; an hourly task,
so a server that was down catches up within a week) each account with
automatic payments on is charged the open amount of *that month's assessment
charge only*: not late fees, other charges or older months, which the owner
chose nothing about (they pay those by hand). Credit already on the account
is used first; when it covers the assessment, nothing is charged. A card
charge adds the credit card surcharge exactly as Pay online does (credit
cards only, ``payments.services.surcharge``). Each charge is a
``PendingCheckout`` (method bank, credit or debit) plus an append-only
``AutopayAttempt``, unique per account, month and try, sent with an
idempotency key of the same three, so re-runs never charge twice. The
payment is recorded only when Stripe says the PaymentIntent succeeded, by the
same verification and recording code as Pay online (``checkout.
_from_payment_intent``); a bank payment stays "processing" and is asked
about until it settles.

Once a year (D47). A payer may choose the annual schedule instead: on the
due day of the month they choose, the next twelve monthly assessments are
charged in one payment, less any credit already on the account. Nothing is
charged monthly then. The payment is recorded like any other (applied to
that month's assessment when it is already posted); the rest is credit that
open-item allocation applies to each month's assessment as it posts, exactly
as a prepayment (D30). One annual charge per account and year (period
"YYYY", idempotency key ``autopay-<account>-annual-<year>-<try>``).

Review (D47, replacing D46's percent ceiling). Any automatic charge whose
total (surcharge included) is above rule ``payments.autopay_review_cents``
is not charged: an ``AutopayReview`` waits for a board member who does not
pay for that home (``payments.autopay_review``). Approved, it is charged on
the next run; declined or not decided within 14 days, nothing is charged
and the payers are told.

Failures. A decline, a payment needing the cardholder's authentication or a
failed bank debit is recorded (append-only), emailed to the payers and shown
as a to-do. A soft decline (such as insufficient funds) is retried once,
three days later; a hard decline, a returned bank payment or a second
failure pauses automatic payments for the account until a payer sets up a
payment method again.
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
from core.money import format_cents
from core.scoping import internal, system_access
from payments import checkout as flow
from payments import services as fees
from payments import stripe_api
from payments.models import (
    AlertKind,
    AutopayAttempt,
    AutopayEvent,
    AutopayEventKind,
    AutopaySchedule,
    AutopaySetup,
    AutopayType,
    CheckoutState,
    Method,
    PendingCheckout,
    SetupState,
    StripeCustomer,
)
from payments.providers import config

logger = logging.getLogger("payments")

SETUP_HOURS = 24
MAX_SETUPS_PER_HOUR = 5
RETRY_DAYS = 3
CHARGE_WINDOW_DAYS = 7  # a run missed on the due day still charges within a week
RECHECK_HOURS = 6  # a bank payment still processing is asked about this often
AMBIGUOUS_MINUTES = 10  # Stripe did not answer: ask again after this long
REPOST_HOURS = 20  # Stripe keeps idempotency keys at least 24 hours
REVIEW_RULE = "payments.autopay_review_cents"
MONTHLY, ANNUAL = AutopaySchedule.MONTHLY.value, AutopaySchedule.ANNUAL.value
ANNUAL_MONTHS = 12

# Declines worth one more try a few days later. Anything else (expired, lost
# or stolen card, closed or unauthorized bank account, a payment needing the
# cardholder's authentication) waits for the payer.
SOFT_DECLINES = frozenset(
    {
        "insufficient_funds",
        "generic_decline",
        "card_declined",
        "try_again_later",
        "processing_error",
        "issuer_not_available",
        "card_velocity_exceeded",
        "withdrawal_count_limit_exceeded",
        "approve_with_id",
        "reenter_transaction",
        "do_not_honor",
        "call_issuer",
    }
)

ON, PAUSED, OFF = "on", "paused", "off"


def _today() -> datetime.date:
    """Today, site time (one place, so tests can move the calendar)."""
    return timezone.localdate()


class AutopayError(ValueError):
    pass


def _refuse_if_locked(user) -> None:
    """D58: no automatic-payment changes for a while after a recovery."""
    from accounts.recovery import refuse_if_locked

    refuse_if_locked(user, "automatic payments", AutopayError)


def autopay_enabled(request=None) -> bool:
    """The board switched on both features and Stripe is set up here."""
    from core import features

    return features.enabled("autopay", request) and fees.online_payments_enabled(request)


# --- State ---------------------------------------------------------------------------------


@dataclass
class State:
    status: str  # on, paused, off
    method: AutopayEvent | None = None  # the enrolled/replaced row in use
    since: datetime.datetime | None = None  # when this enrolment began
    last_failure: AutopayEvent | None = None  # a failure since the method was set
    schedule: str = MONTHLY  # D47: monthly or annual
    annual_month: int | None = None  # annual: the month of the charge (1-12)
    schedule_since: datetime.datetime | None = None  # when this schedule took effect

    @property
    def label(self) -> str:
        return method_label(self.method) if self.method else ""

    @property
    def schedule_text(self) -> str:
        return schedule_text(self.schedule, self.annual_month)


def month_name(month: int | None) -> str:
    return datetime.date(2000, month, 1).strftime("%B") if month else ""


def schedule_text(schedule: str, annual_month: int | None) -> str:
    """ "monthly" or "once a year in January"."""
    if schedule == ANNUAL:
        return f"once a year in {month_name(annual_month)}"
    return "monthly"


def _apply_schedule(current: State, event: AutopayEvent) -> None:
    """A row that sets the schedule (rows from before D47 carry none: monthly)."""
    if not event.schedule:
        return
    month = event.annual_month if event.schedule == ANNUAL else None
    if (event.schedule, month) != (current.schedule, current.annual_month):
        current.schedule, current.annual_month = event.schedule, month
        current.schedule_since = event.created_at


def method_label(event: AutopayEvent) -> str:
    """ "Visa ending 4242", "debit card ending 4242", "STRIPE TEST BANK
    account ending 6789"."""
    tail = f" ending {event.last4}" if event.last4 else ""
    if event.method_type == AutopayType.BANK:
        name = f"{event.bank_name} account" if event.bank_name else "bank account"
        return name + tail
    brand = (event.brand or "").replace("_", " ").title() or "Card"
    if brand.lower() in ("amex", "american express"):
        brand = "American Express"
    if event.funding in ("debit", "prepaid"):
        return f"{brand} {event.funding} card{tail}"
    return brand + tail


@internal("autopay state")
def state(account_id) -> State:
    """The account's automatic payments, from its events in order."""
    if account_id is None:
        return State(OFF)
    current = State(OFF)
    for event in AutopayEvent.objects.filter(account_id=account_id).order_by("created_at", "id"):
        if event.kind == AutopayEventKind.ENROLLED:
            current = State(ON, event, event.created_at, schedule_since=event.created_at)
            _apply_schedule(current, event)
            current.schedule_since = event.created_at
        elif event.kind == AutopayEventKind.REPLACED:
            previous = current
            current = State(
                ON,
                event,
                previous.since or event.created_at,
                schedule=previous.schedule if previous.status != OFF else MONTHLY,
                annual_month=previous.annual_month if previous.status != OFF else None,
                schedule_since=previous.schedule_since or event.created_at,
            )
            _apply_schedule(current, event)
        elif event.kind == AutopayEventKind.SCHEDULE and current.status != OFF:
            _apply_schedule(current, event)
        elif event.kind == AutopayEventKind.CANCELLED:
            current = State(OFF)
        elif event.kind == AutopayEventKind.PAUSED and current.status != OFF:
            current.status = PAUSED
        elif event.kind == AutopayEventKind.FAILED and current.status != OFF:
            current.last_failure = event
    return current


def _current_account(lot_id):
    from community.models import Account

    with system_access("autopay account"):
        return Account.objects.filter(lot_id=lot_id, closed_on__isnull=True).first()


def _surcharge_method(event: AutopayEvent) -> str:
    if event.method_type == AutopayType.BANK:
        return Method.BANK
    return Method.CREDIT if event.funding == "credit" else Method.DEBIT


# --- Enrolment -----------------------------------------------------------------------------


def _metadata(setup: AutopaySetup) -> dict:
    values = {
        "autopay_setup": str(setup.pk),
        "nonce": setup.nonce,
        "lot_id": str(setup.lot_id),
        "account_id": str(setup.account_id),
        "person_id": str(setup.person_id),
    }
    return {k: v for k, v in values.items() if v}


@internal("autopay customer")
def _customer(account, livemode: bool) -> str:
    """The account's Stripe Customer, created on first use. Only internal ids
    go to Stripe: Stripe's hosted page asks the payer for the email address
    it needs (for the bank debit authorization and its receipts)."""
    existing = StripeCustomer.objects.filter(account=account, livemode=livemode).first()
    if existing is not None:
        return existing.customer_id
    customer = stripe_api.create_customer(
        {"metadata": {"account_id": str(account.pk), "lot_id": str(account.lot_id)}},
        idempotency_key=f"customer-{account.pk}-{'live' if livemode else 'test'}",
    )
    customer_id = str(customer.get("id") or "")
    if not customer_id.startswith("cus_") or bool(customer.get("livemode")) != livemode:
        raise AutopayError("Stripe could not set up automatic payments. Please try again later.")
    try:
        with transaction.atomic():
            StripeCustomer.objects.create(
                account=account, customer_id=customer_id[:255], livemode=livemode
            )
    except IntegrityError:
        return StripeCustomer.objects.get(account=account, livemode=livemode).customer_id
    return customer_id


def _monthly_cents() -> int:
    from community.rules import rule

    return rule("dues.monthly_cents") or 0


def _day_text(day: int) -> str:
    suffix = "th" if 11 <= day % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    return f"{day}{suffix}"


def authorization_text(schedule: str, annual_month: int | None, method_type: str = "") -> str:
    """What the payer authorizes, in one or two sentences, for the set-up
    page, the schedule form and Stripe's page (D47): which schedule and
    roughly what amount."""
    from community.terms import term_text

    monthly = _monthly_cents()
    if schedule == ANNUAL:
        text = term_text(
            f"About {format_cents(monthly * ANNUAL_MONTHS)} once a year in "
            f"{month_name(annual_month)} (on the {_day_text(_due_day())}), for the next "
            f"{ANNUAL_MONTHS} months' {{terms}}, less any credit already on the account."
        )
    else:
        text = term_text(
            f"About {format_cents(monthly)} each month on the {_day_text(_due_day())}, for "
            "that month's {term}, less any credit already on the account."
        )
    if method_type != AutopayType.BANK and fees.surcharge_bp():
        text += (
            f" Credit cards add the {fees.percent_text(fees.surcharge_bp())} surcharge to "
            "the whole amount."
        )
    return text


def session_params(setup: AutopaySetup, lot_number: str) -> dict:
    """The setup-mode Checkout Session: Stripe's hosted page collects the
    payment method (and, for a bank account, the debit authorization). The
    schedule and the rough amount are named in the set-up's description and
    in the message Stripe shows by the button (D47)."""
    from community.terms import term_text

    base = settings.PORTAL_BASE_URL
    what = authorization_text(setup.schedule, setup.annual_month, setup.method_type)
    plan = (
        f"once a year in {month_name(setup.annual_month)}"
        if setup.schedule == ANNUAL
        else "monthly"
    )
    back = base + reverse("autopay_return", args=[setup.pk])
    params: dict = {
        "mode": "setup",
        "currency": "usd",
        "customer": setup.customer_id,
        "client_reference_id": setup.client_reference,
        "metadata": _metadata(setup),
        "setup_intent_data": {
            "metadata": _metadata(setup),
            "description": term_text(
                f"Automatic HOA {{term}} payments ({plan}), {lot_number}: {what}"
            )[:500],
        },
        "custom_text": {
            "submit": {
                "message": (
                    f"Automatic payments for {lot_number}, {plan}. {what} You can turn them "
                    "off any time on My account."
                )[:1200]
            }
        },
        "payment_method_types": [setup.method_type],
        "success_url": back + "?session_id={CHECKOUT_SESSION_ID}",
        "cancel_url": back + "?cancelled=1",
        "expires_at": int(setup.expires_at.timestamp()),
    }
    if setup.method_type == AutopayType.BANK:
        params["payment_method_options"] = {
            "us_bank_account": {
                "verification_method": "automatic",
                "financial_connections": {"permissions": ["payment_method"]},
            }
        }
    else:
        params["wallet_options"] = {"link": {"display": "never"}}
    return params


def clean_schedule(schedule: str, annual_month) -> tuple[str, int | None]:
    """The schedule and month from a form, or AutopayError."""
    schedule = schedule or MONTHLY
    if schedule not in AutopaySchedule.values:
        raise AutopayError("Choose monthly or once a year.")
    if schedule == MONTHLY:
        return MONTHLY, None
    try:
        month = int(annual_month)
    except (TypeError, ValueError):
        month = 0
    if not 1 <= month <= 12:
        raise AutopayError("Choose the month of the yearly payment.")
    return ANNUAL, month


def start_setup(
    user,
    lot,
    *,
    method_type: str,
    schedule: str = MONTHLY,
    annual_month=None,
    request=None,
) -> str:
    """Create the AutopaySetup and the Stripe session; returns the hosted
    page to send the payer to."""
    from community.services import _refuse_read_only

    _refuse_read_only(user)
    flow.require_can_pay(user, lot)
    _refuse_if_locked(user)
    if not autopay_enabled():
        raise AutopayError("Automatic payments are not available right now.")
    if method_type not in AutopayType.values:
        raise AutopayError("Choose a bank account or a card.")
    schedule, annual_month = clean_schedule(schedule, annual_month)
    recent = AutopaySetup.objects.unscoped("rate limit").filter(
        user=user, created_at__gte=timezone.now() - datetime.timedelta(hours=1)
    )
    if recent.count() >= MAX_SETUPS_PER_HOUR:
        raise AutopayError("Too many attempts. Please try again in an hour.")
    person, account = flow._person_and_account(user, lot)
    if account is None:
        raise AutopayError("This home has no account yet. Ask the board.")
    cfg = config()
    try:
        customer_id = _customer(account, cfg.livemode)
    except stripe_api.StripeError as exc:
        raise AutopayError(
            "Stripe could not set up automatic payments. Please try again later."
        ) from exc
    setup = AutopaySetup.objects.create(
        nonce=secrets.token_hex(32),
        lot=lot,
        account=account,
        person=person,
        user=user,
        method_type=method_type,
        schedule=schedule,
        annual_month=annual_month,
        customer_id=customer_id,
        livemode=cfg.livemode,
        expires_at=timezone.now()
        + datetime.timedelta(hours=SETUP_HOURS)
        - datetime.timedelta(minutes=5),
    )
    try:
        session = stripe_api.create_checkout_session(
            session_params(setup, lot.label),
            idempotency_key=f"autopay-setup-{setup.pk}-{setup.nonce[:24]}",
        )
    except stripe_api.StripeError as exc:
        _set_setup_state(setup, SetupState.FAILED)
        raise AutopayError(
            "Stripe could not set up automatic payments. Please try again later."
        ) from exc
    url = str(session.get("url") or "")
    session_id = str(session.get("id") or "")
    if (
        not url.startswith("https://checkout.stripe.com/")
        or not session_id.startswith("cs_")
        or bool(session.get("livemode")) != cfg.livemode
    ):
        _set_setup_state(setup, SetupState.FAILED)
        raise AutopayError("Stripe could not set up automatic payments. Please try again later.")
    AutopaySetup.objects.unscoped("setup session").filter(pk=setup.pk).update(session_id=session_id)
    record_event(
        "payments.autopay_setup_started",
        actor=user,
        request=request,
        target=setup,
        metadata={
            "lot_id": lot.pk,
            "account_id": account.pk,
            "setup_id": setup.pk,
            "method": method_type,
            "schedule": schedule,
        },
    )
    return url


def _set_setup_state(setup: AutopaySetup, state_: str, **extra) -> None:
    AutopaySetup.objects.unscoped("setup state").filter(pk=setup.pk).update(
        state=state_, state_changed_at=timezone.now(), **extra
    )
    setup.state = state_


def _id_of(value) -> str:
    if isinstance(value, dict):
        value = value.get("id")
    return value if isinstance(value, str) else ""


def _setup_mismatch(setup: AutopaySetup, code: str) -> None:
    _set_setup_state(setup, SetupState.MISMATCH)
    record_event(
        "payments.autopay_setup_mismatch",
        actor=None,
        target=setup,
        metadata={"setup_id": setup.pk, "code": code},
    )


def _verify_setup_session(setup: AutopaySetup, session: dict) -> None:
    if session.get("id") != setup.session_id:
        raise flow.Mismatch("session_id")
    if session.get("client_reference_id") != setup.client_reference:
        raise flow.Mismatch("client_reference")
    returned = {
        k: v
        for k, v in (session.get("metadata") or {}).items()
        if v not in ("", None) and isinstance(k, str)
    }
    if returned != _metadata(setup):
        raise flow.Mismatch("metadata")
    if session.get("mode") != "setup":
        raise flow.Mismatch("mode")
    if bool(session.get("livemode")) != setup.livemode or setup.livemode != config().livemode:
        raise flow.Mismatch("livemode")
    if _id_of(session.get("customer")) != setup.customer_id:
        raise flow.Mismatch("customer")


@internal("autopay setup check")
def check_setup(setup: AutopaySetup, now=None) -> str:
    """Ask Stripe about this set-up now; enrol when the payment method is
    saved and everything matches. Idempotent. Returns the set-up's state."""
    if setup.state not in (SetupState.OPEN, SetupState.WAITING) or not setup.session_id:
        return setup.state
    AutopaySetup.objects.filter(pk=setup.pk).update(last_checked_at=timezone.now())
    try:
        if setup.state == SetupState.WAITING and setup.setup_intent_id:
            return _from_setup_intent(
                setup, stripe_api.retrieve_setup_intent(setup.setup_intent_id)
            )
        session = stripe_api.retrieve_setup_session(setup.session_id)
    except stripe_api.StripeError:
        return setup.state  # ask again later
    try:
        _verify_setup_session(setup, session)
    except flow.Mismatch as exc:
        _setup_mismatch(setup, exc.code)
        return setup.state
    if session.get("status") == "expired":
        _set_setup_state(setup, SetupState.EXPIRED)
        return setup.state
    if session.get("status") != "complete":
        if (now or timezone.now()) > setup.expires_at:
            _set_setup_state(setup, SetupState.EXPIRED)
        return setup.state
    seti = session.get("setup_intent")
    seti_id = _id_of(seti)
    if not seti_id.startswith("seti_"):
        _setup_mismatch(setup, "no_setup_intent")
        return setup.state
    if not setup.setup_intent_id:
        try:
            with transaction.atomic():
                AutopaySetup.objects.filter(pk=setup.pk).update(setup_intent_id=seti_id)
        except IntegrityError:
            _setup_mismatch(setup, "setup_intent_reused")
            return setup.state
        setup.setup_intent_id = seti_id
    if not isinstance(seti, dict):
        try:
            seti = stripe_api.retrieve_setup_intent(seti_id)
        except stripe_api.StripeError:
            return setup.state
    return _from_setup_intent(setup, seti)


def _from_setup_intent(setup: AutopaySetup, seti: dict) -> str:
    try:
        if seti.get("id") != setup.setup_intent_id:
            raise flow.Mismatch("setup_intent_id")
        if bool(seti.get("livemode")) != setup.livemode:
            raise flow.Mismatch("livemode")
        meta = seti.get("metadata") or {}
        if meta.get("autopay_setup") != str(setup.pk) or meta.get("nonce") != setup.nonce:
            raise flow.Mismatch("metadata")
        if _id_of(seti.get("customer")) != setup.customer_id:
            raise flow.Mismatch("customer")
    except flow.Mismatch as exc:
        _setup_mismatch(setup, exc.code)
        return setup.state
    status = seti.get("status")
    if status in ("requires_action", "processing"):
        # A bank account waiting for micro-deposit verification.
        if setup.state != SetupState.WAITING:
            _set_setup_state(setup, SetupState.WAITING)
        return setup.state
    if status in ("requires_payment_method", "canceled"):
        _set_setup_state(setup, SetupState.FAILED)
        record_event(
            "payments.autopay_setup_failed",
            actor=None,
            target=setup,
            metadata={"setup_id": setup.pk, "code": str(status)},
        )
        return setup.state
    if status != "succeeded":
        return setup.state
    pm = seti.get("payment_method")
    if not isinstance(pm, dict):
        _setup_mismatch(setup, "payment_method")
        return setup.state
    try:
        details = _method_details(setup, seti, pm)
    except flow.Mismatch as exc:
        _setup_mismatch(setup, exc.code)
        return setup.state
    _enrol(setup, details)
    return setup.state


def _clean(value, length: int) -> str:
    text = value if isinstance(value, str) else ""
    return "".join(ch for ch in text if ch.isprintable())[:length]


def _method_details(setup: AutopaySetup, seti: dict, pm: dict) -> dict:
    """The saved payment method as Stripe describes it: its id, type, and for
    display the brand or bank name and the last four digits (never a number)."""
    pm_id = _id_of(pm)
    if not pm_id.startswith("pm_"):
        raise flow.Mismatch("payment_method")
    if pm.get("type") != setup.method_type:
        raise flow.Mismatch("payment_method_type")
    if _id_of(pm.get("customer")) != setup.customer_id:
        raise flow.Mismatch("payment_method_customer")
    if bool(pm.get("livemode", setup.livemode)) != setup.livemode:
        raise flow.Mismatch("livemode")
    mandate = _id_of(seti.get("mandate"))
    if setup.method_type == AutopayType.BANK and not mandate.startswith("mandate_"):
        raise flow.Mismatch("mandate")
    inner = pm.get(setup.method_type) if isinstance(pm.get(setup.method_type), dict) else {}
    last4 = inner.get("last4") if isinstance(inner.get("last4"), str) else ""
    funding = inner.get("funding") if setup.method_type == AutopayType.CARD else ""
    return {
        "payment_method_id": pm_id[:255],
        "method_type": setup.method_type,
        "funding": funding if funding in ("credit", "debit", "prepaid") else "",
        "brand": _clean(inner.get("brand"), 40) if setup.method_type == AutopayType.CARD else "",
        "bank_name": _clean(inner.get("bank_name"), 80)
        if setup.method_type == AutopayType.BANK
        else "",
        "last4": last4 if len(last4) == 4 and last4.isdigit() else "",
        "mandate_id": mandate[:255],
    }


@internal("autopay enrol")
def _enrol(setup: AutopaySetup, details: dict) -> AutopayEvent | None:
    from community.models import Account

    with transaction.atomic():
        locked = AutopaySetup.objects.select_for_update().get(pk=setup.pk)
        if locked.state not in (SetupState.OPEN, SetupState.WAITING):
            setup.state = locked.state
            return None
        account = Account.objects.get(pk=locked.account_id)
        if account.closed_on is not None:
            # Sold while the payer was on Stripe's page: nothing carries over.
            _setup_mismatch(setup, "account_closed")
            transaction.on_commit(lambda: _detach(details["payment_method_id"]))
            return None
        before = state(account.pk)
        replaced = before.status != OFF
        event = AutopayEvent.objects.create(
            lot_id=locked.lot_id,
            account=account,
            kind=AutopayEventKind.REPLACED if replaced else AutopayEventKind.ENROLLED,
            setup=locked,
            person_id=locked.person_id,
            user_id=locked.user_id,
            customer_id=locked.customer_id,
            schedule=locked.schedule or MONTHLY,
            annual_month=locked.annual_month if locked.schedule == ANNUAL else None,
            **details,
        )
        _set_setup_state(setup, SetupState.DONE)
        if before.schedule != (locked.schedule or MONTHLY) or (
            before.annual_month != (locked.annual_month if locked.schedule == ANNUAL else None)
        ):
            _close_reviews_for_schedule(account, code="schedule_changed")
        record_event(
            "payments.autopay_enrolled",
            actor=locked.user,
            target=locked,
            metadata={
                "lot_id": locked.lot_id,
                "account_id": account.pk,
                "setup_id": locked.pk,
                "method": locked.method_type,
                "replaced": replaced,
                "schedule": locked.schedule or MONTHLY,
            },
        )
        old = before.method.payment_method_id if before.method else ""
        if old and old != details["payment_method_id"]:
            transaction.on_commit(lambda: _detach(old))
    return event


# --- Cancelling ----------------------------------------------------------------------------


def _detach(pm_id: str) -> None:
    """Remove the payment method from the Stripe Customer (needs the key's
    PaymentMethods write permission). Best effort: a failure is logged by
    code and audited; nothing is charged to a cancelled enrolment anyway."""
    if not pm_id or not config().available:
        return
    try:
        stripe_api.detach_payment_method(pm_id)
    except stripe_api.StripeError as exc:
        if exc.status == 404 or exc.code in ("resource_missing", "payment_method_unexpected_state"):
            return  # already gone
        record_event(
            "payments.autopay_detach_failed",
            actor=None,
            metadata={"code": exc.code or str(exc.status)},
        )


@internal("autopay cancel")
def _cancel(account, *, code: str, user=None, request=None) -> AutopayEvent | None:
    before = state(account.pk)
    if before.status == OFF:
        return None
    person_id = None
    if user is not None:
        from community.models import Person

        person_id = Person.objects.filter(user=user).values_list("pk", flat=True).first()
    event = AutopayEvent.objects.create(
        lot_id=account.lot_id,
        account=account,
        kind=AutopayEventKind.CANCELLED,
        person_id=person_id,
        user=user,
        customer_id=before.method.customer_id if before.method else "",
        payment_method_id=before.method.payment_method_id if before.method else "",
        code=code[:60],
    )
    record_event(
        "payments.autopay_cancelled",
        actor=user,
        request=request,
        target=account,
        metadata={"lot_id": account.lot_id, "account_id": account.pk, "code": code},
    )
    if before.method:
        pm_id = before.method.payment_method_id
        transaction.on_commit(lambda: _detach(pm_id))
    return event


def cancel(user, lot, *, request=None) -> None:
    """A payer turns automatic payments off (any time; audited)."""
    from community.services import _refuse_read_only

    _refuse_read_only(user)
    flow.require_can_pay(user, lot)
    _refuse_if_locked(user)
    account = _current_account(lot.pk)
    if account is None or state(account.pk).status == OFF:
        raise AutopayError("Automatic payments are not set up for this home.")
    with transaction.atomic():
        _cancel(account, code="owner", user=user, request=request)


def change_schedule(user, lot, *, schedule: str, annual_month=None, request=None) -> AutopayEvent:
    """A payer switches between monthly and once a year (or the month of the
    yearly payment). Audited; takes effect from the next charge: a charge
    already made this month or year stands, and a review waiting for the
    board under the old schedule is closed."""
    from community.models import Person
    from community.services import _refuse_read_only

    _refuse_read_only(user)
    flow.require_can_pay(user, lot)
    if not autopay_enabled():
        raise AutopayError("Automatic payments are not available right now.")
    schedule, annual_month = clean_schedule(schedule, annual_month)
    account = _current_account(lot.pk)
    if account is None:
        raise AutopayError("Automatic payments are not set up for this home.")
    with transaction.atomic(), system_access("autopay schedule"):
        _lock_account(account)
        before = state(account.pk)
        if before.status == OFF:
            raise AutopayError("Automatic payments are not set up for this home.")
        if (before.schedule, before.annual_month) == (schedule, annual_month):
            raise AutopayError("That is already the schedule.")
        event = AutopayEvent.objects.create(
            lot_id=account.lot_id,
            account=account,
            kind=AutopayEventKind.SCHEDULE,
            person_id=Person.objects.filter(user=user).values_list("pk", flat=True).first(),
            user=user,
            customer_id=before.method.customer_id if before.method else "",
            payment_method_id=before.method.payment_method_id if before.method else "",
            schedule=schedule,
            annual_month=annual_month,
        )
        record_event(
            "payments.autopay_schedule_changed",
            actor=user,
            request=request,
            target=account,
            metadata={
                "lot_id": account.lot_id,
                "account_id": account.pk,
                "schedule": schedule,
                "month": annual_month or 0,
            },
        )
        _close_reviews_for_schedule(account, code="schedule_changed")
    return event


def _lock_account(account) -> None:
    from community.models import Account

    Account.objects.select_for_update().filter(pk=account.pk).first()


def _close_reviews_for_schedule(account, *, code: str) -> None:
    from payments import autopay_review

    autopay_review.close_waiting(account, code=code)


@internal("autopay ownership ended")
def ownership_changed(lot_id: int) -> int:
    """Called when a lot's account periods change (a sale, an ownership
    ended): automatic payments of every account that is now closed are
    cancelled. Accounts are per ownership period, so a buyer never inherits
    the seller's."""
    from community.models import Account

    count = 0
    for account in Account.objects.filter(lot_id=lot_id, closed_on__isnull=False):
        if _cancel(account, code="ownership_ended") is not None:
            count += 1
    return count


# --- Charging ------------------------------------------------------------------------------


def _due_day() -> int:
    from community.rules import rule

    return min(rule("dues.due_day") or 1, 28)


def _period(day: datetime.date) -> str:
    return f"{day.year:04d}-{day.month:02d}"


def review_cents() -> int:
    """Automatic charges above this (surcharge included) wait for the board
    (rule payments.autopay_review_cents, D47)."""
    from community.rules import rule

    value = rule(REVIEW_RULE)
    return 50_000 if value is None else value


def _shift(year: int, month: int, months: int) -> tuple[int, int]:
    index = year * 12 + (month - 1) + months
    return index // 12, index % 12 + 1


def _add(period: str, months: int) -> str:
    year, month = (int(x) for x in period.split("-"))
    return "{:04d}-{:02d}".format(*_shift(year, month, months))


def periods_between(first: str, last: str) -> list[str]:
    """ "YYYY-MM" periods from ``first`` to ``last``, both included."""
    out = []
    current = first
    while current <= last:
        out.append(current)
        current = _add(current, 1)
    return out


def annual_periods(first: str) -> list[str]:
    """The twelve "YYYY-MM" periods an annual charge starting at ``first`` pays."""
    return periods_between(first, _add(first, ANNUAL_MONTHS - 1))


_FAILED_STATES = (CheckoutState.FAILED, CheckoutState.MISMATCH)


def _annual_attempts(account_id, *, since=None):
    """Annual attempts that hold their months (D47): not failed, or failed
    with the payment method in use now (a retry is pending, or automatic
    payments paused: either way nothing else is charged for those months)."""
    from django.db.models import Q

    q = ~Q(checkout__state__in=_FAILED_STATES)
    if since is not None:
        q |= Q(created_at__gte=since)
    return AutopayAttempt.objects.filter(account_id=account_id, schedule=ANNUAL).filter(q)


@internal("autopay coverage")
def covered_by_annual(account_id, period: str, st: State | None = None) -> bool:
    """Whether ``period`` belongs to an annual charge (made, in flight, or
    for an annual payer also held for, declined or expired at review), so
    it is never charged monthly as well (D47)."""
    from payments.models import AutopayReview, ReviewOutcome

    st = st or state(account_id)
    since = st.method.created_at if st.method else None
    for a in _annual_attempts(account_id, since=since).values_list("covers_from", "covers_through"):
        if a[0] and a[0] <= period <= (a[1] or _add(a[0], ANNUAL_MONTHS - 1)):
            return True
    if st.schedule != ANNUAL:
        return False
    for r in (
        AutopayReview.objects.filter(account_id=account_id, schedule=ANNUAL)
        .exclude(decision__outcome=ReviewOutcome.CLOSED)
        .values_list("covers_from", "covers_through")
    ):
        if r[0] and r[0] <= period <= (r[1] or _add(r[0], ANNUAL_MONTHS - 1)):
            return True
    return False


@internal("autopay coverage")
def annual_coverage_end(account_id) -> str:
    """The last month paid for by annual charges that went through or are
    in flight ("" when none)."""
    ends = [
        t or _add(f, ANNUAL_MONTHS - 1)
        for f, t in _annual_attempts(account_id).values_list("covers_from", "covers_through")
        if f
    ]
    return max(ends) if ends else ""


def annual_window(account_id, charge_period: str) -> tuple[str, str]:
    """(from, through) of an annual charge made in ``charge_period``: twelve
    months from that month, or, when earlier prepaid months run past it,
    twelve months from the month after they end (D47)."""
    end = annual_coverage_end(account_id)
    start = max(charge_period, _add(end, 1)) if end else charge_period
    return charge_period, _add(start, ANNUAL_MONTHS - 1)


@internal("autopay monthly in flight")
def _monthly_held(account_id) -> set[str]:
    """Months a monthly automatic charge is paying (made or in flight)."""
    return set(
        AutopayAttempt.objects.filter(account_id=account_id, schedule=MONTHLY)
        .exclude(checkout__state__in=_FAILED_STATES)
        .values_list("period", flat=True)
    )


def annual_amount(lot_id: int, account_id: int, first: str, through: str = "") -> tuple[list, int]:
    """(open assessment charges among the months, what an annual charge for
    ``first``..``through`` costs; twelve months by default): each month's
    assessment at its open amount when posted (0 once paid, or while a
    monthly automatic charge pays it) or the monthly amount when not posted
    yet, less any credit not yet applied (D47)."""
    from ledger import allocation

    periods = periods_between(first, through or _add(first, ANNUAL_MONTHS - 1))
    held = _monthly_held(account_id)
    monthly = _monthly_cents()
    posted: dict[str, int] = {}
    views = []
    for view in allocation.charges(lot_id, open_only=False, account=account_id):
        entry = view.entry
        if entry.kind != "assessment" or entry.period not in periods or view.reversed:
            continue
        if entry.reverses_id:
            continue
        open_ = 0 if entry.period in held else view.open_cents
        posted[entry.period] = posted.get(entry.period, 0) + open_
        if open_ > 0:
            views.append(view)
    need = sum(0 if p in held else posted.get(p, monthly) for p in periods)
    credit = allocation.unapplied_credit(lot_id, account=account_id)
    views.sort(key=lambda v: (v.entry.period, v.entry.pk))
    return views, max(need - max(credit, 0), 0)


def _assessment(lot_id: int, account_id: int, period: str):
    """This month's assessment charge on the account, as an open-charge view
    (None when not posted yet, reversed, or paid)."""
    from ledger import allocation

    for view in allocation.open_charges(lot_id, account=account_id):
        if view.entry.kind == "assessment" and view.entry.period == period and not view.reversed:
            return view
    return None


def amount_for(lot_id: int, account_id: int, period: str) -> tuple[object, int]:
    """(the assessment's open-charge view or None, what autopay charges for
    it): its open amount less any credit not yet applied."""
    from ledger import allocation

    view = _assessment(lot_id, account_id, period)
    if view is None:
        return None, 0
    credit = allocation.unapplied_credit(lot_id, account=account_id)
    return view, max(view.open_cents - max(credit, 0), 0)


def _grown(account, review, total: int) -> None:
    """An approved amount grew before it was charged (the assessment amount
    changed): nothing is charged, the board checks."""
    alert = flow._alert(
        AlertKind.AUTOPAY,
        f"autopay-review-{review.pk}",
        lot_id=account.lot_id,
        amount=total,
        detail=f"Automatic payment for {account.lot.label} was not charged: it is now "
        f"{format_cents(total)}, more than the {format_cents(review.total_cents)} the board "
        "approved. Check the account and the monthly amount in Community settings.",
    )
    if alert is not None:
        record_event(
            "payments.autopay_held",
            actor=None,
            target=account,
            metadata={
                "lot_id": account.lot_id,
                "account_id": account.pk,
                "amount_cents": total,
                "period": review.period,
            },
        )


@internal("autopay charge")
def _charge(
    account,
    st: State,
    period: str,
    *,
    try_number: int,
    schedule: str = MONTHLY,
    covers_from: str = "",
    covers_through: str = "",
) -> AutopayAttempt | None:
    """Charge once for this try: this month's assessment (monthly, period
    "YYYY-MM") or the next twelve months' (annual, period "YYYY", starting
    at ``covers_from``). Returns the attempt (or None when there was nothing
    to do, or the amount waits for the board's review)."""
    from community.models import Lot
    from payments import autopay_review

    if AutopayAttempt.objects.filter(
        account=account, period=period, try_number=try_number
    ).exists():
        return None
    if account.closed_on is not None:
        with transaction.atomic():
            _cancel(account, code="ownership_ended")
        return None
    enroller = st.method.user
    if enroller is None or account.lot_id not in flow.payable_lot_ids(enroller):
        # The person who set it up can no longer pay for this home.
        with transaction.atomic():
            _cancel(account, code="payer_left")
        return None
    if schedule == ANNUAL:
        views, base = annual_amount(account.lot_id, account.pk, covers_from, covers_through)
        charge = views[0].entry if views else None
        charge_ids = [v.entry.pk for v in views]
        if base < flow.MIN_CENTS:
            return None  # credit already covers the year
    else:
        view, base = amount_for(account.lot_id, account.pk, period)
        if view is None or base < flow.MIN_CENTS:
            return None  # not posted yet, already paid, or covered by credit
        charge, charge_ids = view.entry, [view.entry.pk]
    method = _surcharge_method(st.method)
    surcharge = fees.surcharge(base, method)
    total = base + surcharge
    limit = review_cents()
    if total > limit:
        review = autopay_review.hold(
            account,
            schedule=schedule,
            period=period,
            covers_from=covers_from,
            covers_through=covers_through,
            method=method,
            base=base,
            surcharge=surcharge,
            limit=limit,
        )
        if autopay_review.status(review) != autopay_review.APPROVED:
            return None  # waiting for the board, declined or expired
        if total > review.total_cents:
            _grown(account, review, total)
            return None
    cfg = config()
    lot = Lot.objects.get(pk=account.lot_id)
    if schedule == ANNUAL:
        key = f"autopay-{account.pk}-annual-{period}-{try_number}"
    else:
        key = f"autopay-{account.pk}-{period}-{try_number}"
    try:
        with transaction.atomic():
            checkout = PendingCheckout.objects.create(
                nonce=secrets.token_hex(32),
                lot=lot,
                account=account,
                person_id=st.method.person_id,
                user=enroller,
                method=method,
                charge_ids=charge_ids,
                base_cents=base,
                surcharge_cents=surcharge,
                surcharge_bp=fees.surcharge_bp() if method == Method.CREDIT else 0,
                total_cents=total,
                livemode=cfg.livemode,
                expires_at=timezone.now() + datetime.timedelta(days=30),
            )
            attempt = AutopayAttempt.objects.create(
                lot=lot,
                account=account,
                charge=charge,
                schedule=schedule,
                period=period,
                covers_from=covers_from if schedule == ANNUAL else "",
                covers_through=covers_through if schedule == ANNUAL else "",
                try_number=try_number,
                checkout=checkout,
                payment_method_id=st.method.payment_method_id,
                idempotency_key=key,
            )
    except IntegrityError:
        return None  # another run got there first
    record_event(
        "payments.autopay_charge_started",
        actor=None,
        target=checkout,
        metadata={
            "lot_id": lot.pk,
            "account_id": account.pk,
            "checkout_id": checkout.pk,
            "period": period,
            "try": try_number,
            "method": method,
            "amount_cents": checkout.total_cents,
            "schedule": schedule,
        },
    )
    _submit(attempt)
    return attempt


def _method_event(attempt: AutopayAttempt) -> AutopayEvent | None:
    return (
        AutopayEvent.objects.filter(
            account_id=attempt.account_id,
            payment_method_id=attempt.payment_method_id,
            kind__in=[AutopayEventKind.ENROLLED, AutopayEventKind.REPLACED],
        )
        .order_by("-created_at", "-id")
        .first()
    )


def intent_params(attempt: AutopayAttempt, event: AutopayEvent, lot_number: str) -> dict:
    checkout = attempt.checkout
    metadata = flow._metadata(checkout)
    metadata["autopay_attempt"] = str(attempt.pk)
    if attempt.schedule == ANNUAL:
        what = f"HOA assessments, {attempt.period_text}"
    else:
        what = f"HOA assessment {attempt.period}"
    params: dict = {
        "amount": checkout.total_cents,
        "currency": "usd",
        "customer": event.customer_id,
        "payment_method": event.payment_method_id,
        "payment_method_types": [event.method_type],
        "off_session": True,
        "confirm": True,
        "metadata": metadata,
        "description": f"{what}, {lot_number} (automatic payment)",
    }
    if event.mandate_id:
        params["mandate"] = event.mandate_id
    return params


@internal("autopay submit")
def _submit(attempt: AutopayAttempt, *, late: bool = False) -> None:
    """Send the off-session PaymentIntent (same idempotency key on every
    resend), then read it back from Stripe and act on what it says."""
    event = _method_event(attempt)
    checkout = attempt.checkout
    if event is None:
        _failed(attempt, "no_payment_method")
        return
    key = attempt.idempotency_key + ("-late" if late else "")
    try:
        created = stripe_api.create_payment_intent(
            intent_params(attempt, event, attempt.lot.label), idempotency_key=key
        )
    except stripe_api.StripeError as exc:
        if exc.status == 0 or exc.status >= 500 or exc.status == 429:
            return  # no answer: asked again on the next run (same key)
        if exc.object_id and _bind(checkout, exc.object_id):
            _read_back(attempt, fallback=exc.decline_code or exc.code)
            return
        _failed(attempt, exc.decline_code or exc.code or f"http_{exc.status}")
        return
    pi_id = _id_of(created)
    if not pi_id.startswith("pi_") or not _bind(checkout, pi_id):
        flow._mismatch(checkout, "payment_intent_id")
        return
    _read_back(attempt)


def _bind(checkout: PendingCheckout, pi_id: str) -> bool:
    if checkout.payment_intent_id:
        return checkout.payment_intent_id == pi_id
    try:
        with transaction.atomic():
            PendingCheckout.objects.unscoped("autopay bind").filter(pk=checkout.pk).update(
                payment_intent_id=pi_id
            )
    except IntegrityError:
        return False
    checkout.payment_intent_id = pi_id
    return True


def _read_back(attempt: AutopayAttempt, *, fallback: str = "") -> None:
    checkout = attempt.checkout
    PendingCheckout.objects.unscoped("autopay check").filter(pk=checkout.pk).update(
        last_checked_at=timezone.now()
    )
    try:
        pi = stripe_api.retrieve_payment_intent(checkout.payment_intent_id)
    except stripe_api.StripeError:
        return  # asked again on the next run
    handle(attempt, pi, fallback=fallback)


@internal("autopay outcome")
def handle(attempt: AutopayAttempt, pi: dict, *, fallback: str = "") -> str:
    """Act on the PaymentIntent Stripe reports for an automatic payment. A
    payment is recorded only when Stripe says it succeeded, through the same
    checks and recording as Pay online."""
    checkout = PendingCheckout.objects.get(pk=attempt.checkout_id)
    if checkout.state in (CheckoutState.PAID, CheckoutState.MISMATCH, CheckoutState.FAILED):
        return checkout.state
    meta = pi.get("metadata") or {}
    if (
        pi.get("id") != checkout.payment_intent_id
        or meta.get("pending_checkout") != str(checkout.pk)
        or meta.get("nonce") != checkout.nonce
        or bool(pi.get("livemode")) != checkout.livemode
        or pi.get("amount") != checkout.total_cents
        or pi.get("currency") != "usd"
    ):
        flow._mismatch(checkout, "autopay_intent")
        return checkout.state
    status = pi.get("status")
    if status == "succeeded":
        state_ = flow._from_payment_intent(checkout, pi)
        attempt.checkout.state = state_
        return state_
    if status == "processing":
        if checkout.state != CheckoutState.PROCESSING:
            flow._set_state(checkout, CheckoutState.PROCESSING)
        return checkout.state
    if status in (
        "requires_payment_method",
        "requires_action",
        "requires_confirmation",
        "canceled",
    ):
        error = pi.get("last_payment_error") or {}
        code = (
            (error.get("decline_code") if isinstance(error, dict) else "")
            or (error.get("code") if isinstance(error, dict) else "")
            or fallback
            or str(status)
        )
        _failed(attempt, str(code))
        return CheckoutState.FAILED
    return checkout.state


def _is_soft(code: str) -> bool:
    return code in SOFT_DECLINES


@internal("autopay failed")
def _failed(attempt: AutopayAttempt, code: str) -> None:
    """Record a failed automatic payment once; pause after a hard decline or
    the second failure; tell the payers."""
    code = (code or "failed")[:60]
    with transaction.atomic():
        checkout = PendingCheckout.objects.select_for_update().get(pk=attempt.checkout_id)
        if AutopayEvent.objects.filter(attempt=attempt, kind=AutopayEventKind.FAILED).exists():
            return
        if checkout.state not in (CheckoutState.PAID, CheckoutState.FAILED):
            flow._set_state(checkout, CheckoutState.FAILED)
        st = state(attempt.account_id)
        AutopayEvent.objects.create(
            lot_id=attempt.lot_id,
            account_id=attempt.account_id,
            kind=AutopayEventKind.FAILED,
            attempt=attempt,
            payment_method_id=attempt.payment_method_id,
            code=code,
        )
        record_event(
            "payments.autopay_failed",
            actor=None,
            target=checkout,
            metadata={
                "lot_id": attempt.lot_id,
                "account_id": attempt.account_id,
                "checkout_id": checkout.pk,
                "period": attempt.period,
                "try": attempt.try_number,
                "code": code,
            },
        )
        retry = _is_soft(code) and attempt.try_number == 1 and code != "returned"
        paused = False
        if not retry and st.status == ON:
            AutopayEvent.objects.create(
                lot_id=attempt.lot_id,
                account_id=attempt.account_id,
                kind=AutopayEventKind.PAUSED,
                attempt=attempt,
                payment_method_id=attempt.payment_method_id,
                code=code,
            )
            record_event(
                "payments.autopay_paused",
                actor=None,
                target=checkout,
                metadata={
                    "lot_id": attempt.lot_id,
                    "account_id": attempt.account_id,
                    "code": code,
                },
            )
            paused = True
        _email_failure(attempt, checkout, retry=retry, paused=paused)


def payer_emails(lot_id: int, user=None, account_id=None) -> set[str]:
    """The home's owners on the deed with a sign-in, plus ``user`` (or the
    person who set up the account's automatic payments)."""
    from community.models import Ownership

    with system_access("autopay emails"):
        emails = set(
            Ownership.objects.active()
            .filter(lot_id=lot_id, person__user__isnull=False, person__user__is_active=True)
            .values_list("person__user__email", flat=True)
        )
        if user is None and account_id is not None:
            st = state(account_id)
            user = st.method.user if st.method else None
        if user is not None and user.is_active:
            emails.add(user.email)
    return emails


def _payer_emails(attempt: AutopayAttempt) -> set[str]:
    with system_access("autopay emails"):
        return payer_emails(attempt.lot_id, attempt.checkout.user)


def _email_failure(attempt, checkout, *, retry: bool, paused: bool) -> None:
    from comms.notify import send_now

    lot = attempt.lot
    when = _today() + datetime.timedelta(days=RETRY_DAYS)
    if retry:
        next_step = (
            f"We will try once more on {_date_text(when)}. To avoid that, pay now from My "
            "account, or update your payment method."
        )
    elif paused:
        next_step = (
            "Automatic payments are paused for your home until you set up a payment method "
            "again. Please pay now from My account."
        )
    else:
        next_step = "Please pay now from My account, or update your payment method."
    body = "\n".join(
        [
            f"Your automatic payment for {lot.label} didn't go through.",
            "",
            f"  {_attempt_what(attempt)}: {format_cents(checkout.base_cents)}"
            + (
                f" (plus {format_cents(checkout.surcharge_cents)} credit card surcharge)"
                if checkout.surcharge_cents
                else ""
            ),
            "",
            next_step,
            "",
            settings.PORTAL_BASE_URL + reverse("my_account"),
            "",
            f"-- \n{settings.SITE_NAME}",
        ]
    )
    send_now(
        _payer_emails(attempt),
        subject=f"[{settings.SITE_NAME}] Your automatic payment didn't go through",
        body=body,
    )


def _attempt_what(attempt: AutopayAttempt) -> str:
    from community.terms import term_text

    if attempt.schedule == ANNUAL:
        return term_text(f"{{Terms}} for {attempt.period_text} (annual payment)")
    return term_text(f"{{Term}} {attempt.period_text}")


def _date_text(day: datetime.date) -> str:
    return f"{day:%B} {day.day}, {day.year}"


# --- The runs --------------------------------------------------------------------------------


@internal("autopay accounts")
def _enrolled_accounts():
    from community.models import Account

    ids = set(AutopayEvent.objects.values_list("account_id", flat=True))
    return list(Account.objects.filter(pk__in=ids).select_related("lot"))


@internal("autopay run")
def charge_due(today: datetime.date | None = None) -> int:
    """On the due day (or within a week after, if a run was missed):

    * once a year, in the payer's month: the annual charge (D47);
    * every month, for everyone: this month's assessment, unless an annual
      charge holds the month. For an annual payer that is a "bridge" month
      before the first yearly payment, or between the end of earlier prepaid
      months and the next yearly payment; credit is used first, so a month
      the credit covers costs nothing. Each month is charged once, whatever
      the schedule (monthly attempts per "YYYY-MM", annual per "YYYY")."""
    today = today or _today()
    due_day = _due_day()
    window = datetime.timedelta(days=CHARGE_WINDOW_DAYS)
    count = 0
    for account in _enrolled_accounts():
        st = state(account.pk)
        if st.status != ON or st.method is None:
            continue
        if st.schedule == ANNUAL and st.annual_month:
            due_on = datetime.date(today.year, st.annual_month, due_day)
            since = st.schedule_since or st.since
            if due_on <= today <= due_on + window and not (
                since and timezone.localdate(since) > due_on
            ):
                first, through = annual_window(account.pk, _period(due_on))
                if (
                    _charge(
                        account,
                        st,
                        f"{due_on.year:04d}",
                        try_number=1,
                        schedule=ANNUAL,
                        covers_from=first,
                        covers_through=through,
                    )
                    is not None
                ):
                    count += 1
                st = state(account.pk)
                if st.status != ON or st.method is None:
                    continue
        due_on = datetime.date(today.year, today.month, due_day)
        if not (due_on <= today <= due_on + window):
            continue
        # Set up after this month's due date: it starts next month (the
        # payer pays what is already due themselves).
        if st.since and timezone.localdate(st.since) > due_on:
            continue
        period = _period(due_on)
        if covered_by_annual(account.pk, period, st):
            continue
        if _charge(account, st, period, try_number=1) is not None:
            count += 1
    return count


@internal("autopay retries")
def retry_due(now=None) -> int:
    """One more try, three days after a soft decline, while automatic
    payments are still on (on the same schedule) and something is still
    owed."""
    now = now or timezone.now()
    count = 0
    for failure in AutopayEvent.objects.filter(
        kind=AutopayEventKind.FAILED,
        attempt__try_number=1,
        created_at__lte=now - datetime.timedelta(days=RETRY_DAYS),
        created_at__gte=now - datetime.timedelta(days=RETRY_DAYS + CHARGE_WINDOW_DAYS),
    ).select_related("attempt", "account__lot"):
        if not _is_soft(failure.code):
            continue
        attempt = failure.attempt
        st = state(failure.account_id)
        if st.status != ON or st.method is None:
            continue
        if attempt.schedule == ANNUAL and st.schedule != ANNUAL:
            continue
        if attempt.schedule == MONTHLY and covered_by_annual(
            attempt.account_id, attempt.period, st
        ):
            continue
        if (
            _charge(
                failure.account,
                st,
                attempt.period,
                try_number=2,
                schedule=attempt.schedule,
                covers_from=attempt.covers_from,
                covers_through=attempt.covers_through,
            )
            is not None
        ):
            count += 1
    return count


@internal("autopay approved")
def charge_approved(now=None) -> int:
    """Charges the board approved (D47): charged on the next run, once (the
    same attempt rows and idempotency keys as on the due day), while
    automatic payments are still on with the same schedule."""
    from payments import autopay_review

    count = 0
    for review in autopay_review.approved_uncharged(now):
        account = review.account
        st = state(account.pk)
        if st.status != ON or st.method is None:
            continue
        if review.schedule == ANNUAL and st.schedule != ANNUAL:
            continue
        if review.schedule == MONTHLY and covered_by_annual(account.pk, review.period, st):
            continue
        if (
            _charge(
                account,
                st,
                review.period,
                try_number=1,
                schedule=review.schedule,
                covers_from=review.covers_from,
                covers_through=review.covers_through,
            )
            is not None
        ):
            count += 1
    return count


@internal("autopay poll")
def poll_attempts(now=None) -> int:
    """Automatic payments Stripe has not finished with: bank payments still
    processing (asked every few hours), and charges Stripe did not answer
    about (asked again with the same idempotency key, after looking for the
    PaymentIntent first, so nothing is ever charged twice). Also: a bank
    payment returned after it was recorded pauses automatic payments."""
    now = now or timezone.now()
    count = 0
    for attempt in AutopayAttempt.objects.filter(
        checkout__state__in=[CheckoutState.OPEN, CheckoutState.PROCESSING],
        created_at__gte=now - datetime.timedelta(days=30),
    ).select_related("checkout", "lot"):
        checkout = attempt.checkout
        last = checkout.last_checked_at
        try:
            if checkout.state == CheckoutState.PROCESSING:
                if last and now - last < datetime.timedelta(hours=RECHECK_HOURS):
                    continue
                _read_back(attempt)
            elif checkout.created_at < now - datetime.timedelta(minutes=AMBIGUOUS_MINUTES):
                if checkout.payment_intent_id:
                    _read_back(attempt)
                else:
                    _resolve_unanswered(attempt, now)
            else:
                continue
        except Exception as exc:  # one account's problem never stops the others
            logger.warning(
                "payments.autopay_poll_failed",
                extra={"attempt": attempt.pk, "error": type(exc).__name__},
            )
            continue
        count += 1
        _stuck(attempt, now)
    _returned_payments()
    return count


def _stuck(attempt: AutopayAttempt, now) -> None:
    """Stripe has not settled an automatic payment for weeks (or never
    answered): the board checks it in the Stripe dashboard, as for Pay
    online (sync.poll_processing)."""
    from payments.sync import PROCESSING_DAYS

    checkout = PendingCheckout.objects.get(pk=attempt.checkout_id)
    if checkout.state not in (CheckoutState.OPEN, CheckoutState.PROCESSING):
        return
    if checkout.created_at >= now - datetime.timedelta(days=PROCESSING_DAYS):
        return
    flow._alert(
        AlertKind.MISMATCH,
        f"stuck-{checkout.pk}",
        lot_id=checkout.lot_id,
        amount=checkout.total_cents,
        ref=checkout.payment_intent_id or "",
        detail=f"Automatic payment #{checkout.pk} ({attempt.period}) has not been settled by "
        f"Stripe for over {PROCESSING_DAYS} days. Check it in the Stripe dashboard.",
    )


def _resolve_unanswered(attempt: AutopayAttempt, now) -> None:
    """Stripe did not answer when the charge was sent. Look for the
    PaymentIntent it may have made; if there is none, send it again (the
    same idempotency key while Stripe still keeps it)."""
    checkout = attempt.checkout
    event = _method_event(attempt)
    if event is None:
        _failed(attempt, "no_payment_method")
        return
    since = int(checkout.created_at.timestamp()) - 300
    page = stripe_api.list_payment_intents(
        {"customer": event.customer_id, "created": {"gte": since}, "limit": 100}
    )
    for pi in page.get("data") or []:
        if not isinstance(pi, dict):
            continue
        meta = pi.get("metadata") or {}
        if meta.get("pending_checkout") == str(checkout.pk) and meta.get("nonce") == checkout.nonce:
            if _bind(checkout, _id_of(pi)):
                _read_back(attempt)
            return
    late = checkout.created_at < now - datetime.timedelta(hours=REPOST_HOURS)
    _submit(attempt, late=late)


def _returned_payments() -> None:
    from payments.models import EventKind, StripeEvent

    for ev in StripeEvent.objects.filter(
        kind=EventKind.RETURNED, payment__checkout__autopay_attempt__isnull=False
    ).select_related("payment__checkout__autopay_attempt"):
        attempt = ev.payment.checkout.autopay_attempt
        if not AutopayEvent.objects.filter(attempt=attempt, kind=AutopayEventKind.FAILED).exists():
            _failed(attempt, "returned")


@internal("autopay setups poll")
def poll_setups(now=None) -> int:
    """Set-ups the payer did not come back from, and bank accounts waiting
    for verification."""
    now = now or timezone.now()
    count = 0
    for setup in AutopaySetup.objects.filter(
        state__in=[SetupState.OPEN, SetupState.WAITING],
        session_id__isnull=False,
        created_at__gte=now - datetime.timedelta(days=14),
    ):
        check_setup(setup, now=now)
        count += 1
    return count


def run(now=None) -> int:
    """The hourly task: nothing unless the "autopay" feature is on, online
    payments are on and Stripe is set up on this server."""
    if not autopay_enabled():
        return 0
    today = timezone.localdate(now) if now else _today()
    now = now or timezone.now()
    from payments import autopay_review

    count = poll_attempts(now)
    autopay_review.expire_due(now)
    count += charge_due(today)
    count += charge_approved(now)
    count += retry_due(now)
    return count


# --- What payers and the board see ---------------------------------------------------------


@internal("autopay next date")
def next_charge(account) -> tuple[datetime.date, int, int] | None:
    """(date, amount, surcharge) of the next automatic charge, or None when
    automatic payments are not on. The amount is 0 when credit covers it.
    Annual: the next yearly charge, or a monthly "bridge" charge before it."""
    nxt = next_charge_kind(account)
    return nxt[1:] if nxt else None


@internal("autopay next date")
def next_charge_kind(account) -> tuple[str, datetime.date, int, int] | None:
    """(MONTHLY or ANNUAL, date, amount, surcharge) of the next charge."""
    st = state(account.pk)
    if st.status != ON or st.method is None:
        return None
    monthly = _next_monthly(account, st)
    if st.schedule != ANNUAL:
        return (MONTHLY, *monthly) if monthly else None
    annual = _next_annual(account, st)
    if monthly and (
        annual is None or (_period(monthly[0]) < _period(annual[0]) and monthly[1] > 0)
    ):
        return (MONTHLY, *monthly)
    return (ANNUAL, *annual) if annual else None


def _next_monthly(account, st: State) -> tuple[datetime.date, int, int] | None:
    """The next month's assessment that is charged monthly (not held by an
    annual charge): this month's while it can still be charged, else the
    first such month after."""
    today = _today()
    due_day = _due_day()
    due_on = datetime.date(today.year, today.month, due_day)
    period = _period(due_on)
    this_month = (
        today <= due_on + datetime.timedelta(days=CHARGE_WINDOW_DAYS)
        and not (st.since and timezone.localdate(st.since) > due_on)
        and not AutopayAttempt.objects.filter(account=account, period=period).exists()
        and not covered_by_annual(account.pk, period, st)
    )
    if this_month:
        view, base = amount_for(account.lot_id, account.pk, period)
        posted = (
            view is not None or _posted(account, period)  # posted and already paid or covered
        )
        if not posted:
            base = _monthly_after_credit(account, _monthly_cents())
        if view is not None or not posted:
            when = max(due_on, today)
            return when, base, fees.surcharge(base, _surcharge_method(st.method))
    for months in range(1, 25):
        year, month = _shift(due_on.year, due_on.month, months)
        when = datetime.date(year, month, due_day)
        if covered_by_annual(account.pk, _period(when), st):
            continue
        base = _monthly_after_credit(account, _monthly_cents())
        return when, base, fees.surcharge(base, _surcharge_method(st.method))
    return None


def next_annual_date(account, st: State) -> datetime.date | None:
    """The due day of the payer's month: this year's while it can still be
    charged (not charged yet, schedule chosen before it), else next year's."""
    if not st.annual_month:
        return None
    today = _today()
    due_day = _due_day()
    due_on = datetime.date(today.year, st.annual_month, due_day)
    since = st.schedule_since or st.since
    usable = (
        today <= due_on + datetime.timedelta(days=CHARGE_WINDOW_DAYS)
        and not (since and timezone.localdate(since) > due_on)
        and not AutopayAttempt.objects.filter(account=account, period=f"{due_on.year:04d}").exists()
    )
    if usable:
        return max(due_on, today)
    return datetime.date(today.year + 1, st.annual_month, due_day)


def _next_annual(account, st: State) -> tuple[datetime.date, int, int] | None:
    when = next_annual_date(account, st)
    if when is None:
        return None
    first, through = annual_window(account.pk, _period(when))
    _views, base = annual_amount(account.lot_id, account.pk, first, through)
    return when, base, fees.surcharge(base, _surcharge_method(st.method))


@internal("autopay prepaid")
def prepaid_through(account) -> datetime.date | None:
    """The last month the account's money already pays for (posted
    assessments all paid, then whole months of credit at today's amount),
    or None when this month's assessment is not paid."""
    from ledger import allocation

    latest = None
    for view in allocation.charges(account.lot_id, open_only=False, account=account.pk):
        entry = view.entry
        if entry.kind != "assessment" or view.reversed or entry.reverses_id or not entry.period:
            continue
        if view.open_cents > 0:
            return None
        if latest is None or entry.period > latest:
            latest = entry.period
    today = _today()
    if latest is None:
        year, month = _shift(today.year, today.month, -1)
    else:
        year, month = (int(x) for x in latest.split("-"))
    monthly = _monthly_cents()
    credit = allocation.unapplied_credit(account.lot_id, account=account.pk)
    if monthly > 0 and credit > 0:
        year, month = _shift(year, month, credit // monthly)
    last = datetime.date(year, month, 1)
    if last < today.replace(day=1):
        return None
    return last


def _posted(account, period: str) -> bool:
    from ledger.models import LedgerEntry

    return LedgerEntry.objects.filter(
        account_id=account.pk, kind="assessment", period=period
    ).exists()


def _monthly_after_credit(account, monthly: int) -> int:
    from ledger import allocation

    credit = allocation.unapplied_credit(account.lot_id, account=account.pk)
    return max(monthly - max(credit, 0), 0)


def _retry_on(st: State) -> datetime.date | None:
    failure = st.last_failure
    if failure is None or failure.attempt is None or failure.attempt.try_number != 1:
        return None
    if not _is_soft(failure.code):
        return None
    if failure.created_at < timezone.now() - datetime.timedelta(
        days=RETRY_DAYS + CHARGE_WINDOW_DAYS
    ):
        return None  # past the retry window (retry_due's)
    if AutopayAttempt.objects.filter(
        account_id=failure.account_id, period=failure.attempt.period, try_number=2
    ).exists():
        return None
    return timezone.localdate(failure.created_at) + datetime.timedelta(days=RETRY_DAYS)


def _account(account):
    from community.models import Account

    if isinstance(account, Account):
        return account
    with system_access("autopay account"):
        return Account.objects.filter(pk=account).first()


@internal("autopay statement")
def describe_for_statement(account) -> str | None:
    """One sentence for the monthly statement email (an Account or its id):
    when and how the next assessment is paid automatically, or that
    automatic payments are paused; None when they are not set up (or the
    feature is off)."""
    account = _account(account)
    if account is None or not autopay_enabled():
        return None
    st = state(account.pk)
    if st.status == OFF or st.method is None:
        return None
    if st.status == PAUSED:
        return (
            "Automatic payments are paused because a payment didn't go through. Please pay "
            "from My account and set up your payment method again to restart them."
        )
    retry_on = _retry_on(st)
    if retry_on is not None:
        return (
            f"Your last automatic payment didn't go through; we will try again on "
            f"{_date_text(retry_on)} from your {st.label}."
        )
    review = _waiting_review(account)
    if review is not None:
        if review.schedule == ANNUAL:
            return (
                f"Your annual automatic payment of {format_cents(review.total_cents)} is "
                "waiting for the board's review."
            )
        return (
            f"Your automatic payment of {format_cents(review.total_cents)} is waiting for "
            "the board's review."
        )
    nxt = next_charge_kind(account)
    if nxt is None:
        return None
    kind, when, base, surcharge = nxt
    if st.schedule == ANNUAL:
        annual = _describe_annual(account, kind, when, base, surcharge)
        if annual:
            return annual
    if base <= 0:
        return (
            f"Your credit on account covers the assessment due {_date_text(when)}, so nothing "
            "will be charged automatically then."
        )
    text = f"This will be paid automatically on {_date_text(when)} from your {st.label}"
    if surcharge:
        return text + f" (card surcharge {format_cents(surcharge)} included)."
    return text + "."


def _waiting_review(account):
    from payments import autopay_review

    return autopay_review.waiting_for(account.pk)


def _describe_annual(account, kind: str, when: datetime.date, base: int, surcharge: int):
    """The annual sentences; None for a monthly bridge charge (the monthly
    sentence then)."""
    from community.terms import term_text

    through = prepaid_through(account)
    paid_annually = AutopayAttempt.objects.filter(
        account=account, schedule=ANNUAL, checkout__state=CheckoutState.PAID
    ).exists()
    if through is not None and paid_annually and through >= _today().replace(day=1):
        return term_text(
            f"Your {{terms}} are prepaid through {through:%B %Y} by your annual automatic payment."
        )
    if kind == MONTHLY:
        return None
    if base <= 0:
        return term_text(
            "Your credit on account covers the next 12 months' {terms}, so nothing will be "
            f"charged automatically on {_date_text(when)}."
        )
    text = (
        f"Your annual automatic payment of {format_cents(base + surcharge)} is due on "
        f"{_date_text(when)}"
    )
    if surcharge:
        return text + f" (card surcharge {format_cents(surcharge)} included)."
    return text + "."


@internal("autopay owner view")
def for_payer(user, lot) -> dict | None:
    """What My account shows a payer about automatic payments."""
    if lot.pk not in flow.payable_lot_ids(user) or not autopay_enabled():
        return None
    account = _current_account(lot.pk)
    if account is None:
        return None
    st = state(account.pk)
    waiting = (
        AutopaySetup.objects.filter(account=account, state=SetupState.WAITING)
        .order_by("-created_at")
        .first()
    )
    review = _waiting_review(account) if st.status != OFF else None
    nxt = next_charge(account) if st.status == ON else None
    return {
        "status": st.status,
        "label": st.label,
        "since": st.since,
        "schedule": st.schedule,
        "annual_month": st.annual_month,
        "schedule_text": st.schedule_text,
        "description": describe_for_statement(account),
        "waiting": waiting,
        "retry_on": _retry_on(st) if st.status == ON else None,
        "failed": st.last_failure,
        "review": review,
        "review_text": _review_text(review),
        "next": nxt,
        "prepaid_through": prepaid_through(account) if st.schedule == ANNUAL else None,
        "bridge_text": bridge_text(account, st) if st.status != OFF else [],
    }


@internal("autopay bridge text")
def bridge_text(account, st: State) -> list[str]:
    """For an annual payer: how the months before the next yearly payment
    are paid, and what the next yearly payment covers (D47)."""
    if st.schedule != ANNUAL:
        return []
    when = next_annual_date(account, st)
    if when is None:
        return []
    charge_period = _period(when)
    end = annual_coverage_end(account.pk)
    if not end:
        return [
            f"Until your first yearly payment in {when:%B %Y}, each month is paid "
            "automatically as usual."
        ]
    out = []
    if _add(end, 1) < charge_period:
        out.append(
            f"Your prepaid months end in {_month_text(end)}. Until your next yearly payment "
            f"in {when:%B %Y}, each month is paid automatically as usual."
        )
    _first, through = annual_window(account.pk, charge_period)
    start = _add(through, -(ANNUAL_MONTHS - 1))
    if start > charge_period:
        out.append(
            f"Your next yearly payment, in {when:%B %Y}, pays for the 12 months from "
            f"{_month_text(start)}, the month after your prepaid months end."
        )
    return out


def _month_text(period: str) -> str:
    year, month = (int(x) for x in period.split("-"))
    return f"{datetime.date(year, month, 1):%B %Y}"


def _review_text(review) -> str:
    if review is None:
        return ""
    if review.schedule == ANNUAL:
        return (
            f"Your annual payment of {format_cents(review.total_cents)} is waiting for the "
            "board's review."
        )
    from payments import autopay_review

    return (
        f"Your automatic payment of {format_cents(review.total_cents)} for "
        f"{autopay_review.what_text(review)} is waiting for the board's review."
    )


@internal("autopay to-dos")
def payer_items(user) -> list:
    """My items: an automatic payment that didn't go through (until the
    charge is paid or a payment method is set up again), or paused."""
    from community.models import Lot
    from core.my_items import Item
    from ledger import allocation

    if not autopay_enabled():
        return []
    out = []
    payable = flow.payable_lot_ids(user)
    for lot in Lot.objects.filter(pk__in=payable).order_by("number"):
        account = _current_account(lot.pk)
        if account is None:
            continue
        st = state(account.pk)
        failure = st.last_failure
        if st.status == OFF or failure is None:
            continue
        attempt = failure.attempt
        if attempt is not None and attempt.charge_id is None:
            # Annual, before any of the twelve months was posted: open while
            # the account has an open assessment.
            charge_open = any(
                v.entry.kind == "assessment"
                for v in allocation.open_charges(lot.pk, account=account.pk)
            )
        else:
            charge_open = bool(
                attempt and allocation.charge_status(attempt.charge) in ("open", "partial")
            )
        if st.status == ON and not charge_open:
            continue
        retry_on = _retry_on(st) if st.status == ON else None
        detail = "Pay now or update the payment method"
        if st.status == PAUSED:
            detail += " · automatic payments are paused"
        elif retry_on:
            detail += f" · we try again on {_date_text(retry_on)}"
        out.append(
            Item(
                "My home",
                f"Your automatic payment didn't go through ({lot.label})",
                detail,
                reverse("autopay", args=[lot.pk]),
                "warning",
                timezone.localdate(failure.created_at),
                nav="my_account",
                icon="wallet",
                action_label="Pay now" if charge_open else "",
                action_url=reverse("pay_online", args=[lot.pk]) + "?all=1" if charge_open else "",
            )
        )
    return out


# How long a failed automatic payment stays on the board's My items while it
# is still unresolved (D74).
BOARD_FAILURE_DAYS = 30


def _failure_open(lot_id, account, st: State) -> bool:
    """The account's last failure still matters: paused, or its charge open."""
    from ledger import allocation

    if st.status == PAUSED:
        return True
    failure = st.last_failure
    attempt = failure.attempt if failure else None
    if attempt is None:
        return False
    if attempt.charge_id is None:
        return any(
            v.entry.kind == "assessment" for v in allocation.open_charges(lot_id, account=account)
        )
    return allocation.charge_status(attempt.charge) in ("open", "partial")


def board_failure_items(user) -> list:
    """My items for the board (D74): an automatic payment that didn't go
    through, one per home, while it is unresolved (paused, or the charge is
    still open) and at most BOARD_FAILURE_DAYS old. The payers were emailed;
    the board sees it so a paused home is followed up. Never one's own home
    (D65). Online payments lists the failures."""
    from core import conflicts
    from core.my_items import Item

    if not autopay_enabled():
        return []
    since = timezone.now() - datetime.timedelta(days=BOARD_FAILURE_DAYS)
    with system_access("autopay failures for the board"):
        events = list(
            AutopayEvent.objects.filter(kind=AutopayEventKind.FAILED, created_at__gte=since)
            .select_related("lot", "attempt")
            .order_by("-created_at", "-id")
        )
        out, seen = [], set()
        for event in events:
            if event.account_id in seen:
                continue
            seen.add(event.account_id)
            st = state(event.account_id)
            if st.status == OFF or st.last_failure is None or st.last_failure.pk != event.pk:
                continue
            current = _current_account(event.lot_id)
            if current is None or current.pk != event.account_id:
                continue
            if not _failure_open(event.lot_id, event.account_id, st):
                continue
            if conflicts.own_matter(user, event):
                continue
            detail = "The payers were emailed"
            detail += (
                " · automatic payments are paused until a payment method is set up again"
                if st.status == PAUSED
                else " · the charge is still open"
            )
            out.append(
                Item(
                    "Finance overview",
                    f"Automatic payment didn't go through for {event.lot.label}",
                    detail,
                    reverse("online_payments") + "#autopay-failures",
                    "warning",
                    timezone.localdate(event.created_at),
                    nav="online_payments",
                    side="board",
                )
            )
    return out


@internal("autopay board")
def board_summary(limit: int = 30) -> dict:
    """For the board's Online payments page: every account with automatic
    payments set up (status, kind of method, since, failures in 90 days) and
    the recent failures. No card or bank details."""
    since = timezone.now() - datetime.timedelta(days=90)
    rows = []
    for account in _enrolled_accounts():
        st = state(account.pk)
        if st.status == OFF:
            continue
        rows.append(
            {
                "lot": account.lot,
                "account": account,
                "status": st.status,
                "method_type": st.method.get_method_type_display() if st.method else "",
                "schedule": st.schedule_text,
                "since": st.since,
                "failures": AutopayEvent.objects.filter(
                    account=account, kind=AutopayEventKind.FAILED, created_at__gte=since
                ).count(),
            }
        )
    rows.sort(key=lambda r: r["lot"].number)
    failures = list(
        AutopayEvent.objects.filter(kind=AutopayEventKind.FAILED)
        .select_related("lot", "attempt")
        .order_by("-created_at", "-id")[:limit]
    )
    return {"enrolments": rows, "failures": failures}


def require_payer(user, lot) -> None:
    if lot.pk not in flow.payable_lot_ids(user):
        raise PermissionDenied("payments.pay")
