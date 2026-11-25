"""Every append-only model must have its triggers and lose UPDATE/DELETE grants.

This guards later apps (ledger, ARC decisions, votes): subclassing
AppendOnlyModel without adding append_only_trigger_operations() to the
migration fails here.
"""

import pytest
from django.apps import apps
from django.db import connection

from core.append_only import AppendOnlyModel
from core.db_roles import append_only_tables


def _append_only_models():
    return [m for m in apps.get_models() if issubclass(m, AppendOnlyModel) and not m._meta.proxy]


def test_there_is_at_least_one_append_only_model():
    assert _append_only_models()


@pytest.mark.django_db
@pytest.mark.parametrize("model", _append_only_models(), ids=lambda m: m._meta.label)
def test_append_only_model_is_fully_protected(model, settings):
    table = model._meta.db_table
    assert table in append_only_tables()
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT tgname FROM pg_trigger WHERE tgrelid = %s::regclass AND NOT tgisinternal",
            [table],
        )
        triggers = {row[0] for row in cursor.fetchall()}
        assert f"{table}_append_only_row" in triggers
        assert f"{table}_append_only_truncate" in triggers
        for privilege in ("UPDATE", "DELETE", "TRUNCATE"):
            cursor.execute(
                "SELECT has_table_privilege(%s, %s, %s)", [settings.DB_APP_ROLE, table, privilege]
            )
            assert cursor.fetchone()[0] is False, privilege
