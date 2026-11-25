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
