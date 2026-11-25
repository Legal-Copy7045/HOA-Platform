"""Operations background jobs."""

from procrastinate.contrib.django import app


@app.periodic(cron="29 8 * * *")
@app.task(queue="email", queueing_lock="compliance_reminders")
def compliance_reminders(timestamp: int) -> int:
    from operations.services import send_compliance_reminders

    return send_compliance_reminders()
