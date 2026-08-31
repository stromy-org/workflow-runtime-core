"""The grant manifests, asserted as a shape rather than trusted as a list.

The security property this file protects is one line long: **no migration
ledger is ever granted a write.** Everything else here exists to make that line
hard to break by accident — a ledger moved into the wrong tuple, a manifest that
classifies a table twice, a future privilege set that quietly grows TRUNCATE.
"""

from __future__ import annotations

import pytest

from workflow_runtime_core.grants import (
    CHECKPOINT_MANIFEST,
    CORE_MANIFEST,
    LEDGER_PRIVILEGES,
    OPERATIONAL_PRIVILEGES,
    GrantManifest,
    _statements,
)

pytestmark = pytest.mark.unit

ALL_MANIFESTS = [CORE_MANIFEST, CHECKPOINT_MANIFEST]


def test_ledgers_are_read_only_by_definition() -> None:
    assert LEDGER_PRIVILEGES == ("SELECT",)


def test_operational_privileges_exclude_truncate_and_schema_verbs() -> None:
    """TRUNCATE is not DML for this purpose.

    It is a table-wide delete that bypasses the row predicates every retention
    path is written against, so an application holding it can empty a table its
    own retention logic would have refused to. REFERENCES and TRIGGER are
    schema-shaped and belong to the migrator.
    """
    assert set(OPERATIONAL_PRIVILEGES) == {"SELECT", "INSERT", "UPDATE", "DELETE"}


@pytest.mark.parametrize("manifest", ALL_MANIFESTS, ids=lambda m: m.name)
def test_no_ledger_receives_a_write_grant(manifest: GrantManifest) -> None:
    """The assertion the whole module exists for.

    Rendered SQL is inspected rather than the tuples, because the tuples being
    right is not the claim — the claim is that what actually reaches the
    database grants a ledger nothing but SELECT.
    """
    statements = _statements(manifest, "app_role", "public")
    for table in manifest.ledger_tables:
        grants = [s for s in statements if s.startswith("GRANT") and f".{table} " in s]
        assert grants == [f"GRANT SELECT ON public.{table} TO app_role"], (
            f"{manifest.name}: ledger {table} must receive SELECT and nothing else"
        )


@pytest.mark.parametrize("manifest", ALL_MANIFESTS, ids=lambda m: m.name)
def test_every_ledger_is_revoked_before_it_is_granted(manifest: GrantManifest) -> None:
    """Order matters exactly once, and this is it.

    A re-run must NARROW a ledger that was widened by hand between applications.
    Granting SELECT without revoking first would leave the widening in place
    beside a redundant grant, and the reconciliation would report success.
    """
    statements = _statements(manifest, "app_role", "public")
    for table in manifest.ledger_tables:
        revoke_at = next(i for i, s in enumerate(statements) if s.startswith("REVOKE") and f".{table} " in s)
        grant_at = next(i for i, s in enumerate(statements) if s.startswith("GRANT") and f".{table} " in s)
        assert revoke_at < grant_at


@pytest.mark.parametrize("manifest", ALL_MANIFESTS, ids=lambda m: m.name)
def test_every_operational_table_receives_full_dml(manifest: GrantManifest) -> None:
    statements = _statements(manifest, "app_role", "public")
    for table in manifest.operational_tables:
        expected = f"GRANT {', '.join(OPERATIONAL_PRIVILEGES)} ON public.{table} TO app_role"
        assert expected in statements


def test_a_table_cannot_be_both_operational_and_ledger() -> None:
    """Refused at construction, not resolved at runtime.

    A table in both tuples would receive the ledger REVOKE and the operational
    GRANT, and which one won would depend on statement order — a security
    outcome decided by list position.
    """
    with pytest.raises(ValueError, match="both operational and ledger"):
        GrantManifest(
            name="broken",
            operational_tables=("schema_migrations",),
            ledger_tables=("schema_migrations",),
        )


def test_core_manifest_matches_the_chain_it_describes() -> None:
    """The manifest and the migrations must not drift apart.

    Read out of the migration SQL rather than restated, because a hand-copied
    list is exactly what goes stale — and a table missing from the manifest is
    silently inaccessible to the application rather than loudly broken.
    """
    import re

    from workflow_runtime_core.migrations import _LEDGER_DDL, MIGRATIONS

    # _LEDGER_DDL as well as the migrations: schema_migrations is created by the
    # migrator itself rather than by a numbered step, so a derivation that reads
    # only MIGRATIONS would omit the very table whose protection is the point.
    created = set(re.findall(r"CREATE TABLE IF NOT EXISTS\s+(\w+)", _LEDGER_DDL))
    for migration in MIGRATIONS:
        created |= set(re.findall(r"CREATE TABLE IF NOT EXISTS\s+(\w+)", migration.sql))

    assert created == set(CORE_MANIFEST.tables()), (
        "the core chain creates tables the grant manifest does not classify (or "
        "vice versa) — an unclassified table is inaccessible to the application"
    )


def test_checkpoint_manifest_treats_langgraph_ledger_as_a_ledger() -> None:
    assert "checkpoint_migrations" in CHECKPOINT_MANIFEST.ledger_tables
    assert "checkpoint_migrations" not in CHECKPOINT_MANIFEST.operational_tables


def test_role_name_is_validated_at_render_time() -> None:
    from workflow_runtime_core.auth import AuthConfigurationError
    from workflow_runtime_core.grants import reconcile

    with pytest.raises(AuthConfigurationError, match="plain SQL identifier"):
        reconcile(None, CORE_MANIFEST, application_role="app; DROP TABLE runs")  # type: ignore[arg-type]
