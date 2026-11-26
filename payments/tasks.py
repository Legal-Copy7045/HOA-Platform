"""Stripe polling (decision D32-payments: pull, never push - no webhook)."""

from procrastinate.contrib.django import app


def _autopay_on() -> bool:
    from payments.autopay import autopay_enabled

    return autopay_enabled()


@app.periodic(cron="*/15 * * * *")
@app.task(queue="maintenance", queueing_lock="payments_poll_open")
def poll_open_checkouts(timestamp: int) -> int:
    """Checkouts started in the last 24 hours."""
    from payments.autopay import poll_setups
    from payments.sync import poll_open_checkouts as run

    count = run()
    if _autopay_on():
        count += poll_setups()
    return count


@app.periodic(cron="37 2 * * *")  # daily
@app.task(queue="maintenance", queueing_lock="payments_daily")
def daily_sync(timestamp: int) -> int:
    """Bank payments still processing; refunds, disputes and returns on
    recorded payments; Stripe balance and payouts; then the reconciliation."""
    from payments import sync

    count = sync.poll_processing() + sync.poll_recorded()
    sync.sync_balance()
    sync.reconcile()
    return count


@app.periodic(cron="13 * * * *")  # hourly; charges only on the dues due day
@app.task(queue="maintenance", queueing_lock="payments_autopay")
def autopay(timestamp: int) -> int:
    """Automatic payments (D40-autopay): bank payments processing, unanswered
    charges, this month's assessments on the due day, and the one retry. Does
    nothing unless the "autopay" and "online_payments" features are on and
    Stripe is set up."""
    from payments.autopay import run

    return run()
