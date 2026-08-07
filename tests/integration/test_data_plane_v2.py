"""Schema v2 — the workflow data plane, ported from ORG-PLAN-164 WS2/WS4.

Two families of guarantees, both against a real PostgreSQL:

1. **v2 semantics** — dispatch claims are single-writer, leases recover crashes,
   lineage is per-attempt, and completion records publication atomically.
2. **The expansion window** — this same build serving a *v1* database keeps the
   v1 lifecycle working and fails v2-only calls with the NAMED schema error,
   never a KeyError/UndefinedColumn in a background worker. That failure shape
   is what the 2026-08-03 fork analysis documented as the silent-collision
   surface, so it is pinned here explicitly.
"""

from __future__ import annotations

import uuid

import pytest

from workflow_runtime_core import registry
from workflow_runtime_core.exceptions import (
    ActiveAttemptExists,
    SchemaVersionMismatch,
)
from workflow_runtime_core.migrations import (
    CORE_NAMESPACE,
    LATEST_VERSION,
    apply_migrations,
)
from workflow_runtime_core.schema import require_compatible_schema


def _migrated(dsn: str, *, target: int | None = None) -> None:
    with registry.connect(dsn) as conn:
        apply_migrations(conn, target=target)


# --- v2 semantics -------------------------------------------------------------


@pytest.mark.integration
def test_fresh_migrate_produces_the_latest_with_a_full_ledger(blank_dsn: str) -> None:
    """Stated against ``LATEST_VERSION`` rather than a hard-coded number, so
    adding a migration does not require editing an assertion that was never
    about *which* version — it is about the ledger being complete and the
    result being servable."""
    with registry.connect(blank_dsn) as conn:
        assert apply_migrations(conn) == LATEST_VERSION
        assert require_compatible_schema(conn) == LATEST_VERSION
        with conn.cursor() as cur:
            cur.execute(
                "SELECT version FROM schema_migrations WHERE namespace = %s "
                "ORDER BY version",
                (CORE_NAMESPACE,),
            )
            versions = [r["version"] for r in cur.fetchall()]
    assert versions == list(range(1, LATEST_VERSION + 1))


@pytest.mark.integration
def test_v1_to_v2_upgrade_backfills_workspace_from_run_id(blank_dsn: str) -> None:
    """Every legacy run becomes its own workspace — which it already was.

    Pinned at ``target=2`` deliberately: this test is about the v1→v2 step, so
    letting it drift to whatever the latest version happens to be would quietly
    stop testing the backfill it is named for.
    """
    _migrated(blank_dsn, target=1)
    with registry.connect(blank_dsn) as conn:
        legacy = registry.create_run(conn, workflow="demo", config={})
    with registry.connect(blank_dsn) as conn:
        assert apply_migrations(conn, target=2) == 2
    with registry.connect(blank_dsn) as conn:
        upgraded = registry.get_run(conn, legacy.run_id)
    assert upgraded is not None
    assert upgraded.workspace_id == legacy.run_id
    assert upgraded.attempt_no == 1


@pytest.mark.integration
def test_claim_dispatch_is_single_writer(blank_dsn: str) -> None:
    """Only the matching, unleased, queued dispatch claims; everything else
    returns None so the caller deletes the message and exits cleanly."""
    _migrated(blank_dsn)
    dispatch = str(uuid.uuid4())
    with registry.connect(blank_dsn) as conn:
        run = registry.create_run(conn, workflow="demo", config={})
        registry.set_dispatch(conn, run.run_id, dispatch)

        # Wrong dispatch id: a stale message from a prior enqueue of this run.
        assert (
            registry.claim_dispatch(
                conn,
                run_id=run.run_id,
                dispatch_id=str(uuid.uuid4()),
                owner="w1",
                lease_seconds=60,
            )
            is None
        )

        claimed = registry.claim_dispatch(
            conn, run_id=run.run_id, dispatch_id=dispatch, owner="w1", lease_seconds=60
        )
        assert claimed is not None
        assert claimed.status.value == "running"
        assert claimed.lease_owner == "w1"
        assert claimed.delivery_count == 1

        # Redelivery of the same message while the run is RUNNING: no second writer.
        assert (
            registry.claim_dispatch(
                conn, run_id=run.run_id, dispatch_id=dispatch, owner="w2", lease_seconds=60
            )
            is None
        )


