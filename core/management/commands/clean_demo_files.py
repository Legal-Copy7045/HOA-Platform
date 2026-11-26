"""Remove stored files the database no longer refers to, after a demo reseed.

    python manage.py clean_demo_files

Run by infra/poc/reseed-demo.sh once the new dataset is in (decision D53):
the replaced data's uploads (documents, attachments, sign-in pictures) are
left in the file store with nothing pointing at them. A backup with every
file was taken before the reseed. Only on a demo deployment
(SEED_DEMO_ALLOWED=true); never touches the health-check probe objects.
"""

from django.conf import settings
from django.core.files.storage import default_storage
from django.core.management.base import BaseCommand, CommandError

from audit.models import Channel
from audit.services import record_event
from core.backups import unreferenced_files


class Command(BaseCommand):
    help = "Delete stored files no record refers to (demo reseed only)."

    def handle(self, *args, **opts):
        if not settings.SEED_DEMO_ALLOWED:
            raise CommandError("Refusing: only on a demo deployment (SEED_DEMO_ALLOWED=true).")
        count = 0
        for key in unreferenced_files(default_storage):
            default_storage.delete(key)
            count += 1
        record_event(
            "system.demo_files_cleaned", actor=None, channel=Channel.CLI, metadata={"count": count}
        )
        self.stdout.write(f"Removed {count} file(s) of the replaced data.")
