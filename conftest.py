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


# --- CI sharding (`pytest --shard K/N`) -------------------------------------------------
# Parallel CI jobs each run every Nth test (round robin over the collected
# order), so even the long cross-owner URL matrix is shared out. Locally,
# without the option, everything runs.


def pytest_addoption(parser):
    parser.addoption(
        "--shard",
        default="",
        help="Run only shard K of N (K/N, 0-based), e.g. --shard 1/4 (CI).",
    )


def shard_items(items, k: int, n: int):
    """The items of shard k of n: every nth item starting at k."""
    return [item for i, item in enumerate(items) if i % n == k]


def parse_shard(value: str) -> tuple[int, int] | None:
    if not value:
        return None
    k, _, n = value.partition("/")
    if not (k.isdigit() and n.isdigit()) or not 0 <= int(k) < int(n):
        raise pytest.UsageError("--shard must be K/N with 0 <= K < N, e.g. 0/4")
    return int(k), int(n)


def pytest_collection_modifyitems(config, items):
    shard = parse_shard(config.getoption("--shard"))
    if shard is None:
        return
    keep = shard_items(items, *shard)
    deselected = [item for item in items if item not in keep]
    if deselected:
        config.hook.pytest_deselected(items=deselected)
    items[:] = keep