@pytest.mark.integration
def test_lease_renewal_is_owner_scoped(blank_dsn: str) -> None:
    _migrated(blank_dsn)
    dispatch = str(uuid.uuid4())
    with registry.connect(blank_dsn) as conn:
        run = registry.create_run(conn, workflow="demo", config={})
        registry.set_dispatch(conn, run.run_id, dispatch)
        registry.claim_dispatch(
            conn, run_id=run.run_id, dispatch_id=dispatch, owner="w1", lease_seconds=60
        )
        assert registry.renew_lease(conn, run_id=run.run_id, owner="w1", lease_seconds=60)
        # A worker that is NOT the lease owner must learn it lost, and stop.
        assert not registry.renew_lease(conn, run_id=run.run_id, owner="w2", lease_seconds=60)


@pytest.mark.integration
def test_expired_lease_requeues_and_allows_recovery_claim(blank_dsn: str) -> None:
    """Crash recovery: a lapsed lease returns the run to queued, and only then
    can a replacement worker claim the redelivered message."""
    _migrated(blank_dsn)
    dispatch = str(uuid.uuid4())
    with registry.connect(blank_dsn) as conn:
        run = registry.create_run(conn, workflow="demo", config={})
        registry.set_dispatch(conn, run.run_id, dispatch)
        # A negative lease is already expired the moment it is granted — the
        # deterministic stand-in for "the worker died and time passed".
        registry.claim_dispatch(
            conn, run_id=run.run_id, dispatch_id=dispatch, owner="dead", lease_seconds=-1
        )
        assert registry.requeue_expired_lease(conn, run.run_id)
        recovered = registry.claim_dispatch(
            conn, run_id=run.run_id, dispatch_id=dispatch, owner="w2", lease_seconds=60
        )
        assert recovered is not None
        assert recovered.lease_owner == "w2"
        assert recovered.delivery_count == 2
        # An active (non-expired) lease must NOT requeue.
        assert not registry.requeue_expired_lease(conn, run.run_id)


@pytest.mark.integration
def test_one_live_attempt_per_workspace(blank_dsn: str) -> None:
    _migrated(blank_dsn)
    with registry.connect(blank_dsn) as conn:
        first = registry.create_run(conn, workflow="demo", config={})
        with pytest.raises(ActiveAttemptExists, match="at most one live attempt"):
            registry.create_run(
                conn,
                workflow="demo",
                config={},
                workspace_id=first.workspace_id,
                retry_of=first.run_id,
                attempt_no=2,
            )


@pytest.mark.integration
def test_retry_lineage_after_failure(blank_dsn: str) -> None:
    """A failed attempt frees the workspace; the retry records its ancestry."""
    _migrated(blank_dsn)
    with registry.connect(blank_dsn) as conn:
        first = registry.create_run(conn, workflow="demo", config={})
        registry.mark_failed_structured(
            conn,
            first.run_id,
            {"stage": "graph", "error_type": "Boom", "message": "x", "retryable": True},
        )
        retry = registry.create_run(
            conn,
            workflow="demo",
            config={},
            workspace_id=first.workspace_id,
            retry_of=first.run_id,
            attempt_no=first.attempt_no + 1,
        )
    assert retry.workspace_id == first.workspace_id
    assert retry.retry_of == first.run_id
    assert retry.attempt_no == 2
    assert retry.run_id != first.run_id


@pytest.mark.integration
def test_completion_records_publication_atomically(blank_dsn: str) -> None:
    """Status, descriptors and the publication stamp land in ONE statement, and
    the lease is cleared with them."""
    _migrated(blank_dsn)
    dispatch = str(uuid.uuid4())
    with registry.connect(blank_dsn) as conn:
        run = registry.create_run(conn, workflow="demo", config={})
        registry.set_dispatch(conn, run.run_id, dispatch)
        registry.claim_dispatch(
            conn, run_id=run.run_id, dispatch_id=dispatch, owner="w1", lease_seconds=60
        )
        registry.mark_completed(
            conn,
            run.run_id,
            {"published": [{"artifact_id": "report_pdf", "sha256": "ab" * 32}]},
            artifacts_published=True,
        )
        final = registry.get_run(conn, run.run_id)
    assert final is not None
    assert final.status.value == "completed"
    assert final.artifacts_published_at is not None
    assert final.lease_owner is None
    assert final.lease_expires_at is None


@pytest.mark.integration
def test_structured_failure_clears_the_lease(blank_dsn: str) -> None:
    _migrated(blank_dsn)
    dispatch = str(uuid.uuid4())
    with registry.connect(blank_dsn) as conn:
        run = registry.create_run(conn, workflow="demo", config={})
        registry.set_dispatch(conn, run.run_id, dispatch)
        registry.claim_dispatch(
            conn, run_id=run.run_id, dispatch_id=dispatch, owner="w1", lease_seconds=60
        )
        registry.mark_failed_structured(
            conn,
            run.run_id,
            {
                "stage": "artifacts",
                "error_type": "ArtifactPublicationFailed",
                "message": "blob write failed",
                "retryable": True,
                "correlation_id": run.run_id,
            },
        )
        final = registry.get_run(conn, run.run_id)
    assert final is not None
    assert final.status.value == "failed"
    assert final.error_json is not None
    assert final.error_json["retryable"] is True
    assert final.lease_owner is None


