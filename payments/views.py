"""Online payment pages (decision D32-payments). Every page needs sign-in;
there is no webhook or other anonymous endpoint."""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from audit.services import record_event
from community.models import Lot
from core import policy
from core.money import format_cents, parse_cents
from documents.attachments import pdf_response
from payments import checkout as flow
from payments import services as fees
from payments.models import (
    CheckoutState,
    PaymentAlert,
    PendingCheckout,
    StripePayment,
    StripePayout,
    StripeReconciliation,
)
from payments.providers import config


def _lot_for_payer(request, lot_id) -> Lot:
    if lot_id not in flow.payable_lot_ids(request.user):
        raise Http404
    return get_object_or_404(Lot.objects.for_user(request.user), pk=lot_id)


def _available_or_404():
    if not config().available:
        raise Http404


def _context(request, lot, *, error="", q=None, chosen=None, amount=""):
    cfg = config()
    open_charges = flow._own_open_charges(request.user, lot)
    return {
        "lot": lot,
        "open_charges": open_charges,
        "due_cents": flow.payable_cents(lot.pk),
        "quote": q,
        "chosen": set(chosen if chosen is not None else [c.entry.pk for c in open_charges]),
        "amount": amount,
        "error": error,
        "test_mode": not cfg.livemode,
        "disclosure": fees.disclosure(),
        "surcharge_bp": fees.surcharge_bp(),
    }


@login_required
def pay_online(request, lot_id):
    """The ways to pay, each with its exact fee and total. Opened plainly (or
    with ?all=1 from the dashboard's "Pay $X"), it quotes every open charge
    so the payer chooses the method first; ?choose=1 ("Pay only some
    charges") shows the charge list and the typed-amount box, posted back
    here for the quote. The query string only picks the step: which charges
    and how much are always worked out again on the server."""
    policy.require(request.user, "payments.pay")
    _available_or_404()
    lot = _lot_for_payer(request, lot_id)
    if request.method != "POST":
        ctx = _context(request, lot)
        if request.GET.get("choose"):
            return render(request, "payments/pay.html", ctx)
        everything = [c.entry.pk for c in ctx["open_charges"]]
        try:
            q = flow.quote(request.user, lot, charge_ids=everything)
        except flow.PaymentError as exc:
            # Nothing open, or below the smallest payment: the choice form
            # says why and still offers a typed amount.
            ctx["error"] = str(exc) if ctx["due_cents"] <= 0 or everything else ""
            return render(request, "payments/pay.html", ctx)
        ctx = _context(request, lot, q=q, chosen=q.charge_ids)
        ctx["base_text"] = format_cents(q.base_cents)
        ctx["paying_all"] = True
        return render(request, "payments/pay.html", ctx)
    chosen = request.POST.getlist("charge")
    raw_amount = (request.POST.get("amount") or "").strip()
    amount = None
    try:
        if raw_amount:
            amount = parse_cents(raw_amount)
        q = flow.quote(request.user, lot, charge_ids=chosen, amount_cents=amount)
    except ValueError as exc:
        message = str(exc) if isinstance(exc, flow.PaymentError) else "Enter dollars, e.g. 45.00."
        ctx = _context(request, lot, error=message, chosen=[int(c) for c in chosen if c.isdigit()])
        ctx["amount"] = raw_amount
        return render(request, "payments/pay.html", ctx)
    ctx = _context(request, lot, q=q, chosen=q.charge_ids, amount=raw_amount)
    ctx["base_text"] = format_cents(q.base_cents)
    return render(request, "payments/pay.html", ctx)


@login_required
@require_POST
def pay_checkout(request, lot_id):
    """Create the server-side checkout record, then send the payer to
    Stripe's hosted page."""
    policy.require(request.user, "payments.pay")
    _available_or_404()
    lot = _lot_for_payer(request, lot_id)
    chosen = request.POST.getlist("charge")
    raw_amount = (request.POST.get("amount") or "").strip()
    try:
        amount = parse_cents(raw_amount) if raw_amount else None
        url = flow.start(
            request.user,
            lot,
            method=request.POST.get("method", ""),
            charge_ids=chosen,
            amount_cents=amount,
            request=request,
        )
    except PermissionDenied:
        raise
    except ValueError as exc:
        message = str(exc) if isinstance(exc, flow.PaymentError) else "Enter dollars, e.g. 45.00."
        return render(
            request,
            "payments/pay.html",
            _context(request, lot, error=message, chosen=[int(c) for c in chosen if c.isdigit()]),
        )
    return HttpResponseRedirect(url)


