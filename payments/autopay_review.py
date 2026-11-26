"""Board review of large automatic payments (decision D47).

An automatic charge (monthly or once a year, surcharge included) above rule
``payments.autopay_review_cents`` is not sent to Stripe. ``hold`` records an
append-only ``AutopayReview`` for the account and period (once), the board
sees "Approve automatic payment of $X for <address>" on My items and the
Online payments page, and the payer sees that it is waiting.

* **Approve** (``payments.manage``: board only, never the manager): the next
  autopay run charges it, through the same attempt rows and idempotency keys
  as on the due day, so it is charged once.
* **Decline** (with a reason): nothing is charged and the payers are emailed
  with a link to My account. For the annual schedule, monthly charges do not
  start by themselves.
* **Expiry**: not decided within 14 days, nothing is charged and the payers
  are told.

Nobody decides a payment for a home they pay for (an owner on the deed, an
authorized or view-only contact of the account, or the person who set it
up), even a board member. Decisions are append-only
(``AutopayReviewDecision``, one per review) and audited with ids, amounts
and codes only.
"""

from __future__ import annotations

import datetime

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone

from audit.services import record_event
from core import policy
from core.money import format_cents
from core.scoping import internal, system_access
from payments.models import (
    AutopayAttempt,
    AutopayReview,
    AutopayReviewDecision,
    AutopaySchedule,
    ReviewOutcome,
    twelve_months_start,
)

REVIEW_DAYS = 14
APPROVED_CHARGE_DAYS = 30  # an approval not charged within this long is left alone
WAITING = "waiting"
APPROVED, DECLINED, EXPIRED, CLOSED = (
    ReviewOutcome.APPROVED.value,
    ReviewOutcome.DECLINED.value,
    ReviewOutcome.EXPIRED.value,
    ReviewOutcome.CLOSED.value,
)


class ReviewError(ValueError):
    pass


def _today_text(day: datetime.date) -> str:
    return f"{day:%B} {day.day}, {day.year}"


def what_text(review: AutopayReview) -> str:
    """ "the March 2027 assessment" or "the 12 months' assessments from
    January 2027"."""
    from community.terms import term_text

    if review.schedule == AutopaySchedule.ANNUAL:
        start = twelve_months_start(review)
        return term_text(f"the 12 months' {{terms}} from {start:%B %Y}")
    year, month = (int(x) for x in review.period.split("-"))
    return term_text(f"the {datetime.date(year, month, 1):%B %Y} {{term}}")


@internal("autopay review status")
def status(review: AutopayReview | None) -> str:
    if review is None:
        return ""
    decision = AutopayReviewDecision.objects.filter(review=review).first()
    return decision.outcome if decision else WAITING


@internal("autopay review hold")
def hold(
    account,
    *,
    schedule: str,
    period: str,
    covers_from: str,
    covers_through: str = "",
    method: str,
    base: int,
    surcharge: int,
    limit: int,
) -> AutopayReview:
    """The review of this account's charge for ``period``: the existing one,
    or a new one waiting for the board (audited)."""
    existing = AutopayReview.objects.filter(account=account, period=period).first()
    if existing is not None:
        return existing
    try:
        with transaction.atomic():
            review = AutopayReview.objects.create(
                lot_id=account.lot_id,
                account=account,
                schedule=schedule,
                period=period,
                covers_from=covers_from if schedule == AutopaySchedule.ANNUAL else "",
                covers_through=covers_through if schedule == AutopaySchedule.ANNUAL else "",
                method=method,
                base_cents=base,
                surcharge_cents=surcharge,
                total_cents=base + surcharge,
                limit_cents=limit,
                expires_at=timezone.now() + datetime.timedelta(days=REVIEW_DAYS),
            )
    except IntegrityError:
        return AutopayReview.objects.get(account=account, period=period)
    record_event(
        "payments.autopay_review_requested",
        actor=None,
        target=account,
        metadata={
            "lot_id": account.lot_id,
            "account_id": account.pk,
            "review_id": review.pk,
            "schedule": schedule,
            "period": period,
            "amount_cents": review.total_cents,
        },
    )
    return review


