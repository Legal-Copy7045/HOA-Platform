from django.conf import settings
from django.core.mail import send_mail
from django.urls import reverse
from procrastinate.contrib.django import app

from comms.models import Announcement
from comms.services import recipients_for
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


@app.task(queue="email", retry=0)
def send_urgent_sms(notice_id: int) -> dict:
    """Text an urgent notice to the owners who opted in (once: no retries,
    so nobody gets the same text twice)."""
    from comms.sms import deliver_urgent_notice

    return deliver_urgent_notice(notice_id)