def _own_checkout(request, pk) -> PendingCheckout:
    """A checkout this signed-in person started, on a lot they can still pay
    for. Anything else is 404 (another person's, another lot's, forged)."""
    checkout = get_object_or_404(PendingCheckout.objects.for_user(request.user), pk=pk)
    if checkout.user_id != request.user.pk or checkout.lot_id not in flow.payable_lot_ids(
        request.user
    ):
        raise Http404
    return checkout


@login_required
def pay_return(request, pk):
    """Where Stripe sends the payer back. Only a hint to check now: the
    session id from the address must be the one stored on this person's
    checkout, and the payment is read from Stripe by the server."""
    policy.require(request.user, "payments.pay")
    _available_or_404()
    checkout = _own_checkout(request, pk)
    session_id = request.GET.get("session_id", "")
    if request.GET.get("cancelled"):
        messages.info(request, "Payment cancelled. Nothing was charged.")
        return redirect("my_account")
    if session_id and session_id != checkout.session_id:
        raise Http404
    if checkout.state in (CheckoutState.OPEN, CheckoutState.PROCESSING):
        flow.check(checkout)
        checkout.refresh_from_db()
    payment = StripePayment.objects.for_user(request.user).filter(checkout=checkout).first()
    return render(
        request,
        "payments/return.html",
        {"checkout": checkout, "payment": payment, "test_mode": not checkout.livemode},
    )


@login_required
def payment_receipt(request, pk):
    """Receipt PDF: the payer, people on that account, and finance staff."""
    payment = get_object_or_404(
        StripePayment.objects.for_user(request.user).select_related("checkout", "lot"), pk=pk
    )
    roles = policy.roles_for(request.user)
    if not policy.can(request.user, "finance.view"):
        since = roles.lot_since.get(payment.lot_id)
        if payment.lot_id not in roles.account_lot_ids or (
            since and timezone.localdate(payment.recorded_at) < since
        ):
            raise Http404
    from payments.receipts import receipt_pdf

    record_event(
        "payments.receipt_downloaded",
        actor=request.user,
        request=request,
        target=payment.checkout,
        metadata={"checkout_id": payment.checkout_id},
    )
    return pdf_response(receipt_pdf(payment), f"receipt-{payment.payment_intent_id}.pdf")


# --- Board -------------------------------------------------------------------------------


@login_required
def online_payments(request):
    policy.require(request.user, "finance.view")
    cfg = config()
    alerts = PaymentAlert.objects.for_user(request.user)
    return render(
        request,
        "payments/board.html",
        {
            "config": cfg,
            "payments": StripePayment.objects.for_user(request.user).select_related(
                "lot", "checkout"
            )[:50],
            "processing": PendingCheckout.objects.for_user(request.user)
            .filter(state=CheckoutState.PROCESSING)
            .select_related("lot"),
            "failed": PendingCheckout.objects.for_user(request.user)
            .filter(state__in=[CheckoutState.FAILED, CheckoutState.MISMATCH])
            .select_related("lot")[:20],
            "open_alerts": alerts.filter(resolved_at__isnull=True).select_related("lot"),
            "closed_alerts": alerts.filter(resolved_at__isnull=False).select_related("lot")[:20],
            "payouts": StripePayout.objects.all()[:20],
            "reconciliation": StripeReconciliation.objects.first(),
            "can_manage": policy.can(request.user, "payments.manage"),
            "disclosure": fees.disclosure(),
            "surcharge_bp": fees.surcharge_bp(),
        },
    )


@login_required
@require_POST
def payment_alert_resolve(request, pk):
    policy.require(request.user, "payments.manage")
    alert = get_object_or_404(
        PaymentAlert.objects.for_user(request.user), pk=pk, resolved_at__isnull=True
    )
    note = " ".join((request.POST.get("note") or "").split())[:300]
    if not note:
        messages.error(request, "Say what was done (for example: refund recorded on the account).")
        return redirect("online_payments")
    PaymentAlert.objects.unscoped("resolve").filter(pk=alert.pk).update(
        resolved_at=timezone.now(), resolved_by=request.user, resolution=note
    )
    record_event(
        "payments.alert_resolved",
        actor=request.user,
        request=request,
        target=alert,
        metadata={"alert_id": alert.pk, "kind": alert.kind},
    )
    messages.success(request, "Marked as done.")
    return redirect("online_payments")
