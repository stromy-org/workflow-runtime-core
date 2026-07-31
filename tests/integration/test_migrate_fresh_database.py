"""Migrating a FRESH database — the case a naive probe silently breaks.

Regression guard for the bug this package shipped for exactly one tag: the
"was it ever migrated?" probe selects from ``schema_meta``, which on a fresh
database raises ``UndefinedTable``. Catching that exception is not enough —
Postgres has already aborted the whole transaction, and since
``apply_migrations`` runs the probe while holding its advisory lock in an open
transaction, every subsequent DDL statement failed with
``InFailedSqlTransaction``. The probe must roll back a SAVEPOINT, not just
swallow the error.

The failure only appears against a real database (a mock returning ``None``
reproduces nothing), and only on the fresh-database path — which is the most
common path there is.
"""

from __future__ import annotations

import os

import pytest

from workflow_runtime_core import registry
from workflow_runtime_core.migrations import LATEST_VERSION, apply_migrations
from workflow_runtime_core.schema import read_schema_version, require_compatible_schema

_DSN_ENV = "STROMY_PG_DSN"


@pytest.fixture(scope="module")
def fresh_dsn():
    """An EMPTY database — deliberately not migrated by the fixture."""
    provided = os.environ.get(_DSN_ENV, "").strip()
    if provided:
        yield provided
        return
    testcontainers = pytest.importorskip(
        "testcontainers.postgres",
        reason=f"neither {_DSN_ENV} nor testcontainers is available",
    )
    with testcontainers.PostgresContainer("postgres:16-alpine") as pg:
        yield pg.get_connection_url().replace("postgresql+psycopg2://", "postgresql://")


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
