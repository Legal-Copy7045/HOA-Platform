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
    "board_seat_recorded": "A board seat was recorded in the portal. If you did not expect "
    "a new board member, check the People page and the audit log now: board members can "
    "approve invoices, resets and sysadmin nominations.",
    "role_granted": "An ARC, auditor or manager role was granted in the portal. Check the "
    "People page if you did not expect it.",
    "board_seat_proposed": "A new board seat was proposed in the portal. A different board "
    "member must approve or decline it on the Approvals page.",
    "account_change_requested": "An owner asked to add or remove someone on their account "
    "or on the deed. "
    "A board member (not the owner) can approve or decline it from My items.",
    "mfa_reset_code_locked": "A sign-in reset code was locked after too many wrong entries. "
    "Someone other than the owner may have been trying it. Contact the owner directly; "
    "start a new reset once you are sure who you are speaking to.",
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


@app.periodic(cron="47 3 * * *")  # daily, 03:47 server time
@app.task(queue="maintenance", queueing_lock="contact_retention")
def contact_retention(timestamp: int) -> int:
    """Clear former owners' contact details under the retention rule."""
    from community import retention
    from core.policy import SYSTEM

    result = retention.run(actor=SYSTEM)
    return result.people if result is not None else 0


@app.periodic(cron="53 3 * * *")  # daily, 03:53 server time
@app.task(queue="maintenance", queueing_lock="register_import_files")
def register_import_files(timestamp: int) -> int:
    """Delete uploaded register files older than REGISTER_IMPORT_FILE_DAYS
    (D54): they hold owners' names and emails. The import records stay."""
    from community import register_import

    return register_import.purge_old_files()
