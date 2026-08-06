"""The schema_migrations checksum ledger (B1a, ORG-191) — fail closed, loudly.

The scenario this whole file guards: two builds each carry a DIFFERENT
definition of the same migration version (the ORG-PLAN-155/164 "schema-v2
fork"). ``schema_meta.version`` is a number and cannot tell the shapes apart;
the ledger records each applied migration's sha256 and refuses — at migrate
time AND at application startup — to proceed over a history this build did not
write. Every test runs against a real PostgreSQL because the protection lives
in transaction/DDL behaviour a mock cannot reproduce.
"""

from __future__ import annotations

import itertools
import os

import psycopg
import pytest

from workflow_runtime_core import registry
from workflow_runtime_core.exceptions import MigrationChecksumMismatch
from workflow_runtime_core.migrations import (
    CORE_NAMESPACE,
    LATEST_VERSION,
    MIGRATIONS,
    Migration,
    apply_app_migrations,
    apply_migrations,
    ledger_exists,
    read_app_version,
    verify_ledger,
)
from workflow_runtime_core.schema import require_compatible_schema

_DSN_ENV = "STROMY_PG_DSN"
_counter = itertools.count(1)


@pytest.fixture(scope="module")
def admin_dsn():
    """A reachable PostgreSQL with permission to CREATE DATABASE."""
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


@pytest.fixture
def blank_dsn(admin_dsn: str):
    """A brand-new empty database per test, so tests cannot order-couple."""
    name = f"ledger_test_{next(_counter)}"
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    head, _, _tail = admin_dsn.rpartition("/")
    yield f"{head}/{name}"


def _tamper_checksum(dsn: str, namespace: str, version: int, value: str) -> None:
    with registry.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE schema_migrations SET checksum = %s "
            "WHERE namespace = %s AND version = %s",
            (value, namespace, version),
        )


@pytest.mark.integration
def test_migrate_records_the_ledger_row(blank_dsn: str) -> None:
    with registry.connect(blank_dsn) as conn:
        apply_migrations(conn)
    with registry.connect(blank_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT namespace, version, name, checksum FROM schema_migrations "
            "ORDER BY version"
        )
        rows = cur.fetchall()
    assert len(rows) == len(MIGRATIONS)
    for row, migration in zip(rows, MIGRATIONS, strict=True):
        assert row["namespace"] == CORE_NAMESPACE
        assert row["version"] == migration.version
        assert row["name"] == migration.name
        assert row["checksum"] == migration.checksum


@pytest.mark.integration
def test_startup_gate_fails_closed_on_a_tampered_checksum(blank_dsn: str) -> None:
    """A recorded history this build did not write must stop the application."""
    with registry.connect(blank_dsn) as conn:
        apply_migrations(conn)
    _tamper_checksum(blank_dsn, CORE_NAMESPACE, 1, "0" * 64)
    with registry.connect(blank_dsn) as conn:
        with pytest.raises(MigrationChecksumMismatch, match="DIFFERENT definition"):
            require_compatible_schema(conn)


@pytest.mark.integration
def test_migrator_refuses_to_run_over_a_divergent_history(blank_dsn: str) -> None:
    """The mismatch stops the MIGRATOR too — before it creates or alters anything."""
    with registry.connect(blank_dsn) as conn:
        apply_migrations(conn)
    _tamper_checksum(blank_dsn, CORE_NAMESPACE, 1, "0" * 64)
    with registry.connect(blank_dsn) as conn:
        with pytest.raises(MigrationChecksumMismatch):
            apply_migrations(conn)


@pytest.mark.integration
def test_two_shapes_one_number_is_detected(blank_dsn: str) -> None:
    """THE fork scenario. Build A applied its v2; build B defines v2 differently.

    Neither shape needs to really exist in this package yet — the ledger compares
    content digests, so the test simulates build A's applied history and asserts
    build B's verification refuses it. This is exactly what ``schema_meta.version``
    alone could never detect.
    """
    with registry.connect(blank_dsn) as conn:
        apply_migrations(conn)
        with conn.cursor() as cur:
            # Build A applied ITS v2 and recorded it.
            cur.execute("ALTER TABLE runs ADD COLUMN lease_until TIMESTAMPTZ")
            cur.execute(
                "INSERT INTO schema_migrations (namespace, version, name, checksum) "
                "VALUES (%s, 2, 'data_plane_v2_shape_a', %s)",
                (CORE_NAMESPACE, "a" * 64),
            )
            cur.execute("UPDATE schema_meta SET version = 2")

    build_b_chain = (
        *MIGRATIONS,
        Migration(version=2, name="data_plane_v2_shape_b", sql="SELECT 'different'"),
    )
    with registry.connect(blank_dsn) as conn:
        with pytest.raises(MigrationChecksumMismatch, match="v2"):
            verify_ledger(conn, migrations=build_b_chain, applied_through=2)


