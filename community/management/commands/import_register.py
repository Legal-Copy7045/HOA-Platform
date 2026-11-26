"""Load the community's lot and owner register from a CSV file (run on the
server, once at go-live, or to add lots that are missing).

Every board member is also a homeowner: their ownership comes from this
register like everyone else's, and their board seat is added on top of it.
When a seat ends, the homeowner access stays.

    python manage.py import_register /path/to/register.csv            # check only
    python manage.py import_register /path/to/register.csv --commit   # write

CSV columns (header row required; one row per owner, repeat the lot for joint
owners):

    lot,street_address,owner_name,owner_email,owner_since,ownership_type
    [owner_phone,mailing_street,mailing_city,mailing_state,mailing_zip]

``owner_email`` may be blank; when it matches an existing person (for
example the first officer created by bootstrap_first_officer) that person
is used. ``ownership_type`` is sole, joint, trust or entity (default: sole,
or joint when a lot has several owners). ``owner_since`` is YYYY-MM-DD (the
deed date).

The file holds real personal data: keep it off the repository and delete it
from the server after the import. Nothing from it is printed or logged except
row numbers and counts. Lots that already have current owners are left alone
(changes of owner go through a transfer in the portal).

The board can do the same in the portal (Homes and owners -> Import homes
and owners, D54), with a template, an Excel workbook or a CSV file, a malware
scan and a preview. Both use community/register_import.py.
"""

from __future__ import annotations

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from audit.models import Channel
from audit.services import record_event
from community import register_import as ri
from core.policy import SYSTEM

COLUMNS = ri.COLUMNS
MAX_ROWS = ri.MAX_ROWS


class Command(BaseCommand):
    help = "Check (and with --commit, load) the lot and owner register from a CSV file."

    def add_arguments(self, parser):
        parser.add_argument("path")
        parser.add_argument("--commit", action="store_true", help="Write; otherwise check only.")

    def handle(self, *args, path, commit, **opts):
        sheet = self._read(Path(path))
        result = ri.check(sheet)
        if result.problems:
            for problem in result.problems[:50]:
                self.stdout.write(self.style.ERROR(str(problem)))
            raise CommandError(f"{len(result.problems)} problem(s); nothing was written.")
        if result.unknown_columns:
            self.stdout.write(f"Ignored column(s): {', '.join(result.unknown_columns)}.")
        self.stdout.write(
            f"Lots to create: {len(result.new_lots)}. Lots to fill: {len(result.fill)}. "
            f"Lots skipped (already have owners): {len(result.skip)}. "
            f"Owners to record: {result.owners}."
        )
        for number in result.skip:
            self.stdout.write(f"  lot {number}: already has current owners; left as is")
        if not commit:
            self.stdout.write(self.style.WARNING("Check only. Run again with --commit to write."))
            return
        counts = ri.write(result, actor=SYSTEM)
        record_event(
            "register.imported",
            actor=None,
            channel=Channel.CLI,
            metadata={
                "lots": counts["lots_filled"],
                "owners": counts["owners"],
                "people_created": counts["people_created"],
            },
        )
        self.stdout.write(self.style.SUCCESS("Register loaded."))

    def _read(self, path: Path) -> ri.Sheet:
        if not path.is_file():
            raise CommandError("File not found.")
        if path.stat().st_size > ri.MAX_BYTES:
            raise CommandError("File too large for a register.")
        kind = "xlsx" if path.suffix.lower() == ".xlsx" else "csv"
        try:
            return ri.read(path.read_bytes(), kind)
        except ri.RegisterError as exc:
            raise CommandError(str(exc)) from exc
