"""Runtime database role grants (decision Q8).

Two Postgres roles:

* the *owner* role runs migrations and owns every table;
* the *app* role (``DB_APP_ROLE``) is what web and worker connect as.

After every ``migrate`` we grant the app role ordinary DML on all tables and
sequences, then revoke UPDATE/DELETE/TRUNCATE on tables registered as
append-only. Because this runs on ``post_migrate`` it covers tables created by
any app, including third-party ones, and it is idempotent.

If ``DB_APP_ROLE`` is empty (e.g. a throwaway single-role database) this is a
no-op; production settings refuse to start without it.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.db import connections
from psycopg import sql

logger = logging.getLogger(__name__)

# Tables whose rows may only be inserted and read by the app role.
# Concrete AppendOnlyModel subclasses are found automatically; this set is for
# tables without a Django model.
_APPEND_ONLY_TABLES: set[str] = set()


def register_append_only_table(table: str) -> None:
    _APPEND_ONLY_TABLES.add(table)


def append_only_tables() -> frozenset[str]:
    """Every concrete AppendOnlyModel table, plus any registered explicitly."""
    from django.apps import apps

    from core.append_only import AppendOnlyModel

    model_tables = {
        model._meta.db_table
        for model in apps.get_models()
        if issubclass(model, AppendOnlyModel) and not model._meta.proxy
    }
    return frozenset(model_tables | _APPEND_ONLY_TABLES)


def apply_runtime_grants(using: str = "default") -> bool:
    role = settings.DB_APP_ROLE
    connection = connections[using]
    if not role or connection.vendor != "postgresql":
        return False
    role_id = sql.Identifier(role)
    statements = [
        sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(role_id),
        sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {}").format(
            role_id
        ),
        sql.SQL("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {}").format(role_id),
        sql.SQL("REVOKE TRUNCATE ON ALL TABLES IN SCHEMA public FROM {}").format(role_id),
    ]
    protected = append_only_tables()
    for table in sorted(protected):
        statements.append(
            sql.SQL("REVOKE UPDATE, DELETE, TRUNCATE ON TABLE {} FROM {}").format(
                sql.Identifier(table), role_id
            )
        )
    with connection.cursor() as cursor:
        for statement in statements:
            cursor.execute(statement.as_string(cursor.connection))
    logger.info(
        "db.runtime_grants_applied",
        extra={"append_only_tables": len(protected)},
    )
    return True


def apply_runtime_grants_on_migrate(sender, using: str = "default", **kwargs) -> None:
    # post_migrate fires once per app; only act once per migrate run.
    if sender.name != "core":
        return
    apply_runtime_grants(using=using)
