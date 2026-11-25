from procrastinate.contrib.django import app

from arc.services import apply_deemed_approvals


@app.periodic(cron="7 4 * * *")
@app.task(queue="maintenance", queueing_lock="arc_deemed_approvals")
def arc_deemed_approvals(timestamp: int) -> int:
    return apply_deemed_approvals()
