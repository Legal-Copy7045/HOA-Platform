"""Record that a backup finished (run by infra/poc/backup.sh).

    python manage.py backup_heartbeat --kind local     # encrypted copy on the VM
    python manage.py backup_heartbeat --kind offsite   # every file uploaded

The health check alerts the sysadmins when either is more than a day and a
half old.
"""

from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import Heartbeat


class Command(BaseCommand):
    help = "Record that the nightly backup (local or off-site) finished."

    def add_arguments(self, parser):
        parser.add_argument("--kind", required=True, choices=["local", "offsite"])

    def handle(self, *args, kind, **opts):
        Heartbeat.objects.update_or_create(name=f"backup_{kind}", defaults={"at": timezone.now()})
