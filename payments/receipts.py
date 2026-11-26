"""Receipts for online payments: an email to the person who paid, and a PDF
they can download (core.pdf). Both list the charges paid, the surcharge if
any, and the Stripe reference."""

from __future__ import annotations

from django.conf import settings
from django.urls import reverse
from django.utils import timezone

from core import pdf
from core.money import format_cents
from core.scoping import internal


@internal("receipt")
def lines(payment) -> list[tuple[str, int]]:
    """(what was paid, cents) for each charge the payment was applied to,
    plus any credit left on the account."""
    from ledger import allocation
    from ledger.models import LedgerEntry

    out = []
    applied = allocation.applications_of(payment.entry_id)
    entries = {e.pk: e for e in LedgerEntry.objects.filter(pk__in=[c for c, _ in applied])}
    numbers = {
        c.entry.pk: c
        for c in allocation.charges(payment.lot_id, open_only=False)
        if c.entry.pk in entries
    }
    for charge_id, cents in applied:
        view = numbers.get(charge_id)
        name = f"{view.label}: {view.title}" if view else entries[charge_id].description
        out.append((name, cents))
    left = payment.base_cents - sum(c for _, c in applied)
    if left > 0:
        out.append(("Credit on the account for future charges", left))
    return out


def _method_text(payment) -> str:
    return {"bank": "Bank account", "debit": "Debit card", "credit": "Credit card"}.get(
        payment.method, payment.method
    )


@internal("receipt")
def receipt_pdf(payment) -> bytes:
    from community.models import CommunityProfile

    profile = CommunityProfile.get()
    lot = payment.lot
    paras = [
        ("heading", profile.legal_name),
        ("normal", ""),
        ("heading", "PAYMENT RECEIPT"),
        ("normal", f"Lot {lot.number}, {lot.street_address}"),
        ("normal", f"Paid on {timezone.localtime(payment.recorded_at):%B %-d, %Y}"),
        ("normal", f"Paid by: {_method_text(payment)} (online, through Stripe)"),
        ("normal", f"Stripe reference: {payment.payment_intent_id}"),
        ("normal", ""),
        ("mono", f"{'PAID':<60}{'AMOUNT':>16}"),
    ]
    for name, cents in lines(payment):
        paras.append(
            ("mono", f"{name.replace(chr(0x2013), '-')[:58]:<60}{format_cents(cents):>16}")
        )
    if payment.surcharge_cents:
        paras.append(
            ("mono", f"{'Credit card surcharge':<60}{format_cents(payment.surcharge_cents):>16}")
        )
    paras += [
        ("mono", f"{'TOTAL CHARGED':<60}{format_cents(payment.total_cents):>16}"),
        ("normal", ""),
        (
            "small",
            "The surcharge, if any, is a fee for paying by credit card. It is not part of the "
            "account balance.",
        ),
    ]
    return pdf.render(paras, title=f"Receipt {payment.payment_intent_id}")


@internal("receipt")
def email_receipt(payment) -> None:
    from comms.notify import send_now

    user = payment.checkout.user
    body = [
        f"Thank you. Your online payment for lot {payment.lot.number} was received.",
        "",
    ]
    body += [f"  {name}: {format_cents(cents)}" for name, cents in lines(payment)]
    if payment.surcharge_cents:
        body.append(f"  Credit card surcharge: {format_cents(payment.surcharge_cents)}")
    body += [
        f"  Total charged: {format_cents(payment.total_cents)}",
        "",
        f"Stripe reference: {payment.payment_intent_id}",
        "",
        "Your receipt and account:",
        settings.PORTAL_BASE_URL + reverse("my_account"),
        "",
        f"-- \n{settings.SITE_NAME}",
    ]
    send_now(
        [user.email],
        subject=f"[{settings.SITE_NAME}] Payment received",
        body="\n".join(body),
    )
