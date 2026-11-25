"""Decision Q8: the runtime role gets ordinary DML but no TRUNCATE or DDL."""

import pytest
from django.db import connection

from core import db_roles


def _privilege(role: str, table: str, privilege: str) -> bool:
    with connection.cursor() as cursor:
        cursor.execute("SELECT has_table_privilege(%s, %s, %s)", [role, table, privilege])
        return cursor.fetchone()[0]


@pytest.mark.django_db
def test_app_role_has_dml_but_not_truncate(settings):
    role = settings.DB_APP_ROLE
    assert role, "tests must run with DB_APP_ROLE set"
    for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
        assert _privilege(role, "django_session", privilege)
    assert not _privilege(role, "django_session", "TRUNCATE")


@pytest.mark.django_db
def test_registered_append_only_table_loses_update_and_delete(settings):
    role = settings.DB_APP_ROLE
    with connection.cursor() as cursor:
        cursor.execute("CREATE TABLE core_test_append_only (id int)")
    db_roles.register_append_only_table("core_test_append_only")
    try:
        db_roles.apply_runtime_grants()
        assert _privilege(role, "core_test_append_only", "SELECT")
        assert _privilege(role, "core_test_append_only", "INSERT")
        assert not _privilege(role, "core_test_append_only", "UPDATE")
        assert not _privilege(role, "core_test_append_only", "DELETE")
        assert not _privilege(role, "core_test_append_only", "TRUNCATE")
    finally:
        db_roles._APPEND_ONLY_TABLES.discard("core_test_append_only")


def test_grants_are_noop_without_role(settings):
    settings.DB_APP_ROLE = ""
    assert db_roles.apply_runtime_grants() is False
