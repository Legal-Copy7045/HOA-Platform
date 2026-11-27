"""Online payment pages (decision D32-payments). Every page needs sign-in;
there is no webhook or other anonymous endpoint."""

from __future__ import annotations

import functools

from allauth.account.decorators import reauthentication_required
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
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
    AlertKind,
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
def pay_start(request):
    """Where a printed statement's QR code leads (D43): the ways to pay for
    the reader's own home, every open charge included. ``?home=`` is only a
    hint: it is followed when the signed-in login may pay for that home, and
    otherwise ignored, so a scanned statement never opens anyone else's
    account. With several homes and no usable hint, My account lists them."""
    _available_or_404()
    payable = flow.payable_lot_ids(request.user)
    hint = request.GET.get("home", "")
    lot_id = None
    if hint.isascii() and hint.isdigit() and len(hint) <= 9 and int(hint) in payable:
        lot_id = int(hint)
    elif len(payable) == 1:
        lot_id = next(iter(payable))
    if lot_id is None:
        return redirect("my_account")
    return redirect(reverse("pay_online", args=[lot_id]) + "?all=1")


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
            q = flow.quote_all(request.user, lot)
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
        from ledger.accounts import current_account_id

        since = roles.lot_since.get(payment.lot_id)
        if (
            payment.lot_id not in roles.account_lot_ids
            # Only the people of the account it was paid on: a buyer never
            # sees the seller's receipt, whenever it was recorded (SEC-001).
            or payment.entry.account_id != current_account_id(payment.lot_id)
            or (since and timezone.localdate(payment.recorded_at) < since)
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
            "autopay": _autopay_board(request),
        },
    )


def _autopay_board(request) -> dict | None:
    """Automatic payments for the board (payments.manage; D46):
    statuses and failure counts, no card or bank details."""
    from core import features

    if not features.enabled("autopay", request) or not policy.can(request.user, "payments.manage"):
        return None
    from payments import autopay_review
    from payments.autopay import board_summary, review_cents

    return {
        **board_summary(),
        "reviews": autopay_review.board_list(request.user),
        "review_cents": review_cents(),
    }


@login_required
@require_POST
def payment_alert_resolve(request, pk):
    policy.require(request.user, "payments.manage")
    alert = get_object_or_404(
        PaymentAlert.objects.for_user(request.user), pk=pk, resolved_at__isnull=True
    )
    from core import conflicts
    from ledger.services import LedgerError

    try:
        # Own matters (D65): a problem with one's own home's payment is
        # another board member's to close.
        conflicts.refuse(
            request.user,
            alert,
            area="payments",
            action="resolve",
            request=request,
            error=LedgerError,
        )
    except LedgerError as exc:
        messages.error(request, str(exc))
        return redirect("online_payments")
    if request.POST.get("record_refund") and alert.kind == AlertKind.REFUND:
        from payments.services import record_refund

        try:
            record_refund(alert, actor=request.user, request=request)
        except LedgerError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, "Refund recorded on the account.")
        return redirect("online_payments")
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


# --- Automatic payments (payments.autopay, D46) -----------------------------------


def _autopay_lot(request, lot_id) -> Lot:
    """A lot this person may pay for, with automatic payments available
    (feature on, online payments on, Stripe set up); otherwise 404."""
    from payments.autopay import autopay_enabled

    if not autopay_enabled(request):
        raise Http404
    return _lot_for_payer(request, lot_id)


def _autopay_payer(view):
    """Who may use the page is checked before the password check, so another
    home's address is a plain 404 (cross-owner rule)."""

    @functools.wraps(view)
    def wrapper(request, lot_id, *args, **kwargs):
        policy.require(request.user, "payments.pay")
        _autopay_lot(request, lot_id)
        return view(request, lot_id, *args, **kwargs)

    return wrapper


@login_required
@_autopay_payer
@reauthentication_required(enabled=lambda r: r.method == "GET")
def autopay(request, lot_id):
    """Pay assessments automatically: what is set up, and the two ways to set
    it up (bank account, no fee; card, credit cards with the surcharge).
    Opened after a fresh password or passkey check, like other money set-up."""
    from community.rules import rule
    from payments import autopay as auto

    policy.require(request.user, "payments.pay")
    lot = _autopay_lot(request, lot_id)
    info = auto.for_payer(request.user, lot)
    if info is None:
        raise Http404
    monthly = rule("dues.monthly_cents") or 0
    month_now = auto._today().month
    return render(
        request,
        "payments/autopay.html",
        {
            "lot": lot,
            "info": info,
            "monthly_cents": monthly,
            "annual_cents": monthly * auto.ANNUAL_MONTHS,
            "card_surcharge_cents": fees.surcharge(monthly, fees.CREDIT),
            "annual_card_surcharge_cents": fees.surcharge(
                monthly * auto.ANNUAL_MONTHS, fees.CREDIT
            ),
            "months": [(m, auto.month_name(m)) for m in range(1, 13)],
            "default_month": info.get("annual_month") or 1,
            "month_now": month_now,
            "monthly_authorization": auto.authorization_text(auto.MONTHLY, None),
            "annual_authorization": auto.authorization_text(
                auto.ANNUAL, info.get("annual_month") or 1
            ),
            "review_cents": auto.review_cents(),
            "surcharge_bp": fees.surcharge_bp(),
            "surcharge_text": fees.percent_text(fees.surcharge_bp()),
            "disclosure": fees.disclosure(),
            "test_mode": not config().livemode,
        },
    )