@internal("autopay review waiting")
def waiting_for(account_id) -> AutopayReview | None:
    """The account's charge waiting for the board, if any."""
    return (
        AutopayReview.objects.filter(account_id=account_id, decision__isnull=True)
        .order_by("-created_at", "-id")
        .first()
    )


@internal("autopay review approved")
def approved_uncharged(now=None) -> list[AutopayReview]:
    """Approved reviews whose charge has not been made yet."""
    now = now or timezone.now()
    out = []
    for review in AutopayReview.objects.filter(
        decision__outcome=APPROVED,
        decision__created_at__gte=now - datetime.timedelta(days=APPROVED_CHARGE_DAYS),
    ).select_related("account__lot"):
        if not AutopayAttempt.objects.filter(
            account_id=review.account_id, period=review.period, try_number=1
        ).exists():
            out.append(review)
    return out


@internal("autopay review payers")
def pays_for(user, review: AutopayReview) -> bool:
    """Whether this login pays for (or watches the money of) the home: an
    owner on the deed, a contact of the account, or the person who set the
    automatic payments up. Such a person never decides the review."""
    from community.models import AccountContact, Ownership, Person
    from payments.autopay import state

    person_ids = list(Person.objects.filter(user=user).values_list("pk", flat=True))
    if Ownership.objects.active().filter(lot_id=review.lot_id, person_id__in=person_ids).exists():
        return True
    if (
        AccountContact.objects.active()
        .filter(account_id=review.account_id, person_id__in=person_ids)
        .exists()
    ):
        return True
    st = state(review.account_id)
    return bool(st.method and st.method.user_id == getattr(user, "pk", None))


def _get(user, pk) -> AutopayReview:
    from django.http import Http404

    review = AutopayReview.objects.for_user(user).filter(pk=pk).select_related("lot").first()
    if review is None:
        raise Http404
    return review


def _decide(review: AutopayReview, outcome: str, *, user=None, reason: str = ""):
    try:
        with transaction.atomic():
            return AutopayReviewDecision.objects.create(
                review=review, outcome=outcome, decided_by=user, reason=reason[:300]
            )
    except IntegrityError as exc:
        raise ReviewError("This payment was already decided.") from exc


def _audit(action: str, review: AutopayReview, *, user=None, request=None, code: str = ""):
    metadata = {
        "lot_id": review.lot_id,
        "account_id": review.account_id,
        "review_id": review.pk,
        "amount_cents": review.total_cents,
    }
    if code:
        metadata["code"] = code
    record_event(action, actor=user, request=request, target=review, metadata=metadata)


def _check_decider(user, review: AutopayReview) -> None:
    policy.require(user, "payments.manage")
    if pays_for(user, review):
        raise PermissionDenied("You pay for this home, so another board member decides.")
    if status(review) != WAITING:
        raise ReviewError("This payment was already decided.")
    if timezone.now() >= review.expires_at:
        raise ReviewError("This review has expired. Nothing will be charged.")


def approve(user, pk, *, request=None) -> AutopayReviewDecision:
    """A board member approves: the next autopay run charges it, once."""
    review = _get(user, pk)
    _check_decider(user, review)
    with system_access("autopay review decide"):
        decision = _decide(review, APPROVED, user=user)
    _audit("payments.autopay_review_approved", review, user=user, request=request)
    return decision


def decline(user, pk, *, reason: str, request=None) -> AutopayReviewDecision:
    """A board member declines (with a reason): nothing is charged and the
    payers are emailed."""
    reason = " ".join((reason or "").split())[:300]
    review = _get(user, pk)
    _check_decider(user, review)
    if not reason:
        raise ReviewError("Say why, for the owners (they are emailed).")
    with system_access("autopay review decide"), transaction.atomic():
        decision = _decide(review, DECLINED, user=user, reason=reason)
        _email(review, declined_reason=reason)
    _audit("payments.autopay_review_declined", review, user=user, request=request)
    return decision


