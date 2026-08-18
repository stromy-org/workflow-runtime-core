"""Schema v4 — the server-derived execution snapshot (ORG-PLAN-206 C5).

Two families, mirroring how v2 and v3 are covered:

1. **v4 semantics** — the snapshot is pinned once and is immutable for the life
   of the run, observations accumulate per attempt beside it without disturbing
   it, and only the allowlisted half reaches a client.
2. **The expansion window** — this same build serving a v3 database keeps the
   v3 lifecycle working and fails every v4-only call with the NAMED schema
   error, never an ``UndefinedColumn`` from inside a background worker.

The immutability tests are the point of the whole column. Requirements and
entitlements are editable by design, so a runner that recomputed "who pays"
per attempt would let an edit made between a failure and its retry silently
reprice the retry — against a client who approved neither price.
"""

from __future__ import annotations

import pytest

from workflow_runtime_core import registry
from workflow_runtime_core.exceptions import (
    ExecutionMetadataConflict,
    RegistryError,
    SchemaVersionMismatch,
)
from workflow_runtime_core.migrations import LATEST_VERSION, apply_migrations

_PINNED = {
    "credential_policy": "client",
    "credential_subject": {"kind": "client", "id": "acme-co"},
    "credentials": ["deepseek-api", "openai-api"],
    "model_registry_digest": "sha256:beef",
}


def _run_on(dsn: str, *, target: int | None = None) -> str:
    with registry.connect(dsn) as conn:
        apply_migrations(conn, target=target)
    with registry.connect(dsn) as conn:
        return registry.create_run(conn, workflow="demo", config={}).run_id


# --- 1. v4 semantics ----------------------------------------------------------


@pytest.mark.integration
def test_v4_is_the_latest_this_build_applies(blank_dsn: str) -> None:
    assert LATEST_VERSION == 4
    with registry.connect(blank_dsn) as conn:
        assert apply_migrations(conn) == 4


@pytest.mark.integration
def test_a_fresh_run_carries_no_snapshot(blank_dsn: str) -> None:
    """``None``, not ``{}``. "Nothing was decided" and "it was decided that
    nothing is needed" are opposite statements, and a client-mode run that read
    the second as the first would fall through to operator credentials."""
    run_id = _run_on(blank_dsn)
    with registry.connect(blank_dsn) as conn:
        assert registry.read_execution_metadata(conn, run_id) is None


@pytest.mark.integration
def test_pin_then_read_round_trips(blank_dsn: str) -> None:
    run_id = _run_on(blank_dsn)
    with registry.connect(blank_dsn) as conn:
        assert registry.pin_execution_metadata(conn, run_id, _PINNED) == _PINNED
    with registry.connect(blank_dsn) as conn:
        assert registry.read_execution_metadata(conn, run_id) == _PINNED


@pytest.mark.integration
def test_repinning_the_identical_snapshot_is_a_no_op(blank_dsn: str) -> None:
    """The create path is idempotency-keyed, so it can legitimately run twice."""
    run_id = _run_on(blank_dsn)
    with registry.connect(blank_dsn) as conn:
        registry.pin_execution_metadata(conn, run_id, _PINNED)
        assert registry.pin_execution_metadata(conn, run_id, dict(_PINNED)) == _PINNED


@pytest.mark.integration
def test_repinning_a_different_snapshot_is_refused(blank_dsn: str) -> None:
    """The retry-repricing case, stated directly: the entitlement flipped from
    ``client`` to ``operator`` between attempts, and the second attempt is
    refused rather than quietly billed to someone else."""
    run_id = _run_on(blank_dsn)
    with registry.connect(blank_dsn) as conn:
        registry.pin_execution_metadata(conn, run_id, _PINNED)
    reprice = {**_PINNED, "credential_policy": "operator"}
    with registry.connect(blank_dsn) as conn, pytest.raises(ExecutionMetadataConflict):
        registry.pin_execution_metadata(conn, run_id, reprice)
    with registry.connect(blank_dsn) as conn:
        assert registry.read_execution_metadata(conn, run_id) == _PINNED


@pytest.mark.integration
def test_pinning_an_unknown_run_is_a_named_error(blank_dsn: str) -> None:
    _run_on(blank_dsn)
    unknown = "00000000-0000-4000-8000-000000000000"
    with registry.connect(blank_dsn) as conn, pytest.raises(RegistryError, match="not found"):
        registry.pin_execution_metadata(conn, unknown, _PINNED)


