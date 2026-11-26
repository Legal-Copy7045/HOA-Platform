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
