from django.conf import settings
from django.core.mail import send_mail
from procrastinate.contrib.django import app

from community.services import board_emails

# Deliberately vague: names stay inside the portal, behind sign-in.
NOTICES = {
    "sysadmin_nominated": "Someone has been nominated as a portal sysadmin. A different "
    "board member must approve or decline the nomination in the portal.",
    "sysadmin_granted": "A sysadmin nomination was approved. The new sysadmin now has "
    "operations access to the portal.",
    "sysadmin_revoked": "A portal sysadmin role was ended.",
    "mfa_reset_started": "A board member started a sign-in reset for an account. A "
    "different board member must approve it in the portal.",
    "mfa_reset_approved": "A sign-in reset was approved. The account's authenticators were "
    "removed and all its sessions signed out.",
}


@app.task(queue="email", retry=3)
def notify_board(kind: str) -> int:
    body = (
        f"{NOTICES[kind]}\n\nSign in to {settings.PORTAL_BASE_URL} to see the details "
        f"on the board pages.\n\nIf you did not expect this, check the sysadmin activity "
        f"page and talk to the other board members.\n\n-- \n{settings.SITE_NAME}"
    )
    recipients = board_emails()
    for address in recipients:
        send_mail(f"[{settings.SITE_NAME}] Board notice", body, None, [address])
    return len(recipients)
