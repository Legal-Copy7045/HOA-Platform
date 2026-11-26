"""Copy one (already encrypted) backup file to the off-site bucket.

    cat db.dump.enc | python manage.py push_backup --name 2027-01-01T0230Z-db.dump.enc

The off-site bucket is separate from the portal's own file store and owned by
the HOA (e.g. Cloudflare R2, with a bucket lock on backups/). Its credentials
come from BACKUP_S3_* in .env; files arrive already encrypted on the VM, so
the bucket never sees plain data.
"""

import sys

from django.core.management.base import BaseCommand, CommandError

from core import offsite


class Command(BaseCommand):
    help = "Upload an encrypted backup file from stdin to the off-site bucket."

    def add_arguments(self, parser):
        parser.add_argument("--name", required=True)

    def handle(self, *args, name, **opts):
        try:
            size = offsite.push(name, sys.stdin.buffer)
        except offsite.OffsiteError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(f"Uploaded {name} off-site ({size} bytes).")
