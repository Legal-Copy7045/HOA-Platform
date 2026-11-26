"""Online payments through Stripe hosted Checkout (decision D32-payments).

The portal never sees card or bank numbers. It learns that money arrived
only by asking Stripe (pull, never push: no webhook), and records a payment
only when what Stripe reports matches the checkout this portal started.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils import timezone

from core.append_only import AppendOnlyModel, ScopedAppendOnlyModel
from core.scoping import ScopedModel, ScopedQuerySet


class Method(models.TextChoices):
    BANK = "bank", "Bank account (no fee)"
    DEBIT = "debit", "Debit card (no fee)"
    CREDIT = "credit", "Credit card"


class CheckoutState(models.TextChoices):
    OPEN = "open", "Waiting for payment"
    PROCESSING = "processing", "Bank payment processing"
    PAID = "paid", "Paid"
    FAILED = "failed", "Failed"
    EXPIRED = "expired", "Expired"
    MISMATCH = "mismatch", "Not recorded: did not match"
    # Stripe says it has no such checkout (e.g. after the key changed), and
    # the board was told: no longer asked about (fixres BUG-005).
    UNREACHABLE = "unreachable", "Not found at Stripe"


class PendingCheckout(ScopedModel):
    """A checkout started by a signed-in person, created before the
    redirect to Stripe. Everything Stripe later reports is compared with
    this row; it is consumed once."""

    scope_lot_path = "lot"
    objects = ScopedQuerySet.as_manager()

    nonce = models.CharField(max_length=64, unique=True)
    lot = models.ForeignKey("community.Lot", on_delete=models.PROTECT, related_name="+")
    account = models.ForeignKey(
        "community.Account", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    person = models.ForeignKey("community.Person", on_delete=models.PROTECT, related_name="+")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    method = models.CharField(max_length=8, choices=Method.choices)
    charge_ids = models.JSONField(default=list, blank=True)
    base_cents = models.BigIntegerField()
    surcharge_cents = models.BigIntegerField(default=0)
    surcharge_bp = models.PositiveIntegerField(default=0)
    total_cents = models.BigIntegerField()
    livemode = models.BooleanField(default=False)
    session_id = models.CharField(max_length=255, unique=True, null=True, blank=True)
    payment_intent_id = models.CharField(max_length=255, unique=True, null=True, blank=True)
    state = models.CharField(
        max_length=12, choices=CheckoutState.choices, default=CheckoutState.OPEN
    )
    state_changed_at = models.DateTimeField(default=timezone.now)
    last_checked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(base_cents__gt=0)
                & models.Q(surcharge_cents__gte=0)
                & models.Q(total_cents=models.F("base_cents") + models.F("surcharge_cents")),
                name="checkout_amounts_add_up",
            )
        ]

    def __str__(self) -> str:
        return f"Online payment #{self.pk}"

    def delete(self, *args, **kwargs):
        raise PermissionError("Checkouts are kept.")

    @property
    def client_reference(self) -> str:
        return f"pc{self.pk}"


class StripePayment(ScopedAppendOnlyModel):
    """A payment Stripe confirmed, with the ledger payment it became. The
    unique PaymentIntent id makes recording idempotent."""

    scope_lot_path = "lot"

    checkout = models.OneToOneField(
        PendingCheckout, on_delete=models.PROTECT, related_name="payment"
    )
    lot = models.ForeignKey("community.Lot", on_delete=models.PROTECT, related_name="+")
    entry = models.OneToOneField("ledger.LedgerEntry", on_delete=models.PROTECT, related_name="+")
    payment_intent_id = models.CharField(max_length=255, unique=True)
    charge_id = models.CharField(max_length=255, unique=True)
    balance_transaction_id = models.CharField(max_length=255, blank=True)
    method = models.CharField(max_length=8, choices=Method.choices)
    funding = models.CharField(max_length=16, blank=True)  # credit/debit/prepaid/unknown/bank
    base_cents = models.BigIntegerField()
    surcharge_cents = models.BigIntegerField(default=0)
    fee_cents = models.BigIntegerField(null=True, blank=True)  # Stripe's processing fee
    livemode = models.BooleanField(default=False)
    recorded_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-recorded_at"]

    def __str__(self) -> str:
        return f"Online payment {self.payment_intent_id}"

    @property
    def total_cents(self) -> int:
        return self.base_cents + self.surcharge_cents


class EventKind(models.TextChoices):
    REFUND_SEEN = "refund", "Refund seen at Stripe"
    DISPUTE_SEEN = "dispute", "Dispute opened at Stripe"
    RETURNED = "returned", "Bank payment returned"
    FEE_SEEN = "fee", "Stripe fee recorded"


class StripeEvent(ScopedAppendOnlyModel):
    """Something the poller saw at Stripe after a payment was recorded."""

    scope_lot_path = "payment__lot"

    payment = models.ForeignKey(StripePayment, on_delete=models.PROTECT, related_name="events")
    kind = models.CharField(max_length=10, choices=EventKind.choices)
    stripe_id = models.CharField(max_length=255)  # re_..., dp_..., ch_...
    amount_cents = models.BigIntegerField(default=0)
    seen_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["seen_at", "id"]
        constraints = [
            models.UniqueConstraint(fields=["kind", "stripe_id"], name="stripe_event_once")
        ]

    def __str__(self) -> str:
        return f"{self.kind} {self.stripe_id}"


class AlertKind(models.TextChoices):
    MISMATCH = "mismatch", "Payment did not match its checkout"
    RETURNED = "returned", "Bank payment returned"
    REFUND = "refund", "Refund to record"
    DISPUTE = "dispute", "Dispute to answer"
    SURCHARGE_REFUND = "surcharge", "Refund a surcharge"
    RECONCILIATION = "reconcile", "Stripe records do not match"
    FORMER_OWNER = "former", "Payment from a former owner"
    # Autopay held back (payments.autopay): before D47 an amount over the
    # ceiling; since D47 an approved amount that grew before it was charged.
    AUTOPAY = "autopay", "Automatic payment held back"


class PaymentAlert(ScopedModel):
    """A to-do for the board on My items. Closed with a note, never deleted."""

    scope_lot_path = "lot"
    objects = ScopedQuerySet.as_manager()

    kind = models.CharField(max_length=10, choices=AlertKind.choices)
    key = models.CharField(max_length=255)  # Stripe id or check code: one alert each
    lot = models.ForeignKey(
        "community.Lot", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    amount_cents = models.BigIntegerField(default=0)
    stripe_ref = models.CharField(max_length=255, blank=True)
    detail = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    resolution = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [models.UniqueConstraint(fields=["kind", "key"], name="payment_alert_once")]

    def __str__(self) -> str:
        return self.get_kind_display()

    def delete(self, *args, **kwargs):
        raise PermissionError("Payment alerts are closed, not deleted.")


class StripeBalanceTransaction(AppendOnlyModel):
    """A line of the HOA's Stripe balance (charge, refund, fee, payout...),
    copied from Stripe for the three-way reconciliation."""

    bt_id = models.CharField(max_length=255, unique=True)
    type = models.CharField(max_length=40)
    source_id = models.CharField(max_length=255, blank=True)  # ch_/py_/re_/po_...
    amount_cents = models.BigIntegerField()
    fee_cents = models.BigIntegerField(default=0)
    net_cents = models.BigIntegerField()
    created = models.DateTimeField()
    livemode = models.BooleanField(default=False)
    fetched_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created"]

    def __str__(self) -> str:
        return self.bt_id


class StripePayout(AppendOnlyModel):
    """A payout from Stripe to the HOA's bank account, with the balance lines
    it settles. The bank deposit should equal ``amount_cents``."""

    payout_id = models.CharField(max_length=255, unique=True)
    amount_cents = models.BigIntegerField()  # net, as it reaches the bank
    gross_cents = models.BigIntegerField(default=0)  # payments
    refunds_cents = models.BigIntegerField(default=0)
    fees_cents = models.BigIntegerField(default=0)
    other_cents = models.BigIntegerField(default=0)
    arrival_date = models.DateField()
    status = models.CharField(max_length=20)
    livemode = models.BooleanField(default=False)
    fetched_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-arrival_date", "-id"]

    def __str__(self) -> str:
        return self.payout_id

    @property
    def adds_up(self) -> bool:
        return (
            self.gross_cents - self.refunds_cents - self.fees_cents + self.other_cents
            == self.amount_cents
        )


class StripePayoutItem(AppendOnlyModel):
    payout = models.ForeignKey(StripePayout, on_delete=models.PROTECT, related_name="items")
    bt_id = models.CharField(max_length=255)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["payout", "bt_id"], name="payout_item_once")]

    def __str__(self) -> str:
        return self.bt_id


class StripeReconciliation(AppendOnlyModel):
    """One run of the daily ledger-versus-Stripe comparison."""

    ran_at = models.DateTimeField(default=timezone.now)
    ok = models.BooleanField()
    problems = models.JSONField(default=list, blank=True)  # codes and Stripe ids only
    checked_payments = models.PositiveIntegerField(default=0)
    checked_payouts = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["-ran_at", "-id"]

    def __str__(self) -> str:
        return f"Stripe reconciliation {self.ran_at:%Y-%m-%d}"


# --- Automatic payments (payments.autopay) ---------------------------------------------------


class AutopayType(models.TextChoices):
    """The kind of payment method Stripe saved (Stripe's own type names)."""

    CARD = "card", "Card"
    BANK = "us_bank_account", "Bank account"


class AutopaySchedule(models.TextChoices):
    """How often automatic payments charge (D47)."""

    MONTHLY = "monthly", "Monthly"
    ANNUAL = "annual", "Once a year"


class StripeCustomer(ScopedAppendOnlyModel):
    """The Stripe Customer of one ownership account (created on first use, one
    per account and mode). Stripe holds only internal ids as its metadata:
    no name, email or address is sent."""

    scope_lot_path = "account__lot"

    account = models.ForeignKey("community.Account", on_delete=models.PROTECT, related_name="+")
    customer_id = models.CharField(max_length=255, unique=True)
    livemode = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["account", "livemode"], name="stripe_customer_one_per_account"
            )
        ]

    def __str__(self) -> str:
        return self.customer_id


