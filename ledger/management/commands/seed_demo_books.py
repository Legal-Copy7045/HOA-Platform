"""Synthetic books for the demo community (D69-D71): see ledger/demo_books.py.

Needs SEED_DEMO_ALLOWED=true; does nothing when the demo bank accounts
already exist."""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Seed fictional bank accounts, coded lines and closed periods (demo only)."

    def handle(self, *args, **opts):
        if not settings.SEED_DEMO_ALLOWED:
            raise CommandError("Refusing: set SEED_DEMO_ALLOWED=true (never in production).")
        from ledger.demo_books import seed

        result = seed()
        self.stdout.write(f"Demo books: {result}")
