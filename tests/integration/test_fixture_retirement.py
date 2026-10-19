"""A finished case releases only the database its real fixture created."""
from __future__ import annotations

import uuid

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

from tests.conftest import blank_dsn as database_fixture


def test_finished_fixture_retires_its_database_and_preserves_another(admin_dsn, request):
    sentinel = "fixture_sentinel_" + uuid.uuid4().hex
    generated = database_fixture.__wrapped__(admin_dsn, request)
    name = None
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(sentinel)))
    try:
        created = next(generated)
        name = conninfo_to_dict(created)["dbname"]
        with psycopg.connect(created) as conn:
            assert conn.execute("SELECT current_database()").fetchone()[0] == name
        generated.close()
        with psycopg.connect(admin_dsn) as conn:
            assert conn.execute("SELECT datname FROM pg_database WHERE datname = %s", (name,)).fetchone() is None
            preserved = conn.execute("SELECT datname FROM pg_database WHERE datname = %s", (sentinel,)).fetchone()
            assert preserved == (sentinel,)
    finally:
        generated.close()
        with psycopg.connect(admin_dsn, autocommit=True) as conn:
            for owned in (name, sentinel):
                if owned:
                    conn.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(owned)))
