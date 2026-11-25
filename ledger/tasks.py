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
    today = timezone.localdate()
    try:
        return post_monthly_assessments(year=today.year, month=today.month, actor=SYSTEM)
    except LedgerError:
        return 0


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