@login_required
@_autopay_payer
@reauthentication_required
def autopay_start(request, lot_id):
    """POST: start the set-up on Stripe's hosted page. (A GET, after the
    password check sends someone back here, returns to the page.)"""
    from payments import autopay as auto

    policy.require(request.user, "payments.pay")
    lot = _autopay_lot(request, lot_id)
    if request.method != "POST":
        return redirect("autopay", lot_id=lot.pk)
    try:
        url = auto.start_setup(
            request.user,
            lot,
            method_type=request.POST.get("method", ""),
            schedule=request.POST.get("schedule", auto.MONTHLY),
            annual_month=request.POST.get("annual_month"),
            request=request,
        )
    except auto.AutopayError as exc:
        messages.error(request, str(exc))
        return redirect("autopay", lot_id=lot.pk)
    return HttpResponseRedirect(url)


@login_required
def autopay_return(request, pk):
    """Where Stripe sends the payer back: only a hint to check now. The
    session id must be the one stored on this person's set-up."""
    from community.terms import term_text
    from payments import autopay as auto
    from payments.models import AutopaySetup, SetupState

    policy.require(request.user, "payments.pay")
    if not auto.autopay_enabled(request):
        raise Http404
    setup = get_object_or_404(AutopaySetup.objects.for_user(request.user), pk=pk)
    if setup.user_id != request.user.pk or setup.lot_id not in flow.payable_lot_ids(request.user):
        raise Http404
    session_id = request.GET.get("session_id", "")
    if request.GET.get("cancelled"):
        messages.info(request, "Nothing was set up.")
        return redirect("my_account")
    if session_id and session_id != setup.session_id:
        raise Http404
    state = auto.check_setup(setup)
    if state == SetupState.DONE:
        if setup.schedule == auto.ANNUAL:
            messages.success(
                request,
                term_text(
                    "Automatic payments are set up: the next 12 months' {terms} are paid once "
                    f"a year in {auto.month_name(setup.annual_month)}."
                ),
            )
        else:
            messages.success(
                request,
                term_text(
                    "Automatic payments are set up. Each monthly {term} is paid on its due day."
                ),
            )
    elif state == SetupState.WAITING:
        messages.info(
            request,
            "Your bank account needs to be verified first (Stripe emails you about small test "
            "deposits). Automatic payments start once it is verified.",
        )
    elif state == SetupState.OPEN:
        messages.info(request, "Waiting for Stripe. Refresh My account in a minute.")
    else:
        messages.error(request, "Automatic payments were not set up. Nothing was saved.")
    return redirect("my_account")


@login_required
@require_POST
def autopay_cancel(request, lot_id):
    from payments import autopay as auto

    policy.require(request.user, "payments.pay")
    lot = _autopay_lot(request, lot_id)
    try:
        auto.cancel(request.user, lot, request=request)
    except auto.AutopayError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(
            request, "Automatic payments are off. Nothing more will be charged automatically."
        )
    return redirect("my_account")


@login_required
@_autopay_payer
@reauthentication_required
def autopay_schedule(request, lot_id):
    """POST: the payer switches between monthly and once a year (D47).
    Audited; takes effect from the next charge."""
    from payments import autopay as auto

    policy.require(request.user, "payments.pay")
    lot = _autopay_lot(request, lot_id)
    if request.method != "POST":
        return redirect("autopay", lot_id=lot.pk)
    if not request.POST.get("authorize"):
        messages.error(request, "Tick the box to authorize the new schedule.")
        return redirect("autopay", lot_id=lot.pk)
    try:
        auto.change_schedule(
            request.user,
            lot,
            schedule=request.POST.get("schedule", ""),
            annual_month=request.POST.get("annual_month"),
            request=request,
        )
    except auto.AutopayError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Schedule changed. It takes effect from the next charge.")
    return redirect("autopay", lot_id=lot.pk)


@login_required
@require_POST
def autopay_review(request, pk):
    """The board approves or declines an automatic payment above the review
    limit (D47). Board only (payments.manage), never for a home the board
    member pays for."""
    from core import features
    from payments import autopay_review as review

    policy.require(request.user, "payments.manage")
    if not features.enabled("autopay", request):
        raise Http404
    decision = request.POST.get("decision", "")
    try:
        if decision == "approve":
            review.approve(request.user, pk, request=request)
            messages.success(request, "Approved. It is charged on the next automatic run.")
        elif decision == "decline":
            review.decline(request.user, pk, reason=request.POST.get("reason", ""), request=request)
            messages.success(request, "Declined. Nothing is charged; the owners are emailed.")
        else:
            messages.error(request, "Choose approve or decline.")
    except review.ReviewError as exc:
        messages.error(request, str(exc))
    return redirect(reverse("online_payments") + "#autopay-reviews")
