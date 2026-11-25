"""Refuse to start web or worker as the database owner (security review E6).

Append-only protection relies on the runtime role having no UPDATE/DELETE on
those tables and owning nothing; a deployment that hands web the owner's
DATABASE_URL would silently lose that. The container entrypoint runs this
before starting web or worker.
"""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection


class Command(BaseCommand):
    help = "Check that this process connects as DB_APP_ROLE and owns no tables."

    def handle(self, *args, **options):
        role = settings.DB_APP_ROLE
        if not role:
            self.stdout.write("DB_APP_ROLE is empty: single-role database, check skipped.")
            return
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_user")
            current = cursor.fetchone()[0]
            cursor.execute(
                "SELECT count(*) FROM pg_catalog.pg_class c "
                "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
                "WHERE pg_catalog.pg_get_userbyid(c.relowner) = current_user "
                "AND n.nspname NOT IN ('pg_catalog', 'information_schema') "
                "AND c.relkind IN ('r', 'p')"
            )
            owned = cursor.fetchone()[0]
        if current != role:
            raise CommandError(
                f"Connected as {current!r}, not the runtime role {role!r}. Web and worker "
                "must use the app role's DATABASE_URL, never the owner's (decision D8)."
            )
        if owned:
            raise CommandError(f"The runtime role owns {owned} table(s); it must own none.")
        self.stdout.write("Database role OK.")