@pytest.mark.integration
def test_observations_accumulate_per_attempt_without_touching_the_pin(
    blank_dsn: str,
) -> None:
    """Attempt 2 records its own account of what happened rather than
    overwriting attempt 1's — a retry re-reads its values, and the record of
    which key funded the FIRST attempt is not something a retry gets to edit."""
    run_id = _run_on(blank_dsn)
    with registry.connect(blank_dsn) as conn:
        registry.pin_execution_metadata(conn, run_id, _PINNED)
        registry.record_credential_sources(
            conn, run_id, {"openai-api": "client-registered"}, attempt_no=1
        )
        registry.record_credential_sources(
            conn, run_id, {"openai-api": "operator-env"}, attempt_no=2
        )
    with registry.connect(blank_dsn) as conn:
        assert registry.read_execution_metadata(conn, run_id) == _PINNED
        run = registry.get_run(conn, run_id)
    assert run is not None
    assert run.public()["execution"]["credential_sources"] == {
        "1": {"openai-api": "client-registered"},
        "2": {"openai-api": "operator-env"},
    }


@pytest.mark.integration
def test_sources_can_be_recorded_before_anything_is_pinned(blank_dsn: str) -> None:
    """The column starts NULL, so the merge has to create it rather than assume
    a pin came first. ``jsonb_set`` would silently no-op here."""
    run_id = _run_on(blank_dsn)
    with registry.connect(blank_dsn) as conn:
        registry.record_credential_sources(conn, run_id, {"apify-api": "operator-env"})
    with registry.connect(blank_dsn) as conn:
        run = registry.get_run(conn, run_id)
    assert run is not None
    assert run.public()["execution"]["credential_sources"] == {
        "1": {"apify-api": "operator-env"}
    }


@pytest.mark.integration
def test_a_non_string_source_is_refused_before_it_is_stored(blank_dsn: str) -> None:
    """The guard exists because this column IS projected to clients. A caller
    that passed the key itself instead of its label would otherwise publish it."""
    run_id = _run_on(blank_dsn)
    with registry.connect(blank_dsn) as conn, pytest.raises(RegistryError, match="never values"):
        registry.record_credential_sources(
            conn,
            run_id,
            {"openai-api": {"value": "sk-live"}},  # type: ignore[dict-item]
        )
    with registry.connect(blank_dsn) as conn:
        run = registry.get_run(conn, run_id)
    assert run is not None
    assert "execution" not in run.public()


@pytest.mark.integration
def test_the_public_projection_of_a_real_row_hides_the_pin(blank_dsn: str) -> None:
    """The end-to-end statement the unit test makes in isolation: a client
    polling this run sees the source labels and nothing about the policy."""
    run_id = _run_on(blank_dsn)
    with registry.connect(blank_dsn) as conn:
        registry.pin_execution_metadata(conn, run_id, _PINNED)
        registry.record_credential_sources(conn, run_id, {"openai-api": "client-registered"})
        run = registry.get_run(conn, run_id)
    assert run is not None
    rendered = str(run.public())
    assert "client-registered" in rendered
    assert "sha256:beef" not in rendered
    assert "credential_policy" not in rendered


# --- 2. consumer-owned events -------------------------------------------------


@pytest.mark.integration
def test_record_event_lands_on_the_run_timeline(blank_dsn: str) -> None:
    run_id = _run_on(blank_dsn)
    with registry.connect(blank_dsn) as conn:
        registry.record_event(conn, run_id, "credentials_resolved", {"count": 2})
        events = registry.list_events(conn, run_id)
    kinds = [e["kind"] for e in events]
    assert "created" in kinds
    assert "credentials_resolved" in kinds
    resolved = next(e for e in events if e["kind"] == "credentials_resolved")
    assert resolved["detail"] == {"count": 2}


# --- 3. the expansion window --------------------------------------------------


@pytest.mark.integration
def test_v3_keeps_working_and_v4_calls_fail_by_name(blank_dsn: str) -> None:
    """One test, both halves, because they are one property: a build that can
    read v4 must still SERVE v3 rather than half-serving it.

    The named error is the whole point. Without the translation these surface as
    ``UndefinedColumn`` from deep inside a worker, *after* the startup gate went
    green — the silent-collision shape the 2026-08-03 fork analysis documented.
    """
    run_id = _run_on(blank_dsn, target=3)

    with registry.connect(blank_dsn) as conn:
        # v3 lifecycle: unchanged.
        assert registry.claim_run(conn, run_id) is not None
        fetched = registry.get_run(conn, run_id)
    assert fetched is not None
    assert fetched.execution_metadata_json is None
    assert "execution" not in fetched.public()

    for call in (
        lambda conn: registry.pin_execution_metadata(conn, run_id, _PINNED),
        lambda conn: registry.read_execution_metadata(conn, run_id),
        lambda conn: registry.record_credential_sources(conn, run_id, {"a": "operator-env"}),
    ):
        with (
            registry.connect(blank_dsn) as conn,
            pytest.raises(SchemaVersionMismatch, match="requires schema v4"),
        ):
            call(conn)
