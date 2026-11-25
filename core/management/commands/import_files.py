"""Restore uploaded files from a tar archive made by export_files.

    python manage.py import_files --in -           # from stdin (restore.sh pipes it)

Existing objects are kept. Replacing them (--overwrite) can change published
document versions, so it is break-glass: it needs --actor and --reason
(SEC-009). Every import is audited.
"""

import sys

from django.core.management.base import CommandError

from audit.models import Channel
from audit.services import record_event
from core.backups import import_files
from core.breakglass import BreakGlassCommand


class Command(BreakGlassCommand):
    help = "Import files from a tar archive (stdin with --in -)."
    command_name = "import_files"

    def add_arguments(self, parser):
        parser.add_argument("--in", dest="source", required=True)
        parser.add_argument("--overwrite", action="store_true")
        parser.add_argument("--actor", help="Your sysadmin login email (needed with --overwrite).")
        parser.add_argument("--reason", help="Why (needed with --overwrite).")

    def handle(self, *args, source, overwrite, **opts):
        actor = None
        if overwrite:
            if not opts.get("actor") or not opts.get("reason"):
                raise CommandError("--overwrite replaces stored files: give --actor and --reason.")
            actor = self.resolve_actor(opts)
        if source == "-":
            count = import_files(sys.stdin.buffer, overwrite=overwrite)
        else:
            with open(source, "rb") as fh:
                count = import_files(fh, overwrite=overwrite)
        if actor is not None:
            self.audit(actor, opts, f"{count} files")
        record_event(
            "backup.files_imported",
            actor=actor,
            channel=Channel.CLI,
            metadata={"count": count, "overwrite": overwrite},
        )
        self.stdout.write(f"Imported {count} files.")
