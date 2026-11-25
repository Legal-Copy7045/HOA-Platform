import os

import psycopg
import pytest
from django.db import connection


@pytest.fixture
def app_role_connection(settings):
    """A raw connection to the *test* database as the runtime app role.

    Used to prove that Postgres itself (not just Django) enforces the
    append-only and no-DDL rules for the role web and worker run as.
    """
    params = connection.settings_dict
    conn = psycopg.connect(
        host=params.get("HOST") or "localhost",
        port=params.get("PORT") or 5432,
        dbname=params["NAME"],
        user=settings.DB_APP_ROLE,
        password=os.environ.get("DB_APP_PASSWORD_FOR_TESTS", "hoa_app"),
        autocommit=True,
    )
    try:
        yield conn
    finally:
        conn.close()
