"""Stream every uploaded file into a tar archive (for backups).

python manage.py export_files --out -          # to stdout (backup.sh pipes it)
python manage.py export_files --out files.tar
"""

import sys

from django.core.management.base import BaseCommand

from audit.models import Channel
from audit.services import record_event
from core.backups import export_files


class Command(BaseCommand):
    help = "Export the file store to a tar archive (stdout with --out -)."

    def add_arguments(self, parser):
        parser.add_argument("--out", required=True)

    def handle(self, *args, out, **opts):
        if out == "-":
            count = export_files(sys.stdout.buffer)
            sys.stdout.buffer.flush()
            self.stderr.write(f"Exported {count} files.")
        else:
            with open(out, "wb") as fh:
                count = export_files(fh)
            self.stdout.write(f"Exported {count} files.")
        # Run nightly by the backup timer, so no person to name; audited (SEC-009).
        record_event(
            "backup.files_exported", actor=None, channel=Channel.CLI, metadata={"count": count}
        )
