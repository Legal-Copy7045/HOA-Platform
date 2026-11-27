"""The safety net for automatic monthly posting (rule ``dues.auto_post``, D48).

When automatic posting is on and this month's charge is not on every home
after the 1st's run, the board is told: once a month by email (sent at once,
never in the weekly digest, with a link to the signed-in page only), as a
My items entry until the month is posted, and as a health check so the
sysadmins get the usual health email and System status shows it. Everything
clears itself once the month is posted. Nothing here holds personal data.
"""

from __future__ import annotations

import datetime

from django.conf import settings
from django.urls import reverse
from django.utils import timezone

from community.rules import rule
from community.terms import charge_term, term_text

POST_ANCHOR = "#post-monthly"


def _now(now=None) -> datetime.datetime:
    return timezone.localtime(now or timezone.now())


def record_failure(period: str, reason: str, now=None) -> None:
    """The monthly task could not post: remember why (a reason code only)."""
    from ledger.models import AssessmentPostingCheck

    AssessmentPostingCheck.objects.update_or_create(
        period=period, defaults={"reason": reason, "failed_at": now or timezone.now()}
    )


def missed(now=None) -> dict | None:
    """This month's automatic posting, if it looks missed; else None."""
    if not rule("dues.auto_post"):
        return None
    from ledger import services
    from ledger.models import AssessmentPostingCheck, PostingFailure

    status = services.auto_post_status(now)
    row = AssessmentPostingCheck.objects.filter(period=status["period"]).first()
    # A recorded failure counts at once; otherwise only after the 1st's run.
    failed = bool(row and row.failed_at and not status["complete"])
    if not (status["missed"] or failed):
        return None
    reason = row.reason if row else PostingFailure.NOT_RUN
    if reason == PostingFailure.NOT_RUN and not rule("dues.monthly_cents"):
        reason = PostingFailure.NO_AMOUNT
    term = charge_term()
    return {
        **status,
        "reason": reason,
        "reason_text": PostingFailure(reason).label,
        "title": term_text(f"{status['month']:%B} {{term}} wasn't posted automatically", term),
    }


def health():
    """core.health check "Monthly assessment posting": only when it is on."""
    if not rule("dues.auto_post"):
        return None
    m = missed()
    if m:
        return False, f"{m['title']}: {m['reason_text'].lower()}"
    from ledger import services

    status = services.auto_post_status()
    if status["homes"]:
        return True, f"{status['month']:%B} posted to {status['homes']} homes"
    return True, f"Next run {status['next']:%b %-d}"


def board_emails() -> list[str]:
    """Board members who can post the charge. The code has no narrower
    finance group than the board (officers are offices, not permissions)."""
    from accounts.models import User
    from core import policy

    out = []
    for user in User.objects.filter(is_active=True):
        groups = policy.roles_for(user).groups
        if policy.BOARD in groups and policy.can(user, "ledger.manage"):
            out.append(user.email)
    return out


def check_and_alert(now=None) -> bool:
    """Run daily and after the 1st's posting: email the board once a month
    while the month is missed. Returns True when an email went out."""
    from comms.notify import send_now
    from ledger.models import AssessmentPostingCheck

    m = missed(now)
    if m is None:
        return False
    row, _ = AssessmentPostingCheck.objects.get_or_create(period=m["period"])
    if row.emailed_at is not None:
        return False
    link = settings.PORTAL_BASE_URL + reverse("ledger_home") + POST_ANCHOR
    term = charge_term()
    body = term_text(
        f"The monthly {{term}} for {m['month']:%B %Y} should have been posted automatically "
        f"on the 1st, but it isn't on every home yet. Reason: {m['reason_text'].lower()}.\n\n"
        f"Post it now on Finance, Owner accounts (sign in first):\n{link}\n\n"
        "Homes already charged for the month are skipped, so posting is safe to repeat. "
        "This email is sent once a month; the item stays in My items until the month is posted.",
        term,
    )
    send_now(board_emails(), subject=m["title"], body=body, board=True)
    row.emailed_at = now or timezone.now()
    row.save(update_fields=["emailed_at"])
    return True
