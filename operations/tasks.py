"""Operations background jobs."""

from procrastinate.contrib.django import app


@app.periodic(cron="29 8 * * *")
@app.task(queue="email", queueing_lock="compliance_reminders")
def compliance_reminders(timestamp: int) -> int:
    from operations.services import send_compliance_reminders

    return send_compliance_reminders()


# --- Vendor onboarding (D79) -------------------------------------------------------------


@app.task(queue="email", retry=3)
def send_vendor_onboarding_link(link_id: int) -> bool:
    """Only the link's id travels through the queue; the token is minted here."""
    from operations.vendor_onboarding import send_link_email

    return send_link_email(link_id)


@app.task(queue="maintenance")
def rescan_vendor_file(file_id: int) -> str:
    """Scan a vendor document again after the scanner gave no answer."""
    from operations.vendor_onboarding import rescan

    return rescan(file_id)