@pytest.mark.integration
def test_unaccounted_v2_history_fails_closed(blank_dsn: str) -> None:
    """A v2 with NO ledger row means a ledger-less build applied it — refuse.

    This is the guard that makes landing the ledger FIRST meaningful: the
    pre-reconciliation ORG-PLAN-164 branch carried a v2 and no ledger, and this
    is exactly the state its migration would have left behind.
    """
    with registry.connect(blank_dsn) as conn:
        apply_migrations(conn)
        with conn.cursor() as cur:
            cur.execute("UPDATE schema_meta SET version = 2")

    with registry.connect(blank_dsn) as conn:
        with pytest.raises(MigrationChecksumMismatch, match="unaccounted"):
            require_compatible_schema(conn, maximum=2)


@pytest.mark.integration
def test_pre_ledger_database_is_tolerated_and_backfilled(blank_dsn: str) -> None:
    """A healthy v1 database migrated before the ledger existed must keep working.

    Startup tolerates the absent ledger (amnesty is bounded to the pre-ledger
    era), and the next migrate backfills the missing rows instead of treating
    them as corruption.
    """
    with registry.connect(blank_dsn) as conn:
        apply_migrations(conn)
        with conn.cursor() as cur:
            cur.execute("DROP TABLE schema_migrations")

    with registry.connect(blank_dsn) as conn:
        assert not ledger_exists(conn)
        assert require_compatible_schema(conn) == LATEST_VERSION  # amnesty
        assert apply_migrations(conn) == LATEST_VERSION  # no-op apply backfills

    with registry.connect(blank_dsn) as conn:
        assert ledger_exists(conn)
        verify_ledger(conn, applied_through=LATEST_VERSION)
        assert require_compatible_schema(conn) == LATEST_VERSION


@pytest.mark.integration
def test_app_chain_applies_records_and_is_idempotent(blank_dsn: str) -> None:
    chain = (
        Migration(
            version=1,
            name="upload_sessions",
            sql="CREATE TABLE IF NOT EXISTS facade_upload_sessions (id UUID PRIMARY KEY)",
        ),
    )
    with registry.connect(blank_dsn) as conn:
        apply_migrations(conn)
        assert read_app_version(conn, "facade") is None
        assert apply_app_migrations(conn, "facade", chain) == 1
        assert apply_app_migrations(conn, "facade", chain) == 1  # idempotent
        assert read_app_version(conn, "facade") == 1
        # The app chain never touches the core version indicator.
        assert require_compatible_schema(conn) == LATEST_VERSION


@pytest.mark.integration
def test_app_chain_fails_closed_on_a_divergent_definition(blank_dsn: str) -> None:
    chain_a = (Migration(version=1, name="s", sql="CREATE TABLE t_a (id INT)"),)
    chain_b = (Migration(version=1, name="s", sql="CREATE TABLE t_b (id INT)"),)
    with registry.connect(blank_dsn) as conn:
        apply_migrations(conn)
        apply_app_migrations(conn, "facade", chain_a)
    with registry.connect(blank_dsn) as conn:
        with pytest.raises(MigrationChecksumMismatch):
            apply_app_migrations(conn, "facade", chain_b)


@pytest.mark.integration
def test_app_namespaces_are_isolated_from_each_other(blank_dsn: str) -> None:
    chain_x = (Migration(version=1, name="x", sql="CREATE TABLE app_x (id INT)"),)
    chain_y = (Migration(version=1, name="y", sql="CREATE TABLE app_y (id INT)"),)
    with registry.connect(blank_dsn) as conn:
        apply_migrations(conn)
        assert apply_app_migrations(conn, "app-x", chain_x) == 1
        assert apply_app_migrations(conn, "app-y", chain_y) == 1
        assert read_app_version(conn, "app-x") == 1
        assert read_app_version(conn, "app-y") == 1


@pytest.mark.integration
def test_fresh_database_flow_is_unchanged(blank_dsn: str) -> None:
    """The ledger must not regress the v0.1.1 fresh-database fix: probing and
    migrating an empty database still works end-to-end, including the DML."""
    with registry.connect(blank_dsn) as conn:
        assert not ledger_exists(conn)
        verify_ledger(conn)  # absent ledger, nothing applied — tolerated
        assert apply_migrations(conn) == LATEST_VERSION
        run = registry.create_run(conn, workflow="demo", config={})
        assert registry.claim_run(conn, run.run_id) is not None