class SetupState(models.TextChoices):
    OPEN = "open", "Waiting for the payer on Stripe's page"
    WAITING = "waiting", "Waiting for the bank account to be verified"
    DONE = "done", "Set up"
    FAILED = "failed", "Not set up"
    EXPIRED = "expired", "Expired"
    MISMATCH = "mismatch", "Not set up: did not match"


class AutopaySetup(ScopedModel):
    """A payment-method set-up started by a signed-in payer, created before the
    redirect to Stripe's hosted page (Checkout in setup mode). Like
    PendingCheckout, everything Stripe later reports is compared with this
    row, and it is consumed once."""

    scope_lot_path = "lot"
    objects = ScopedQuerySet.as_manager()

    nonce = models.CharField(max_length=64, unique=True)
    lot = models.ForeignKey("community.Lot", on_delete=models.PROTECT, related_name="+")
    account = models.ForeignKey("community.Account", on_delete=models.PROTECT, related_name="+")
    person = models.ForeignKey("community.Person", on_delete=models.PROTECT, related_name="+")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    method_type = models.CharField(max_length=20, choices=AutopayType.choices)
    # The schedule the payer chose on the set-up page (D47); the month of the
    # annual charge (1-12) for "annual".
    schedule = models.CharField(
        max_length=8, choices=AutopaySchedule.choices, default=AutopaySchedule.MONTHLY
    )
    annual_month = models.PositiveSmallIntegerField(null=True, blank=True)
    customer_id = models.CharField(max_length=255)
    livemode = models.BooleanField(default=False)
    session_id = models.CharField(max_length=255, unique=True, null=True, blank=True)
    setup_intent_id = models.CharField(max_length=255, unique=True, null=True, blank=True)
    state = models.CharField(max_length=10, choices=SetupState.choices, default=SetupState.OPEN)
    state_changed_at = models.DateTimeField(default=timezone.now)
    last_checked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Automatic payment set-up #{self.pk}"

    def delete(self, *args, **kwargs):
        raise PermissionError("Set-ups are kept.")

    @property
    def client_reference(self) -> str:
        return f"as{self.pk}"


