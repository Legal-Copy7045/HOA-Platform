"""Fictional owner-help data (D73) on top of seed_demo: the board's words on
two help pages and some demo logins' getting-started progress.

    python manage.py seed_demo_help

Needs SEED_DEMO_ALLOWED=true. Safe to run again.
"""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Add fictional help-page text and getting-started progress (demo data)."

    def handle(self, *args, **options):
        if not getattr(settings, "SEED_DEMO_ALLOWED", False):
            raise CommandError("Refusing: SEED_DEMO_ALLOWED is not true.")
        from core import demo_help

        self.stdout.write(demo_help.seed())
