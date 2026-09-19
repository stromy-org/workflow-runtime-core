"""The pinned execution snapshot is lineage: it survives a retry (ORG-318).

``_execution_snapshot`` in the facade says the snapshot is "immutable for the
life of the run **and every attempt of it**", and ``pin_execution_metadata``
refuses a second, different snapshot for exactly that reason. Only the first
half was ever implemented: ``create_retry`` carried the workspace, the input set
and the configuration forward and dropped the snapshot, so every retry reached
the runner looking like a run created before the credential plane existed.

The consequence is not a missing field. An unpinned run binds nothing — no
scrub, no injection — so the attempt runs on whatever ambient credentials the job
carries. Measured on run ``7157f728``: attempts 2 and 3 recorded no
``credential_sources`` at any lifecycle point, while the providers were plainly
being paid by someone.

These run against a real engine because the claim is about what rows hold after
a retry, not about what the function meant to write.
"""

from __future__ import annotations

from typing import Any

import pytest

from workflow_runtime_core import registry
from workflow_runtime_core.exceptions import ExecutionMetadataConflict
from workflow_runtime_core.migrations import apply_migrations

_PINNED: dict[str, Any] = {
    "credential_policy": "client",
    "funding": {"deepseek-api": "client", "tavily-api": "operator"},
    "credentials": ["deepseek-api", "tavily-api"],
    "model_registry_digest": "sha256:beef",
}


def _failed_run(dsn: str, *, target: int | None = None) -> registry.RunRecord:
    with registry.connect(dsn) as conn:
        apply_migrations(conn, target=target)
    with registry.connect(dsn) as conn:
        run = registry.create_run(conn, workflow="demo", config={"depth": 1})
        registry.claim_run(conn, run.run_id)
        registry.mark_failed(conn, run.run_id, "node blew up")
        settled = registry.get_run(conn, run.run_id)
    assert settled is not None
    return settled


def _raw_column(dsn: str, run_id: str) -> Any:
    """The column as stored — ``None`` and ``{"pinned": None}`` differ here."""
    with registry.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT execution_metadata_json AS meta FROM runs WHERE run_id = %s",
            (run_id,),
        )
        row = cur.fetchone()
    assert row is not None
    return row["meta"]


@pytest.mark.integration
def test_a_retry_inherits_the_pinned_snapshot(blank_dsn: str) -> None:
    """The regression, stated as the row the retry actually holds."""
    parent = _failed_run(blank_dsn)
    with registry.connect(blank_dsn) as conn:
        registry.pin_execution_metadata(conn, parent.run_id, _PINNED)

    with registry.connect(blank_dsn) as conn:
        attempt = registry.create_retry(conn, run_id=parent.run_id)

    assert attempt.run_id != parent.run_id
    # Returned in hand, so a caller that dispatches straight off this record
    # cannot ship an attempt whose funding it never saw.
    assert (attempt.execution_metadata_json or {}).get("pinned") == _PINNED
    with registry.connect(blank_dsn) as conn:
        assert registry.read_execution_metadata(conn, attempt.run_id) == _PINNED


@pytest.mark.integration
def test_a_retry_does_not_inherit_what_the_parent_OBSERVED(blank_dsn: str) -> None:
    """``pinned`` travels; the runner's record of the parent attempt does not.

    Copying ``credential_sources`` forward would hand the next attempt an
    attribution for work it has not done — a worse failure than the missing one,
    because it reads as proof. The absence here is what forces the runner to
    write its own.
    """
    parent = _failed_run(blank_dsn)
    with registry.connect(blank_dsn) as conn:
        registry.pin_execution_metadata(conn, parent.run_id, _PINNED)
        registry.record_credential_sources(conn, parent.run_id, {"deepseek-api": "client-vault"}, attempt_no=1)

    with registry.connect(blank_dsn) as conn:
        attempt = registry.create_retry(conn, run_id=parent.run_id)

    stored = _raw_column(blank_dsn, attempt.run_id)
    assert stored == {"pinned": _PINNED}
    assert "credential_sources" not in stored
    assert "degradations" not in stored
    # And the parent's own record is untouched by any of this.
    assert _raw_column(blank_dsn, parent.run_id)["credential_sources"] == {"1": {"deepseek-api": "client-vault"}}


@pytest.mark.integration
def test_a_retry_of_an_unpinned_run_still_carries_the_column(blank_dsn: str) -> None:
    """``{"pinned": None}``, never SQL NULL — the absence has to be diagnostic.

    A workflow that declares no credential requirements is pinned as nothing, on
    purpose: "undeclared" and "declares it needs nothing" are opposite policies.
    Writing the column anyway is what lets a downstream reader tell THAT apart
    from a snapshot that went missing — under v4, a retry row with a NULL column
    now means something was dropped, and a consumer can refuse instead of
    guessing its way into operator spend.
    """
    parent = _failed_run(blank_dsn)
    assert _raw_column(blank_dsn, parent.run_id) is None

    with registry.connect(blank_dsn) as conn:
        attempt = registry.create_retry(conn, run_id=parent.run_id)

    assert _raw_column(blank_dsn, attempt.run_id) == {"pinned": None}
    with registry.connect(blank_dsn) as conn:
        # Still "no snapshot" to every reader that asks the question properly.
        assert registry.read_execution_metadata(conn, attempt.run_id) is None
    assert attempt.execution_metadata_column_present is True


@pytest.mark.integration
def test_an_inherited_snapshot_cannot_be_repriced(blank_dsn: str) -> None:
    """Inheriting it also closes the window the drop left open.

    With no column on the retry, a facade could have pinned a *freshly derived*
    snapshot onto the attempt — repricing it against entitlements edited since
    the failure, which is the exact thing pinning exists to prevent.
    """
    parent = _failed_run(blank_dsn)
    with registry.connect(blank_dsn) as conn:
        registry.pin_execution_metadata(conn, parent.run_id, _PINNED)
    with registry.connect(blank_dsn) as conn:
        attempt = registry.create_retry(conn, run_id=parent.run_id)

    repriced = {**_PINNED, "funding": {"deepseek-api": "operator", "tavily-api": "operator"}}
    with (
        registry.connect(blank_dsn) as conn,
        pytest.raises(ExecutionMetadataConflict, match="immutable"),
    ):
        registry.pin_execution_metadata(conn, attempt.run_id, repriced)

    # Re-pinning the SAME snapshot stays the documented no-op, so a facade that
    # pins defensively on the retry path is not broken by this change.
    with registry.connect(blank_dsn) as conn:
        assert registry.pin_execution_metadata(conn, attempt.run_id, _PINNED) == _PINNED


@pytest.mark.integration
def test_a_retry_below_v4_is_unchanged(blank_dsn: str) -> None:
    """The expansion window survives: no column to inherit, no error either.

    ``create_retry`` requires v2. It must not start requiring v4 because the
    snapshot happens to live there, and it must not raise the named v4 error on
    a registry that never had the column.
    """
    parent = _failed_run(blank_dsn, target=3)
    with registry.connect(blank_dsn) as conn:
        attempt = registry.create_retry(conn, run_id=parent.run_id)

    assert attempt.workspace_id == parent.workspace_id
    assert attempt.retry_of == parent.run_id
    assert attempt.execution_metadata_json is None
    with registry.connect(blank_dsn) as conn:
        fetched = registry.get_run(conn, attempt.run_id)
    assert fetched is not None
    assert fetched.execution_metadata_column_present is False