def twelve_months_start(row):
    """The first of the twelve months an annual attempt or review buys."""
    import datetime

    if row.covers_through:
        year, month = (int(x) for x in row.covers_through.split("-"))
        index = year * 12 + month - 1 - 11
        return datetime.date(index // 12, index % 12 + 1, 1)
    year, month = (int(x) for x in (row.covers_from or f"{row.period}-01").split("-"))
    return datetime.date(year, month, 1)


class AutopayAttempt(ScopedAppendOnlyModel):
    """One automatic charge (try 1 on the due day, try 2 the one retry): of
    one month's assessment (schedule monthly, period "YYYY-MM"), or of the
    next twelve months' assessments (schedule annual, period "YYYY": one per
    account and year, D47). Bound to a PendingCheckout so the existing
    verification and recording path records the payment once Stripe says it
    succeeded; the unique (account, period, try) and the idempotency key
    make re-runs harmless."""

    scope_lot_path = "lot"

    lot = models.ForeignKey("community.Lot", on_delete=models.PROTECT, related_name="+")
    account = models.ForeignKey("community.Account", on_delete=models.PROTECT, related_name="+")
    # The month's assessment (monthly); for an annual charge, the first of the
    # twelve months' assessments when it was already posted, else empty.
    charge = models.ForeignKey(
        "ledger.LedgerEntry", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    schedule = models.CharField(
        max_length=8, choices=AutopaySchedule.choices, default=AutopaySchedule.MONTHLY
    )
    period = models.CharField(max_length=7)
    # Annual (D47): the months this charge pays for, "YYYY-MM" to "YYYY-MM":
    # from the month it is charged to the end of the twelve months it buys
    # (longer than twelve when it starts after earlier prepaid months end).
    covers_from = models.CharField(max_length=7, blank=True)
    covers_through = models.CharField(max_length=7, blank=True)
    try_number = models.PositiveSmallIntegerField(default=1)
    checkout = models.OneToOneField(
        PendingCheckout, on_delete=models.PROTECT, related_name="autopay_attempt"
    )
    payment_method_id = models.CharField(max_length=255)
    idempotency_key = models.CharField(max_length=100, unique=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["account", "period", "try_number"], name="autopay_attempt_once"
            )
        ]

    def __str__(self) -> str:
        return f"Automatic payment {self.period} try {self.try_number}"

    @property
    def period_text(self) -> str:
        """ "March 2027" (monthly) or "the year from January 2027" (annual)."""
        import datetime

        if self.schedule == AutopaySchedule.ANNUAL:
            return f"twelve months from {twelve_months_start(self):%B %Y}"
        year, month = (int(x) for x in self.period.split("-"))
        return f"{datetime.date(year, month, 1):%B %Y}"


class AutopayEventKind(models.TextChoices):
    ENROLLED = "enrolled", "Set up"
    REPLACED = "replaced", "Payment method replaced"
    CANCELLED = "cancelled", "Cancelled"
    FAILED = "failed", "Payment failed"
    PAUSED = "paused", "Paused"
    # D47: the payer changed the schedule (monthly / once a year, and month).
    SCHEDULE = "schedule", "Schedule changed"


class AutopayEvent(ScopedAppendOnlyModel):
    """What happened to an account's automatic payments, append-only. The
    current state (on, paused, off) is derived from these rows."""

    scope_lot_path = "lot"

    lot = models.ForeignKey("community.Lot", on_delete=models.PROTECT, related_name="+")
    account = models.ForeignKey("community.Account", on_delete=models.PROTECT, related_name="+")
    kind = models.CharField(max_length=10, choices=AutopayEventKind.choices)
    setup = models.ForeignKey(
        AutopaySetup, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    attempt = models.ForeignKey(
        AutopayAttempt, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    person = models.ForeignKey(
        "community.Person", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    # Stripe ids and the display details Stripe returns (brand or bank name
    # and the last four digits): never a card or account number.
    customer_id = models.CharField(max_length=255, blank=True)
    payment_method_id = models.CharField(max_length=255, blank=True)
    method_type = models.CharField(max_length=20, choices=AutopayType.choices, blank=True)
    funding = models.CharField(max_length=16, blank=True)  # credit/debit/prepaid/unknown
    brand = models.CharField(max_length=40, blank=True)
    bank_name = models.CharField(max_length=80, blank=True)
    last4 = models.CharField(max_length=4, blank=True)
    mandate_id = models.CharField(max_length=255, blank=True)
    # The schedule set by this row (enrolled, replaced, schedule; D47). Blank
    # on other kinds and on rows from before D47 (those mean monthly).
    schedule = models.CharField(max_length=8, choices=AutopaySchedule.choices, blank=True)
    annual_month = models.PositiveSmallIntegerField(null=True, blank=True)
    # Why: a Stripe decline or error code, or "owner", "ownership_ended"...
    code = models.CharField(max_length=60, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self) -> str:
        return f"Automatic payments {self.kind}"


class AutopayReview(ScopedAppendOnlyModel):
    """An automatic charge above the review limit (rule
    ``payments.autopay_review_cents``, D47), held for the board instead of
    being charged. One per account and period; what happened to it is an
    ``AutopayReviewDecision`` (approved, declined or expired)."""

    scope_lot_path = "lot"

    lot = models.ForeignKey("community.Lot", on_delete=models.PROTECT, related_name="+")
    account = models.ForeignKey("community.Account", on_delete=models.PROTECT, related_name="+")
    schedule = models.CharField(max_length=8, choices=AutopaySchedule.choices)
    period = models.CharField(max_length=7)  # as on AutopayAttempt
    covers_from = models.CharField(max_length=7, blank=True)  # annual: as on AutopayAttempt
    covers_through = models.CharField(max_length=7, blank=True)
    method = models.CharField(max_length=8, choices=Method.choices)
    base_cents = models.BigIntegerField()
    surcharge_cents = models.BigIntegerField(default=0)
    total_cents = models.BigIntegerField()
    limit_cents = models.BigIntegerField()
    created_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["account", "period"], name="autopay_review_once")
        ]

    def __str__(self) -> str:
        return f"Automatic payment review #{self.pk}"


class ReviewOutcome(models.TextChoices):
    APPROVED = "approved", "Approved"
    DECLINED = "declined", "Declined"
    EXPIRED = "expired", "Expired"
    CLOSED = "closed", "Closed: the payer changed the schedule"


class AutopayReviewDecision(ScopedAppendOnlyModel):
    """The board's answer to an ``AutopayReview`` (or its expiry after 14
    days), once."""

    scope_lot_path = "review__lot"

    review = models.OneToOneField(AutopayReview, on_delete=models.PROTECT, related_name="decision")
    outcome = models.CharField(max_length=8, choices=ReviewOutcome.choices)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    reason = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    def __str__(self) -> str:
        return f"Review #{self.review_id} {self.outcome}"
