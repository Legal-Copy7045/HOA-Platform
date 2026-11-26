"""Stripe polling (decision D32-payments: pull, never push - no webhook)."""

from procrastinate.contrib.django import app


@app.periodic(cron="*/15 * * * *")
@app.task(queue="maintenance", queueing_lock="payments_poll_open")
def poll_open_checkouts(timestamp: int) -> int:
    """Checkouts started in the last 24 hours."""
    from payments.sync import poll_open_checkouts as run

    return run()


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
