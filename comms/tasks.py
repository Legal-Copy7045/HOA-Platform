from django.conf import settings
from django.core.mail import send_mail
from django.urls import reverse
from procrastinate import RetryStrategy
from procrastinate.contrib.django import app

from comms.models import Announcement
from comms.services import recipients_for
from core.email import EmailUnavailable
from core.text import single_line


@app.task(queue="email", retry=3)
def email_announcement(announcement_id: int) -> int:
    announcement = Announcement.objects.get(pk=announcement_id)
    link = settings.PORTAL_BASE_URL + reverse("announcement_detail", args=[announcement.pk])
    prefix = "URGENT: " if announcement.urgent else ""
    body = (
        f"A new announcement was posted on the {settings.SITE_NAME} portal:\n\n"
        f"  {announcement.title}\n\nRead it here (sign-in required):\n{link}\n\n-- \n"
        f"{settings.SITE_NAME}"
    )
    sent = 0
    for address in recipients_for(announcement.audience):
        send_mail(
            f"[{settings.SITE_NAME}] {prefix}{single_line(announcement.title)}",
            body,
            None,
            [address],
        )
        sent += 1
    Announcement.objects.filter(pk=announcement.pk).update(emailed_count=sent)
    return sent


@app.periodic(cron="13 7 * * 1")  # Monday mornings
@app.task(queue="email", queueing_lock="weekly_digest")
def weekly_digest(timestamp: int) -> int:
    from comms.notify import send_digests

    return send_digests()


# Its own queue, and one push job at a time (lock), so slow or unreachable push
# services never hold up email: the worker runs more than one job at once
# (docker/entrypoint.sh) and push can take only one of those slots (R2-007).
@app.task(queue="push", lock="push", retry=2)
def send_push(user_ids: list[int], title: str, path: str) -> dict:
    """Push a title and a portal path to these logins' devices."""
    from comms.push import deliver

    return deliver(user_ids, title=title, path=path)


@app.periodic(cron="37 * * * *")  # hourly: events starting within the next day
@app.task(queue="email", queueing_lock="event_reminders")
def event_reminders(timestamp: int) -> int:
    """Remind people going to an event the day before (events that asked
    for it). Each event is reminded once."""
    from comms.rsvp import send_reminders

    return send_reminders()


@app.task(queue="email", retry=0)
def send_urgent_sms(notice_id: int) -> dict:
    """Text an urgent notice to the owners who opted in (once: no retries,
    so nobody gets the same text twice)."""
    from comms.sms import deliver_urgent_notice

    return deliver_urgent_notice(notice_id)


@app.periodic(cron="41 */4 * * *")  # every 4 hours; only with "usps_tracking" on and set up
@app.task(queue="maintenance", queueing_lock="usps_tracking")
def usps_tracking(timestamp: int) -> dict:
    """Read new USPS scans of barcoded paper mail (D60): pull only."""
    from comms.usps_tracking import poll

    return poll()


@app.periodic(cron="7,27,47 * * * *")  # every 20 minutes; only with "bounce_tracking" on and set up
@app.task(queue="maintenance", queueing_lock="poll_bounces")
def poll_bounces(timestamp: int) -> dict:
    """Read bounced email from the bounce mailbox (D74): pull only, IMAP over TLS."""
    from comms import bounces

    if not bounces.active():
        return {"state": "off"}
    return bounces.poll()


@app.periodic(cron="2-59/5 * * * *")  # every 5 minutes; only with EMAIL_PROVIDER=smtp2go and a key
@app.task(queue="maintenance", queueing_lock="poll_email_activity")
def poll_email_activity(timestamp: int) -> dict:
    """Read SMTP2Go's delivery, bounce, spam-complaint and unsubscribe
    reports since the last cursor (0.43.0): pull only, no webhook."""
    from comms import delivery

    if not delivery.active():
        return {"state": "off"}
    return delivery.pull()


# Sending in the worker: tried again after about 25 s, 2 min, 10 min and
# 52 min when the relay could not be reached or refused for now.
EMAIL_RETRY = RetryStrategy(max_attempts=5, exponential_wait=5, retry_exceptions={EmailUnavailable})


@app.task(queue="email", retry=EMAIL_RETRY)
def send_arc_decision_email(decision_id: int) -> int:
    """Tell the applicant an ARC decision is ready (once per address: a
    retry never emails an address that already got it)."""
    from arc.notify import send_decision_email

    return send_decision_email(decision_id)
