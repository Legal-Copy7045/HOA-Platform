"""Prove the off-site bucket refuses to delete backups (its bucket lock).

    python manage.py check_offsite_lock

Writes a tiny probe file under backups/, tries to delete it, and checks it is
still there. Exits non-zero when the delete succeeded (no lock). The result
is audited and shown on System status (Backups).
"""

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from audit.models import Channel
from audit.services import record_event
from core import offsite
from core.models import Heartbeat


class Command(BaseCommand):
    help = "Check the off-site bucket's lock stops backups being deleted."

    def handle(self, *args, **opts):
        try:
            locked = offsite.lock_probe()
        except offsite.OffsiteError as exc:
            raise CommandError(str(exc)) from exc
        record_event(
            "system.offsite_lock_checked",
            actor=None,
            channel=Channel.CLI,
            metadata={"locked": locked},
        )
        if not locked:
            Heartbeat.objects.filter(name="offsite_lock").delete()
            raise CommandError(
                "The bucket let a backup be deleted: add a bucket lock rule for the prefix "
                "backups/ (R2 -> bucket -> Settings -> Bucket lock rules), then run this again."
            )
        Heartbeat.objects.update_or_create(name="offsite_lock", defaults={"at": timezone.now()})
        self.stdout.write("Locked: the bucket refused to delete a backup file.")
