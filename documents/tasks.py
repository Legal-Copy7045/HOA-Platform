"""Malware scans the worker retries (D57, documents/scan_queue.py)."""

from procrastinate.contrib.django import app


@app.task(queue="maintenance")
def scan_file(kind: str, pk: int) -> str:
    """Scan one waiting file again (queued after an upload the scanner could
    not answer, or by System status "Scan waiting files now")."""
    from documents import scan_queue

    return scan_queue.rescan(kind, pk) or "not waiting"


@app.periodic(cron="*/10 * * * *")
@app.task(queue="maintenance", queueing_lock="scan_sweep")
def scan_sweep(timestamp: int) -> int:
    """Every 10 minutes: once the scanner answers, scan every file still waiting."""
    from documents import scan_queue

    return scan_queue.sweep()


# Renewals and records retention (D72).


@app.periodic(cron="13 7 * * *")  # daily, 07:13 server time
@app.task(queue="email", queueing_lock="renewal_reminders")
def renewal_reminders(timestamp: int) -> int:
    """Email the board about renewals that crossed a reminder threshold."""
    from core.features import enabled
    from documents import renewals

    if not enabled("renewals"):
        return 0
    return renewals.send_reminders()


@app.periodic(cron="23 3 * * *")  # nightly, 03:23 server time
@app.task(queue="maintenance", queueing_lock="records_retention_scan")
def records_retention_scan(timestamp: int) -> int:
    """List files past their retention period (never removes one by itself)."""
    from core.features import enabled
    from documents import retention

    if not enabled("records_retention"):
        return 0
    return retention.scan().due
