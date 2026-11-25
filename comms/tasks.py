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
