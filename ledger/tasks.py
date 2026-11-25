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
