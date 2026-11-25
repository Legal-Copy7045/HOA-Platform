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

``owner_email`` may be blank; when it matches an existing person (for
example the first officer created by bootstrap_first_officer) that person
is used. ``ownership_type`` is sole, joint, trust or entity (default: sole,
or joint when a lot has several owners). ``owner_since`` is YYYY-MM-DD (the
deed date).

The file holds real personal data: keep it off the repository and delete it
from the server after the import. Nothing from it is printed or logged except
row numbers and counts. Lots that already have current owners are left alone
(changes of owner go through a transfer in the portal).
"""

from __future__ import annotations

import csv
import datetime
from collections import defaultdict
from pathlib import Path

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.core.validators import validate_email
from django.db import transaction

from audit.models import Channel
from audit.services import record_event
from community import services
from community.models import Lot, Ownership, OwnershipType, Person
from core.policy import SYSTEM
from core.scoping import system_access

COLUMNS = ["lot", "street_address", "owner_name", "owner_email", "owner_since", "ownership_type"]
MAX_ROWS = 2000


class Command(BaseCommand):
    help = "Check (and with --commit, load) the lot and owner register from a CSV file."

    def add_arguments(self, parser):
        parser.add_argument("path")
        parser.add_argument("--commit", action="store_true", help="Write; otherwise check only.")

    def handle(self, *args, path, commit, **opts):
        rows = self._read(Path(path))
        lots, problems = self._validate(rows)
        if problems:
            for problem in problems[:50]:
                self.stdout.write(self.style.ERROR(problem))
            raise CommandError(f"{len(problems)} problem(s); nothing was written.")
        plan = self._plan(lots)
        self.stdout.write(
            f"Lots to create: {plan['new_lots']}. Lots to fill: {len(plan['fill'])}. "
            f"Lots skipped (already have owners): {len(plan['skip'])}. "
            f"Owners to record: {sum(len(v) for v in plan['fill'].values())}."
        )
        for number in plan["skip"]:
            self.stdout.write(f"  lot {number}: already has current owners; left as is")
        if not commit:
            self.stdout.write(self.style.WARNING("Check only. Run again with --commit to write."))
            return
        created_people = self._write(lots, plan)
        record_event(
            "register.imported",
            actor=None,
            channel=Channel.CLI,
            metadata={
                "lots": len(plan["fill"]),
                "owners": sum(len(v) for v in plan["fill"].values()),
                "people_created": created_people,
            },
        )
        self.stdout.write(self.style.SUCCESS("Register loaded."))

    # --- reading and checking --------------------------------------------------------

    def _read(self, path: Path) -> list[dict]:
        if not path.is_file():
            raise CommandError("File not found.")
        if path.stat().st_size > 2_000_000:
            raise CommandError("File too large for a register.")
        with path.open(newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            header = [h.strip().lower() for h in (reader.fieldnames or [])]
            missing = [c for c in COLUMNS[:5] if c not in header]
            if missing:
                raise CommandError(f"Missing column(s): {', '.join(missing)}.")
            rows = []
            for row in reader:
                rows.append({k.strip().lower(): (v or "").strip() for k, v in row.items() if k})
                if len(rows) > MAX_ROWS:
                    raise CommandError("Too many rows.")
        return rows

    def _validate(self, rows):
        problems: list[str] = []
        lots: dict[int, dict] = defaultdict(lambda: {"address": None, "owners": []})
        for i, row in enumerate(rows, start=2):  # row 1 is the header
            where = f"row {i}"
            try:
                number = int(row.get("lot", ""))
                if not 1 <= number <= 9999:
                    raise ValueError
            except ValueError:
                problems.append(f"{where}: lot must be a whole number")
                continue
            address = " ".join(row.get("street_address", "").split())[:200]
            name = " ".join(row.get("owner_name", "").split())[:200]
            email = row.get("owner_email", "")
            kind = (row.get("ownership_type") or "").lower()
            if not address:
                problems.append(f"{where}: street_address is empty")
            elif lots[number]["address"] not in (None, address):
                problems.append(f"{where}: lot {number} has two different addresses")
            lots[number]["address"] = lots[number]["address"] or address
            if not name:
                problems.append(f"{where}: owner_name is empty")
            if email:
                try:
                    validate_email(email)
                except ValidationError:
                    problems.append(f"{where}: owner_email is not a valid address")
            try:
                since = datetime.date.fromisoformat(row.get("owner_since", ""))
            except ValueError:
                problems.append(f"{where}: owner_since must be YYYY-MM-DD")
                continue
            if since > datetime.date.today():
                problems.append(f"{where}: owner_since is in the future")
            if kind and kind not in OwnershipType.values:
                problems.append(f"{where}: ownership_type must be one of {OwnershipType.values}")
            lots[number]["owners"].append(
                {"name": name, "email": email, "since": since, "kind": kind}
            )
        for number, lot in lots.items():
            emails = [o["email"].lower() for o in lot["owners"] if o["email"]]
            if len(emails) != len(set(emails)):
                problems.append(f"lot {number}: the same email is listed twice")
        return dict(lots), problems

    def _plan(self, lots):
        plan = {"new_lots": 0, "fill": {}, "skip": []}
        with system_access("import_register plan"):
            existing = {lot.number: lot for lot in Lot.objects.filter(number__in=list(lots))}
            for number, lot in sorted(lots.items()):
                current = existing.get(number)
                if current is None:
                    plan["new_lots"] += 1
                elif Ownership.objects.active().filter(lot=current).exists():
                    plan["skip"].append(number)
                    continue
                plan["fill"][number] = lot["owners"]
        return plan

    # --- writing ------------------------------------------------------------------------

    def _write(self, lots, plan) -> int:
        created_people = 0
        with transaction.atomic(), system_access("import_register write"):
            for number, owners in plan["fill"].items():
                lot = Lot.objects.filter(number=number).first()
                if lot is None:
                    lot = Lot.objects.create(number=number, street_address=lots[number]["address"])
                for owner in owners:
                    person = None
                    if owner["email"]:
                        person = Person.objects.filter(email__iexact=owner["email"]).first()
                    if person is None:
                        person = services.record_person(
                            display_name=owner["name"], email=owner["email"], actor=SYSTEM
                        )
                        created_people += 1
                    kind = owner["kind"] or (
                        OwnershipType.JOINT if len(owners) > 1 else OwnershipType.SOLE
                    )
                    services.record_ownership(
                        person=person,
                        lot=lot,
                        start_date=owner["since"],
                        ownership_type=kind,
                        actor=SYSTEM,
                    )
        return created_people
