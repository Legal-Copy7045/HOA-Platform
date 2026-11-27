"""Refuse to start web or worker as the database owner (security review E6).

Append-only protection relies on the runtime role having no UPDATE/DELETE on
those tables and owning nothing; a deployment that hands web the owner's
DATABASE_URL would silently lose that. A superuser (or a role that can
bypass row security, create roles or create databases) could also switch the
append-only triggers off or grant itself back what was revoked, so such a
runtime role is refused too (D87). So is a runtime role that is a member,
directly or through other roles, of a role that owns tables, has any of those
powers, or of pg_write_all_data / pg_database_owner (SEC-002): membership
hands it the same rights. The container entrypoint runs this before starting
web or worker.
"""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection

# Roles the current user is a member of (recursively: pg_has_role follows the
# whole membership chain, with or without INHERIT, since SET ROLE reaches
# them) that would give it back what the append-only rules take away.
MEMBERSHIP_SQL = (
    "SELECT r.rolname FROM pg_catalog.pg_roles r "
    "WHERE r.rolname <> current_user "
    "AND pg_catalog.pg_has_role(current_user, r.oid, 'MEMBER') "
    "AND (r.rolname IN ('pg_write_all_data', 'pg_database_owner') "
    "OR r.rolsuper OR r.rolbypassrls OR r.rolcreaterole OR r.rolcreatedb "
    "OR EXISTS (SELECT 1 FROM pg_catalog.pg_class c "
    "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
    "WHERE c.relowner = r.oid AND n.nspname NOT IN ('pg_catalog', 'information_schema') "
    "AND c.relkind IN ('r', 'p'))) "
    "ORDER BY r.rolname"
)


class Command(BaseCommand):
    help = (
        "Check that this process connects as DB_APP_ROLE, owns no tables and is not a "
        "superuser or a role with BYPASSRLS, CREATEROLE or CREATEDB, nor a member of a "
        "role that owns tables, has those powers, or of pg_write_all_data."
    )

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
            cursor.execute(
                "SELECT rolsuper, rolbypassrls, rolcreaterole, rolcreatedb "
                "FROM pg_catalog.pg_roles WHERE rolname = current_user"
            )
            flags = cursor.fetchone() or (False, False, False, False)
            cursor.execute(MEMBERSHIP_SQL)
            member_of = [row[0] for row in cursor.fetchall()]
        if current != role:
            raise CommandError(
                f"Connected as {current!r}, not the runtime role {role!r}. Web and worker "
                "must use the app role's DATABASE_URL, never the owner's (decision D8)."
            )
        powers = [
            name
            for name, on in zip(
                ("SUPERUSER", "BYPASSRLS", "CREATEROLE", "CREATEDB"), flags, strict=True
            )
            if on
        ]
        problems = []
        if powers:
            problems.append(
                f"The runtime role {role!r} has {', '.join(powers)}: it must be a plain login "
                "role (NOSUPERUSER NOBYPASSRLS NOCREATEROLE NOCREATEDB), or the append-only "
                "rules can be switched off (docker/postgres/init-roles.sh)."
            )
        if owned:
            problems.append(f"The runtime role owns {owned} table(s); it must own none.")
        if member_of:
            problems.append(
                f"The runtime role {role!r} is a member of {', '.join(member_of)}, which "
                "gives it the rights the append-only rules take away (UPDATE/DELETE, "
                "switching triggers off): REVOKE that membership."
            )
        if problems:
            raise CommandError(" ".join(problems))
        self.stdout.write("Database role OK.")
