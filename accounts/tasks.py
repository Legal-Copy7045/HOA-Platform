from procrastinate.contrib.django import app

from accounts.services import send_invitation_email


@app.task(queue="email", retry=3)
def send_invitation(invitation_id: int) -> bool:
    # Only the ID travels through the job queue; the token is minted here.
    return send_invitation_email(invitation_id)


@app.task(queue="email", retry=0)
def send_recovery_link(user_id: int) -> str:
    """Account recovery (D58): only a login id (0 for an unknown address)
    travels through the queue; eligibility is decided here, so the page that
    asked answers the same way whatever the address."""
    from accounts.recovery import send_link

    return send_link(user_id)