@internal("autopay review expiry")
def expire_due(now=None) -> int:
    """Reviews nobody decided within 14 days: nothing is charged and the
    payers are told."""
    now = now or timezone.now()
    count = 0
    for review in AutopayReview.objects.filter(
        decision__isnull=True, expires_at__lte=now
    ).select_related("lot"):
        try:
            with transaction.atomic():
                _decide(review, EXPIRED, reason="Not decided within 14 days.")
                _email(review)
        except ReviewError:
            continue
        _audit("payments.autopay_review_expired", review, code="not_decided")
        count += 1
    return count


@internal("autopay review close")
def close_waiting(account, *, code: str) -> int:
    """The payer changed the schedule (or the month): a yearly payment
    waiting under the old one is closed, nothing charged. A month's charge
    waiting for review stays: months are charged monthly under either
    schedule unless a yearly payment holds them."""
    count = 0
    for review in AutopayReview.objects.filter(
        account=account, decision__isnull=True, schedule=AutopaySchedule.ANNUAL
    ):
        try:
            _decide(review, CLOSED, reason="The payer changed the schedule.")
        except ReviewError:
            continue
        _audit("payments.autopay_review_expired", review, code=code)
        count += 1
    return count


def _email(review: AutopayReview, *, declined_reason: str = "") -> None:
    from comms.notify import send_now
    from payments.autopay import payer_emails

    lot = review.lot
    amount = format_cents(review.total_cents)
    if declined_reason:
        subject = f"[{settings.SITE_NAME}] Your automatic payment was not approved"
        lines = [
            f"The board did not approve the automatic payment of {amount} for {lot.label} "
            f"({what_text(review)}). Nothing was charged.",
            "",
            f"The board's reason: {declined_reason}",
        ]
    else:
        subject = f"[{settings.SITE_NAME}] Your automatic payment was not charged"
        lines = [
            f"The automatic payment of {amount} for {lot.label} ({what_text(review)}) "
            f"waited for the board's review and was not decided within {REVIEW_DAYS} days, "
            "so nothing was charged.",
        ]
    if review.schedule == AutopaySchedule.ANNUAL:
        lines += [
            "",
            "Automatic payments stay set up once a year; monthly payments do not start by "
            "themselves. You can change the schedule on the automatic payments page.",
        ]
    lines += [
        "",
        "Please pay from My account (sign in first):",
        settings.PORTAL_BASE_URL + reverse("my_account"),
        "",
        f"-- \n{settings.SITE_NAME}",
    ]
    send_now(
        payer_emails(review.lot_id, account_id=review.account_id),
        subject=subject,
        body="\n".join(lines),
    )


def _item_text(review: AutopayReview) -> str:
    return f"Approve automatic payment of {format_cents(review.total_cents)} for {review.lot.label}"


@internal("autopay review board")
def board_items(user) -> list:
    """My items for the board: automatic payments waiting for review, except
    for a home the board member pays for (another member decides those)."""
    from core.my_items import Item

    now = timezone.now()
    out = []
    for review in AutopayReview.objects.filter(
        decision__isnull=True, expires_at__gt=now
    ).select_related("lot"):
        if pays_for(user, review):
            continue
        out.append(
            Item(
                "Finance",
                _item_text(review),
                f"{what_text(review).capitalize()}: above the "
                f"{format_cents(review.limit_cents)} review limit. Approve or decline by "
                f"{_today_text(timezone.localdate(review.expires_at))}.",
                reverse("online_payments") + "#autopay-reviews",
                "warning",
                timezone.localdate(review.expires_at),
                nav="online_payments",
            )
        )
    return out


@internal("autopay review list")
def board_list(user, limit: int = 20) -> dict:
    """For the Online payments page: waiting reviews (with whether this
    board member may decide) and recent decisions."""
    now = timezone.now()
    waiting = []
    for review in AutopayReview.objects.filter(
        decision__isnull=True, expires_at__gt=now
    ).select_related("lot"):
        waiting.append(
            {
                "review": review,
                "title": _item_text(review),
                "what": what_text(review),
                "own": pays_for(user, review),
            }
        )
    decided = list(
        AutopayReviewDecision.objects.select_related("review__lot", "decided_by").order_by(
            "-created_at", "-id"
        )[:limit]
    )
    return {"waiting": waiting, "decided": decided}
