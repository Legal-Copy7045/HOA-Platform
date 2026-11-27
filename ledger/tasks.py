import contextlib

from django.utils import timezone
from procrastinate.contrib.django import app

from community.rules import rule
from core.policy import SYSTEM
from ledger.services import LedgerError, post_monthly_assessments


@app.periodic(cron="23 5 1 * *")  # 05:23 on the 1st of each month
@app.task(queue="maintenance", queueing_lock="monthly_assessments")
def monthly_assessments(timestamp: int) -> int:
    """Only if the board turned on dues.auto_post; otherwise they post by button."""
    if not rule("dues.auto_post"):
        return 0
    from ledger import autopost
    from ledger.models import PostingFailure

    today = timezone.localdate()
    period = f"{today:%Y-%m}"
    try:
        return post_monthly_assessments(year=today.year, month=today.month, actor=SYSTEM)
    except LedgerError:
        # Say why without personal data, for the board's alert (D48).
        reason = PostingFailure.ERROR if rule("dues.monthly_cents") else PostingFailure.NO_AMOUNT
        autopost.record_failure(period, reason)
        return 0
    except Exception:
        autopost.record_failure(period, PostingFailure.ERROR)
        raise
    finally:
        autopost.check_and_alert()


@app.periodic(cron="15 12 * * *")  # daily, about 07:15 site time (cron is UTC)
@app.task(queue="maintenance", queueing_lock="monthly_assessments_check")
def monthly_assessments_check(timestamp: int) -> bool:
    """Tell the board (once a month) when automatic posting missed a month (D48)."""
    from ledger import autopost

    return autopost.check_and_alert()


@app.periodic(cron="41 6 * * *")  # daily
@app.task(queue="maintenance", queueing_lock="late_fees")
def late_fees(timestamp: int) -> int:
    """Only when the board switched on late_fee.auto_apply (off by default)."""
    from ledger.services import post_late_fees

    if not rule("late_fee.auto_apply"):
        return 0
    today = timezone.localdate()
    try:
        return post_late_fees(year=today.year, month=today.month, actor=SYSTEM)
    except LedgerError:
        return 0


@app.periodic(cron="17 9 * * *")  # daily; does nothing unless the board set the day
@app.task(queue="maintenance", queueing_lock="courtesy_reminders")
def courtesy_reminders(timestamp: int) -> int:
    from ledger.accounting import send_courtesy_reminders

    return send_courtesy_reminders()


@app.periodic(cron="53 10 * * *")  # daily; only with the "bank_feeds" feature on
@app.task(queue="maintenance", queueing_lock="bank_feed_sync")
def bank_feed_sync(timestamp: int) -> int:
    from ledger.bankfeeds.services import sync_all

    return sync_all()


@app.task(queue="email")
def send_statement_emails(year: int, month: int) -> int:
    """Queued when the month's charge is posted (D42); each account's
    statement goes out at most once a month, so re-runs are harmless."""
    from ledger.statement_email import send_monthly_statements

    return send_monthly_statements(year=year, month=month)["emails"]


@app.periodic(cron="37 11 * * *")  # daily, about 06:37 site time
@app.task(queue="maintenance", queueing_lock="books_auto_close")
def books_auto_close(timestamp: int) -> int:
    """Code new bank lines by the rules, confirm unambiguous matches, and
    close every due month (and fiscal year) whose checks pass (D70, D71)."""
    from ledger.books.close import run

    return len(run()["closed"])


@app.periodic(cron="47 7 * * *")  # daily; only with "overdue_interest" on (D80)
@app.task(queue="maintenance", queueing_lock="overdue_interest")
def overdue_interest(timestamp: int) -> int:
    """On or after the posting day, post the month's interest (once per account)."""
    from ledger.interest import run_monthly

    return run_monthly()


# --- Finance gaps (D75, D76) --------------------------------------------------------------


@app.periodic(cron="29 10 * * *")  # daily, about 05:29 site time
@app.task(queue="maintenance", queueing_lock="special_assessments")
def special_assessments(timestamp: int) -> int:
    """Post special assessment installments due today (or missed earlier);
    once per home and installment, so re-runs are harmless (D75). Late fees
    on them only when late fees post by themselves."""
    from ledger import special_assessments as sa

    posted = sa.post_due()
    if rule("late_fee.auto_apply"):
        with contextlib.suppress(sa.SpecialAssessmentError):
            posted += sa.post_late_fees(actor=SYSTEM)
    return posted


@app.periodic(cron="37 11 * * *")  # daily, about 06:37 site time
@app.task(queue="maintenance", queueing_lock="payment_plans")
def payment_plans(timestamp: int) -> dict:
    """Complete paid payment plans; default plans past their default date (D76)."""
    from ledger.payment_plans import check_plans

    return check_plans()
