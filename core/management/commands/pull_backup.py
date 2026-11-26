"""Read backups back from the off-site bucket (for the restore drill or a
real restore onto a new server).

    python manage.py pull_backup --list                 # complete backups, oldest first
    python manage.py pull_backup --name <stamp>-<file> > file

Files come back still encrypted; restore-drill.sh checks their keyed checksum
before anything is decrypted.
"""

import sys

from django.core.management.base import BaseCommand, CommandError

from core import offsite


class Command(BaseCommand):
    help = "List or download encrypted backups from the off-site bucket."

    def add_arguments(self, parser):
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument("--list", action="store_true")
        group.add_argument("--name")

    def handle(self, *args, list, name, **opts):
        try:
            if list:
                for stamp in offsite.stamps():
                    self.stdout.write(stamp)
                return
            offsite.fetch(name, sys.stdout.buffer)
            sys.stdout.buffer.flush()
        except offsite.OffsiteError as exc:
            raise CommandError(str(exc)) from exc
