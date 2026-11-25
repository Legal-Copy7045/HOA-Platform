"""Counts of the main records (no personal data): a quick check of what a
database holds, e.g. after seed_demo or a restore."""

from django.conf import settings
from django.core.management.base import BaseCommand

from core.scoping import system_access


class Command(BaseCommand):
    help = "Print record counts (lots, people, owners, board, accounts, operations)."

    def handle(self, *args, **opts):
        from community.models import Account, BoardSeat, Lot, Ownership, Person
        from operations.models import OwnerRequest, Vendor

        db = settings.DATABASES["default"]
        self.stdout.write(f"Database: {db.get('NAME')} on {db.get('HOST') or 'local socket'}")
        self.stdout.write(f"Demo seeding allowed here: {settings.SEED_DEMO_ALLOWED}")
        with system_access("data_status counts"):
            rows = [
                ("Lots", Lot.objects.count()),
                ("People", Person.objects.count()),
                ("Current ownerships", Ownership.objects.active().count()),
                ("Active board seats", BoardSeat.objects.active().count()),
                ("Accounts", Account.objects.count()),
                ("Vendors", Vendor.objects.count()),
                ("Owner requests", OwnerRequest.objects.count()),
            ]
        for label, n in rows:
            self.stdout.write(f"  {label:<22} {n}")
        if rows[0][1] == 0:
            self.stdout.write(
                self.style.WARNING("No lots: run seed_demo (POC only) or record lots and owners.")
            )
