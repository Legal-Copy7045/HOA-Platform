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
