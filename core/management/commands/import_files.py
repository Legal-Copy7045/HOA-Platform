"""Restore uploaded files from a tar archive made by export_files.

    python manage.py import_files --in -           # from stdin (restore.sh pipes it)

Existing objects are kept unless --overwrite. Used by the restore drill.
"""

import sys

from django.core.management.base import BaseCommand

from core.backups import import_files


class Command(BaseCommand):
    help = "Import files from a tar archive (stdin with --in -)."

    def add_arguments(self, parser):
        parser.add_argument("--in", dest="source", required=True)
        parser.add_argument("--overwrite", action="store_true")

    def handle(self, *args, source, overwrite, **opts):
        if source == "-":
            count = import_files(sys.stdin.buffer, overwrite=overwrite)
        else:
            with open(source, "rb") as fh:
                count = import_files(fh, overwrite=overwrite)
        self.stdout.write(f"Imported {count} files.")