@pytest.mark.integration
def test_an_input_set_binds_to_its_run(blank_dsn: str) -> None:
    """The facade owns the upload tables; the core owns this column. A consumer
    reaching across to update it with its own SQL is the coupling the ownership
    rule prevents."""
    _migrated(blank_dsn)
    input_set = str(uuid.uuid4())
    with registry.connect(blank_dsn) as conn:
        run = registry.create_run(conn, workflow="demo", config={})
        registry.set_input_set(conn, run.run_id, input_set)
        bound = registry.get_run(conn, run.run_id)
    assert bound is not None
    assert bound.input_set_id == input_set


@pytest.mark.integration
def test_progress_snapshot_round_trips(blank_dsn: str) -> None:
    _migrated(blank_dsn)
    with registry.connect(blank_dsn) as conn:
        run = registry.create_run(conn, workflow="demo", config={})
        registry.record_progress(
            conn, run.run_id, {"current_node": "score", "completed_nodes": ["load"]}
        )
        row = registry.get_run(conn, run.run_id)
    assert row is not None
    assert row.progress_json == {"current_node": "score", "completed_nodes": ["load"]}
    assert row.heartbeat_at is not None


# --- the expansion window: this build against a v1 database -------------------


@pytest.mark.integration
def test_v1_lifecycle_still_works_under_the_v2_build(blank_dsn: str) -> None:
    """The shared production registry stays at v1 during the window; the whole
    Phase-A lifecycle must keep working through this build unchanged."""
    _migrated(blank_dsn, target=1)
    with registry.connect(blank_dsn) as conn:
        assert require_compatible_schema(conn) == 1
        run = registry.create_run(conn, workflow="demo", config={"a": 1})
        assert registry.claim_run(conn, run.run_id) is not None
        registry.mark_paused(conn, run.run_id, {"question": "?"})
        registry.request_resume(conn, run.run_id, {"answer": "!"})
        assert registry.claim_run(conn, run.run_id) is not None
        registry.mark_completed(conn, run.run_id, {"artifacts": {"x": 1}})
        final = registry.get_run(conn, run.run_id)
    assert final is not None
    assert final.status.value == "completed"
    # v2 fields read as their defaults through the tolerant reader.
    assert final.lease_owner is None
    assert final.attempt_no == 1


@pytest.mark.integration
def test_v2_only_calls_fail_with_the_named_error_on_v1(blank_dsn: str) -> None:
    """The fork analysis's exact complaint: v2-only paths on a v1 database must
    raise a NAMED error, not a KeyError in a worker after startup went green."""
    _migrated(blank_dsn, target=1)
    with registry.connect(blank_dsn) as conn:
        run = registry.create_run(conn, workflow="demo", config={})

        with pytest.raises(SchemaVersionMismatch, match="requires schema v2"):
            registry.claim_dispatch(
                conn,
                run_id=run.run_id,
                dispatch_id=str(uuid.uuid4()),
                owner="w1",
                lease_seconds=60,
            )
        with pytest.raises(SchemaVersionMismatch, match="requires schema v2"):
            registry.set_dispatch(conn, run.run_id, str(uuid.uuid4()))
        with pytest.raises(SchemaVersionMismatch, match="requires schema v2"):
            registry.set_input_set(conn, run.run_id, str(uuid.uuid4()))
        with pytest.raises(SchemaVersionMismatch, match="requires schema v2"):
            registry.renew_lease(conn, run_id=run.run_id, owner="w1", lease_seconds=60)
        with pytest.raises(SchemaVersionMismatch, match="requires schema v2"):
            registry.record_progress(conn, run.run_id, {"current_node": "x"})
        with pytest.raises(SchemaVersionMismatch, match="lineage requires schema v2"):
            registry.create_run(
                conn, workflow="demo", config={}, workspace_id=str(uuid.uuid4())
            )
        with pytest.raises(SchemaVersionMismatch, match="artifacts_published"):
            registry.mark_completed(conn, run.run_id, None, artifacts_published=True)

        # And the connection is still usable after every refusal — the guards
        # must not leave the transaction poisoned.
        assert registry.get_run(conn, run.run_id) is not None
