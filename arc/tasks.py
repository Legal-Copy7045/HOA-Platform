from procrastinate.contrib.django import app

from arc.services import apply_deemed_approvals


@app.periodic(cron="7 4 * * *")
@app.task(queue="maintenance", queueing_lock="arc_deemed_approvals")
def arc_deemed_approvals(timestamp: int) -> int:
    return apply_deemed_approvals()


@app.task(queue="default", retry=1)
def read_signed_form(reading_id: int) -> int:
    """Read an uploaded signed sign-off form with Gemini (arc.signoffs)."""
    from arc.models import ScanReading
    from arc.signoffs import run_reading

    reading = ScanReading.objects.filter(pk=reading_id, state="queued").first()
    return run_reading(reading) if reading else 0
