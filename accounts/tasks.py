from procrastinate.contrib.django import app

from accounts.services import send_invitation_email


@app.task(queue="email", retry=3)
def send_invitation(invitation_id: int) -> bool:
    # Only the ID travels through the job queue; the token is minted here.
    return send_invitation_email(invitation_id)
