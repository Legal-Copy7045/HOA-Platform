"""History tables are insert-only for the runtime role (decision D8)."""

import psycopg
import pytest

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize(
    "table",
    [
        "community_historicallot",
        "community_historicalperson",
        "community_historicalownership",
        "community_historicalboardseat",
        "community_historicalofficeholding",
    ],
)
def test_app_role_cannot_rewrite_history(app_role_connection, table):
    for statement in (
        f"UPDATE {table} SET history_id = history_id WHERE false",  # nosec B608
        f"DELETE FROM {table} WHERE false",  # nosec B608
    ):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            app_role_connection.execute(statement)
