"""Email the sysadmins a host-security alert (Falco, file integrity, audit).

Run by the VM's hardening timers (infra/poc/harden-host.sh), with the alert
text on stdin:

    printf '%s' "$SUMMARY" | python manage.py host_alert --kind falco

The text describes the host (paths, container and process names), never owner
data. It is capped and stripped of control characters, and each alert is
audited (counts only). It exits non-zero when the alert reached nobody (no
sysadmin email, or every send failed), so the timers keep the report and
send it again on their next run instead of accepting the change.
"""

import logging
import re
import sys

from django.conf import settings
from django.core.mail import send_mail
from django.core.management.base import BaseCommand, CommandError

from audit.models import Channel
from audit.services import record_event
from core.health import sysadmin_emails

KINDS = {
    "falco": "Container behavior (Falco)",
    "integrity": "System files changed (file integrity check)",
    "audit": "Sensitive files touched (audit log)",
    "updates": "Security updates",
}
MAX_CHARS = 8000
logger = logging.getLogger(__name__)
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


class Command(BaseCommand):
    help = "Email the sysadmins a host-security alert read from stdin."

    def add_arguments(self, parser):
        parser.add_argument("--kind", required=True, choices=sorted(KINDS))

    def handle(self, *args, kind, **opts):
        text = _CONTROL.sub("", sys.stdin.read())[:MAX_CHARS].strip()
        if not text:
            raise CommandError("Nothing to send (empty alert).")
        lines = text.count("\n") + 1
        recipients = sysadmin_emails()
        body = (
            f"{KINDS[kind]} on the portal server:\n\n{text}\n\n"
            "If you did not expect this, see docs/runbook.md (Host security).\n\n"
            f"-- \n{settings.SITE_NAME}"
        )
        sent = 0
        for address in recipients:
            try:
                sent += send_mail(
                    f"[{settings.SITE_NAME}] Host security: {KINDS[kind]}",
                    body,
                    None,
                    [address],
                )
            except Exception:  # counted below; no address or error text in the log
                logger.warning("Host alert email could not be sent (kind %s).", kind)
        record_event(
            "system.host_alert",
            actor=None,
            channel=Channel.CLI,
            metadata={"kind": kind, "lines": lines, "sent": sent},
        )
        if not sent:
            # The timers keep the report (old AIDE baseline, Falco offset, audit
            # checkpoint) and send it again next run (R2-006).
            raise CommandError(
                f"The alert reached nobody ({len(recipients)} sysadmin address(es)); "
                "it will be sent again on the next run."
            )
        self.stdout.write(f"Sent to {sent} sysadmin(s).")
