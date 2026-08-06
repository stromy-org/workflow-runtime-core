"""Migration-ledger unit tests (no database required).

The database-backed checksum-mismatch and serialisation behaviour lives in
``tests/integration/test_migration_ledger.py``. What is testable here is the
part that must never drift silently: the set of known migrations, the pending
calculation that decides whether an operator has work to do, the refusal to
touch a database newer than this build, and the app-chain input validation
that rejects bad namespaces/version sequences before any SQL runs.
"""

from __future__ import annotations

import pytest

from workflow_runtime_core.exceptions import MigrationError
from workflow_runtime_core.migrations import (
    LATEST_VERSION,
    MIGRATIONS,
    PRE_LEDGER_MAX_VERSION,
    Migration,
    apply_app_migrations,
    get_migration,
    pending,
)
from workflow_runtime_core.schema import SCHEMA_VERSION, SUPPORTED_SCHEMA_MAX


@pytest.mark.unit
def test_versions_are_contiguous_and_start_at_one() -> None:
    versions = [m.version for m in MIGRATIONS]
    assert versions == list(range(1, len(versions) + 1)), (
        "migration versions must be contiguous from 1 — a gap makes `pending` "
        "silently skip a step"
    )


@pytest.mark.unit
def test_latest_version_matches_the_default_migrate_target() -> None:
    assert LATEST_VERSION == SCHEMA_VERSION


@pytest.mark.unit
def test_supported_ceiling_never_exceeds_what_we_can_create() -> None:
    """A build must not claim to read a version it cannot produce.

    During expand/migrate/contract the ceiling may legitimately EXCEED the
    default create target; it may never be the other way round, which would mean
    `wrc migrate` produces a schema the same build then refuses to serve.
    """
    assert SUPPORTED_SCHEMA_MAX >= SCHEMA_VERSION


@pytest.mark.unit
def test_pending_from_unmigrated_is_everything() -> None:
    assert pending(None) == MIGRATIONS


@pytest.mark.unit
def test_pending_from_current_is_empty() -> None:
    assert pending(LATEST_VERSION) == ()


@pytest.mark.unit
def test_pending_from_a_future_version_is_empty() -> None:
    """A database ahead of this build has nothing pending — apply_migrations
    raises instead, rather than us inventing a downgrade."""
    assert pending(LATEST_VERSION + 5) == ()


@pytest.mark.unit
def test_get_migration_rejects_an_unknown_version() -> None:
    with pytest.raises(MigrationError, match="no migration 99"):
        get_migration(99)


@pytest.mark.unit
def test_checksum_is_stable_and_content_addressed() -> None:
    a = Migration(version=1, name="x", sql="SELECT 1;")
    b = Migration(version=2, name="y", sql="SELECT 1;")
    c = Migration(version=1, name="x", sql="SELECT 2;")
    assert a.checksum == b.checksum, "checksum must cover SQL only"
    assert a.checksum != c.checksum, "different SQL must produce a different checksum"


@pytest.mark.unit
def test_v1_ddl_creates_the_tables_the_registry_writes() -> None:
    sql = get_migration(1).sql
    for table in ("schema_meta", "runs", "run_events"):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in sql


@pytest.mark.unit
def test_pre_ledger_era_is_pinned_to_v1() -> None:
    """The amnesty boundary marks the pre-ledger ERA and never moves.

    If this constant ever tracked "the current version", every future migration
    would grant itself amnesty from the ledger — which is the entire protection.
    """
    assert PRE_LEDGER_MAX_VERSION == 1


@pytest.mark.unit
def test_app_chain_rejects_the_core_namespace() -> None:
    chain = (Migration(version=1, name="x", sql="SELECT 1"),)
    with pytest.raises(MigrationError, match="invalid app namespace"):
        apply_app_migrations(None, "core", chain)  # type: ignore[arg-type]


@pytest.mark.unit
@pytest.mark.parametrize("bad", ["", "Core", "UPPER", "1starts-with-digit", "a b", "x" * 65])
def test_app_chain_rejects_malformed_namespaces(bad: str) -> None:
    chain = (Migration(version=1, name="x", sql="SELECT 1"),)
    with pytest.raises(MigrationError, match="invalid app namespace"):
        apply_app_migrations(None, bad, chain)  # type: ignore[arg-type]


@pytest.mark.unit
@pytest.mark.parametrize(
    "versions",
    [(), (2,), (1, 3), (1, 1), (2, 1)],
    ids=["empty", "starts-at-2", "gap", "duplicate", "descending"],
)
def test_app_chain_rejects_non_contiguous_versions(versions: tuple[int, ...]) -> None:
    chain = tuple(Migration(version=v, name=f"m{i}", sql="SELECT 1") for i, v in enumerate(versions))
    with pytest.raises(MigrationError, match="contiguous from 1"):
        apply_app_migrations(None, "facade", chain)  # type: ignore[arg-type]


@pytest.mark.unit
def test_v1_ddl_keeps_the_partial_idempotency_index() -> None:
    """Partial, not total: the unique constraint must apply only to non-NULL keys,
    or a second run without an idempotency key would collide with the first."""
    sql = get_migration(1).sql
    assert "runs_idempotency_key_uniq" in sql
    assert "WHERE idempotency_key IS NOT NULL" in sql
