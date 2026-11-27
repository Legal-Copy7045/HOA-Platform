"""Core background jobs: the worker's heartbeat and the health alerts."""

from django.utils import timezone
from procrastinate.contrib.django import app


@app.periodic(cron="*/5 * * * *")
@app.task(queue="maintenance", queueing_lock="worker_heartbeat")
def worker_heartbeat(timestamp: int) -> None:
    from core.models import Heartbeat

    Heartbeat.objects.update_or_create(name="worker", defaults={"at": timezone.now()})


@app.periodic(cron="*/10 * * * *")
@app.task(queue="maintenance", queueing_lock="health_alerts")
def health_alerts(timestamp: int) -> int:
    """Alerts from inside the worker. (If the worker itself stops, the VM's
    hoa-health timer, which runs health_check --alert in the web container,
    notices the stale heartbeat.)"""
    from core.health import alert, run_checks

    return alert(run_checks())


@app.periodic(cron="23 3 * * *")  # daily
@app.task(queue="maintenance", queueing_lock="purge_server_errors")
def purge_server_errors(timestamp: int) -> int:
    """Server error counts and signatures are kept RETENTION_DAYS (D45)."""
    from core.errors import purge

    return purge()


@app.periodic(cron="37 3 * * *")  # daily
@app.task(queue="maintenance", queueing_lock="remove_old_jobs", pass_context=True)
async def remove_old_jobs(context, timestamp: int) -> None:
    """Finished background jobs are kept two weeks, then removed, so the job
    tables do not grow forever (optimization review A7)."""
    from procrastinate import builtin_tasks

    await builtin_tasks.remove_old_jobs(
        context,
        max_hours=24 * 14,
        remove_failed=True,
        remove_cancelled=True,
        remove_aborted=True,
    )


@app.periodic(cron="53 2 * * *")  # daily
@app.task(queue="maintenance", queueing_lock="growth_snapshot")
def growth_snapshot(timestamp: int) -> bool:
    """Database and disk sizes for the 90-day trend (D74); old hourly
    tallies are removed at the same time."""
    from core import growth, ops_timeline

    ops_timeline.purge_tallies()
    return growth.snapshot() is not None
