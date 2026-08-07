"""Migrating a FRESH database — the case a naive probe silently breaks.

Regression guard for the bug this package shipped for exactly one tag: the
"was it ever migrated?" probe selects from ``schema_meta``, which on a fresh
database raises ``UndefinedTable``. Catching that exception is not enough —
Postgres has already aborted the whole transaction, and since
``apply_migrations`` runs the probe while holding its advisory lock in an open
transaction, every subsequent DDL statement failed with
``InFailedSqlTransaction``.

The durable fix is not "remember the savepoint" but "never ask the database a
question by triggering an error": ``read_schema_version`` resolves the table
through ``to_regclass``, which returns NULL instead of raising.
``test_probe_never_puts_the_connection_in_error`` pins that property directly —
it asserts the connection never enters ``INERROR``, so re-introducing an EAFP
probe fails here even if a savepoint would have masked it.

The failure only appears against a real database (a mock returning ``None``
reproduces nothing), and only on the fresh-database path — which is the first
thing every new deployment does and the one thing an existing one never does.
"""

from __future__ import annotations

import uuid

import pytest

from workflow_runtime_core import registry
from workflow_runtime_core.migrations import LATEST_VERSION, apply_migrations
from workflow_runtime_core.schema import read_schema_version, require_compatible_schema


@pytest.fixture(scope="module")
def fresh_dsn(admin_dsn: str):
    """An EMPTY database — genuinely empty, not merely assumed to be.

    It creates its own database rather than handing back ``$STROMY_PG_DSN``.
    The earlier version returned the provided DSN untouched, so every assertion
    here ("unmigrated", "no schema_meta") silently depended on that server
    having never been used — true for a throwaway testcontainer, false the
    moment anyone runs the suite against a persistent local PostgreSQL, which
    the conftest documents as supported. The tests then fail claiming the probe
    is broken when the only thing wrong is the fixture.
    """
    import psycopg

    name = f"fresh_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}"')  # noqa: S608 - generated name
        conn.execute(f'CREATE DATABASE "{name}"')  # noqa: S608 - generated name
    head, _, _tail = admin_dsn.rpartition("/")
    yield f"{head}/{name}"


@pytest.mark.integration
def test_probe_on_a_fresh_database_leaves_the_transaction_usable(fresh_dsn: str) -> None:
    """The exact regression: probe, then keep using the same transaction."""
    with registry.connect(fresh_dsn) as conn:
        assert read_schema_version(conn) is None
        # If the probe aborted the transaction, this raises InFailedSqlTransaction.
        with conn.cursor() as cur:
            cur.execute("SELECT 1 AS ok")
            assert cur.fetchone()["ok"] == 1


@pytest.mark.integration
def test_probe_never_puts_the_connection_in_error(fresh_dsn: str) -> None:
    """The probe must be a TOTAL function, not a caught exception.

    A savepoint-wrapped EAFP probe would still pass the test above, because the
    savepoint rollback repairs the damage. This asserts the stronger property the
    fix actually buys: no failing statement is ever issued, so the connection
    never transits through ``INERROR``. That is what makes the probe safe to call
    from anywhere — including code that holds a lock it must not lose.
    """
    from psycopg.pq import TransactionStatus

    with registry.connect(fresh_dsn) as conn:
        with conn.cursor() as cur:  # force an open transaction
            cur.execute("SELECT 1")
        assert read_schema_version(conn) is None
        assert conn.info.transaction_status != TransactionStatus.INERROR


@pytest.mark.integration
def test_migrate_creates_the_schema_from_empty(fresh_dsn: str) -> None:
    with registry.connect(fresh_dsn) as conn:
        assert apply_migrations(conn) == LATEST_VERSION
    with registry.connect(fresh_dsn) as conn:
        assert read_schema_version(conn) == LATEST_VERSION
        assert require_compatible_schema(conn) == LATEST_VERSION


@pytest.mark.integration
def test_migrate_is_idempotent(fresh_dsn: str) -> None:
    with registry.connect(fresh_dsn) as conn:
        assert apply_migrations(conn) == LATEST_VERSION
        assert apply_migrations(conn) == LATEST_VERSION


@pytest.mark.integration
def test_registry_round_trips_after_migrating(fresh_dsn: str) -> None:
    """Proves the migration produced a schema the DML actually works against."""
    with registry.connect(fresh_dsn) as conn:
        apply_migrations(conn)
        run = registry.create_run(conn, workflow="demo", config={"a": 1})
        claimed = registry.claim_run(conn, run.run_id)
        assert claimed is not None
        registry.mark_completed(conn, run.run_id, {"artifacts": {"x": 1}})
        final = registry.get_run(conn, run.run_id)
    assert final is not None
    assert final.status.value == "completed"
