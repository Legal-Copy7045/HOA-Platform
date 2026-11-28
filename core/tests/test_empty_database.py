"""infra/poc/empty-database.sql (D98): the reseed and the restore empty the
database table by table, because one DROP SCHEMA public CASCADE took a lock
on every object in a single transaction and ran out of lock slots on the VM
("out of shared memory", psql exit 3, the 0.44.0 reseed failure).

The test builds a scratch database (owned by the owner role, like
docker/postgres/init-roles.sh makes it) whose schema has more objects than
one transaction may lock on this Postgres server (sized from the server's
own max_locks_per_transaction and max_connections), with the kinds the
portal has (foreign keys, an append-only trigger, the btree_gist extension,
a view, sequences), runs the file with psql exactly as the scripts do, and
checks the schema comes back empty, owned by the owner role, with the
runtime role's USAGE. Building it never holds many locks at once either."""

from __future__ import annotations

import shutil
import subprocess  # nosec B404 - runs psql on the repository's own SQL file
from pathlib import Path

import psycopg
import pytest
from django.conf import settings
from django.db import connection
from psycopg import sql

pytestmark = pytest.mark.django_db
SQL_FILE = Path("infra/poc/empty-database.sql")
PSQL = shutil.which("psql")


def _params():
    p = connection.settings_dict
    return {
        "host": p.get("HOST") or "localhost",
        "port": str(p.get("PORT") or 5432),
        "user": p["USER"],
        "password": p.get("PASSWORD") or "",
    }


def _objects(conn) -> int:
    """Objects a DROP SCHEMA CASCADE would lock: relations, types,
    constraints, triggers, functions and defaults in public."""
    return conn.execute(
        """
        SELECT (SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                 WHERE n.nspname = 'public')
             + (SELECT count(*) FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
                 WHERE n.nspname = 'public')
             + (SELECT count(*) FROM pg_constraint k JOIN pg_namespace n ON n.oid = k.connamespace
                 WHERE n.nspname = 'public')
             + (SELECT count(*) FROM pg_trigger g JOIN pg_class c ON c.oid = g.tgrelid
                 JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public')
             + (SELECT count(*) FROM pg_attrdef d JOIN pg_class c ON c.oid = d.adrelid
                 JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public')
        """
    ).fetchone()[0]


@pytest.fixture
def scratch_db():
    params = _params()
    name = f"{connection.settings_dict['NAME']}_empty"[:60]
    admin = psycopg.connect(dbname=connection.settings_dict["NAME"], autocommit=True, **params)
    try:
        admin.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name)))
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        yield name, params
    finally:
        admin.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
        )
        admin.close()


@pytest.mark.skipif(PSQL is None, reason="psql not installed")
def test_a_schema_bigger_than_one_transaction_may_lock_is_emptied(scratch_db):
    name, params = scratch_db
    app = settings.DB_APP_ROLE or params["user"]
    with psycopg.connect(dbname=name, autocommit=True, **params) as conn:
        locks, conns, prepared = (
            int(conn.execute(f"SHOW {s}").fetchone()[0])  # nosec B608
            for s in ("max_locks_per_transaction", "max_connections", "max_prepared_transactions")
        )
        limit = locks * (conns + prepared)
        conn.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
        conn.execute(
            "CREATE FUNCTION reject_change() RETURNS trigger LANGUAGE plpgsql AS "
            "$$ BEGIN RAISE EXCEPTION 'append-only'; END $$"
        )
        conn.execute("CREATE TABLE parent (id bigserial PRIMARY KEY, label text NOT NULL)")
        # About ten lockable objects per table; batches of 40 keep each
        # statement's own locks small.
        tables = limit // 9 + 50
        for start in range(0, tables, 40):
            body = []
            for i in range(start, min(start + 40, tables)):
                body.append(
                    f"CREATE TABLE t{i} (id bigserial PRIMARY KEY, "
                    f"parent_id bigint REFERENCES parent(id), note text DEFAULT 'x', "
                    f"CONSTRAINT t{i}_note CHECK (length(note) < 100));"
                    f"CREATE TRIGGER t{i}_append_only BEFORE UPDATE OR DELETE ON t{i} "
                    f"FOR EACH ROW EXECUTE FUNCTION reject_change();"
                )
            conn.execute("".join(body))
        conn.execute("CREATE VIEW parent_labels AS SELECT label FROM parent")
        assert _objects(conn) > limit  # one transaction could not lock them all
    result = subprocess.run(  # noqa: S603  # nosec B603
        [
            PSQL,
            "-h",
            params["host"],
            "-p",
            params["port"],
            "-U",
            params["user"],
            "-d",
            name,
            "-tAq",
            "-v",
            "ON_ERROR_STOP=1",
            "-v",
            f"app={app}",
        ],
        stdin=SQL_FILE.open(),
        capture_output=True,
        text=True,
        env={"PGPASSWORD": params["password"], "PATH": "/usr/bin:/bin"},
        timeout=600,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    with psycopg.connect(dbname=name, autocommit=True, **params) as conn:
        assert _objects(conn) == 0
        owner, usage = conn.execute(
            "SELECT nspowner::regrole::text, has_schema_privilege(%s, 'public', 'USAGE') "
            "FROM pg_namespace WHERE nspname = 'public'",
            [app],
        ).fetchone()
        assert owner == params["user"] and usage is True
        assert not conn.execute(
            "SELECT 1 FROM pg_extension WHERE extname = 'btree_gist'"
        ).fetchone()
        # PUBLIC may not create in it again.
        assert not conn.execute(
            "SELECT has_schema_privilege('public', 'public', 'CREATE')"
        ).fetchone()[0]


def test_the_file_never_drops_the_schema_before_its_tables():
    text = SQL_FILE.read_text()
    assert text.index("FROM pg_tables WHERE schemaname = 'public'") < text.index(
        "DROP SCHEMA public CASCADE;"
    )
    assert text.count("\\gexec") == 3
    assert "BEGIN" not in text
    for script in ("reseed-demo.sh", "restore-portal.sh"):
        body = (Path("infra/poc") / script).read_text()
        assert (
            'psql_owner -v app="$APP_USER" >/dev/null <"$APP_DIR/infra/poc/empty-database.sql"'
            in body
        )
        assert "DROP SCHEMA public CASCADE;" not in body
