"""Shared plumbing for break-glass management commands (decision D9).

Each command requires --actor (an active sysadmin's login email) and
--reason, runs the normal service as SYSTEM, and records a
``sysadmin.breakglass`` audit event that the board sees on the sysadmin
activity page. Keep reasons free of personal data.
"""

from django.core.management.base import BaseCommand, CommandError

from accounts.models import User
from audit.models import Channel
from audit.services import record_event
from core.policy import is_sysadmin


class BreakGlassCommand(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument("--actor", required=True, help="Your sysadmin login email.")
        parser.add_argument(
            "--reason", required=True, help="Why (no personal data; up to 200 characters)."
        )

    def resolve_actor(self, opts) -> User:
        reason = (opts["reason"] or "").strip()
        if len(reason) < 10 or len(reason) > 200:
            raise CommandError("--reason must be 10 to 200 characters.")
        actor = User.objects.filter(email=opts["actor"].strip().lower(), is_active=True).first()
        if actor is None or not is_sysadmin(actor):
            raise CommandError("--actor must be the login email of an active sysadmin.")
        return actor

    def audit(self, actor, opts, target_id) -> None:
        record_event(
            "sysadmin.breakglass",
            actor=actor,
            channel=Channel.CLI,
            metadata={
                "command": self.command_name,
                "reason": opts["reason"].strip(),
                "target_id": str(target_id),
            },
        )
