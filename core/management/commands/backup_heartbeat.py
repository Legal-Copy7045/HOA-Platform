"""Record that a backup or a restore drill finished (run by the VM's scripts).

    python manage.py backup_heartbeat --kind local          # encrypted copy on the VM (backup.sh)
    python manage.py backup_heartbeat --kind offsite        # every file uploaded (backup.sh)
    python manage.py backup_heartbeat --kind restore_drill  # drill passed (restore-drill.sh)

The health check alerts the sysadmins when a backup is more than a day and a
half old, and, in production, when the last successful restore drill is more
than 90 days old (D57).
"""

from django.core.management.base import BaseCommand
from django.utils import timezone

from core.health import RESTORE_DRILL_BEAT
from core.models import Heartbeat

NAMES = {"local": "backup_local", "offsite": "backup_offsite", "restore_drill": RESTORE_DRILL_BEAT}
# What the incident timeline says (D74). A drill records its heartbeat only
# after the signed backup's keyed checksum verified and the restore passed.
WORDS = {
    "local": "Backup taken: an encrypted copy of the database and the files on the server",
    "offsite": "Off-site copy uploaded to the locked bucket",
    "restore_drill": "Restore drill passed: the signed backup verified and restored into a "
    "throwaway copy",
}


class Command(BaseCommand):
    help = "Record that the nightly backup (local or off-site) or a restore drill finished."

    def add_arguments(self, parser):
        parser.add_argument("--kind", required=True, choices=sorted(NAMES))

    def handle(self, *args, kind, **opts):
        from core import ops_timeline

        Heartbeat.objects.update_or_create(name=NAMES[kind], defaults={"at": timezone.now()})
        ops_timeline.record("backups", WORDS[kind], key=kind)
