"""Retry lineage and ordered retention (ORG-PLAN-164 WS5), against real Postgres.

These two features are the same invariant read from opposite ends. Retry says *a
workspace outlives the attempt that failed on it*; retention says *nothing deletes
a workspace while an attempt still needs it*. Get either half wrong and the damage
is silent: a retry that resumes the failed thread quietly redoes the failure, and a
retention pass that ignores lineage deletes the folder a live run is writing into.

Every assertion here is about rows the database actually holds, not about the code
that wrote them — including the two guards that only a real engine can prove: the
partial unique index that permits one live attempt per workspace, and the
``retry_of`` foreign key that refuses to orphan audit history.
"""

from __future__ import annotations

import pytest

from workflow_runtime_core import registry
from workflow_runtime_core.exceptions import (
    ActiveAttemptExists,
    RegistryError,
    RetryNotAllowed,
    SchemaVersionMismatch,
)
from workflow_runtime_core.migrations import apply_migrations
from workflow_runtime_core.models import RunStatus


def _migrated(dsn: str, *, target: int | None = None) -> None:
    with registry.connect(dsn) as conn:
        apply_migrations(conn, target=target)


def _failed_run(dsn: str, **kwargs: object) -> registry.RunRecord:
    with registry.connect(dsn) as conn:
        run = registry.create_run(conn, workflow="demo", config={"depth": 1}, **kwargs)  # type: ignore[arg-type]
        registry.claim_run(conn, run.run_id)
        registry.mark_failed(conn, run.run_id, "node blew up")
        settled = registry.get_run(conn, run.run_id)
    assert settled is not None
    return settled


def _age(dsn: str, run_id: str, *, days: int) -> None:
    """Backdate a row's ``updated_at`` so retention sees it as old.

    Time travel by SQL rather than by waiting or by monkeypatching ``now()``: the
    retention predicates are evaluated by Postgres, so the clock that matters is
    the server's.
    """
    with registry.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE runs SET updated_at = now() - make_interval(days => %s) "
            "WHERE run_id = %s",
            (days, run_id),
        )
        conn.commit()


# --- retry lineage ------------------------------------------------------------


@pytest.mark.integration
def test_a_retry_keeps_the_workspace_and_takes_a_new_thread(blank_dsn: str) -> None:
    """The whole point of the lineage: same folder, different thread.

    Same workspace, so completed stage outputs survive into the new attempt.
    Different thread, so the graph starts clean instead of replaying the state that
    failed.
    """
    _migrated(blank_dsn)
    parent = _failed_run(blank_dsn)

    with registry.connect(blank_dsn) as conn:
        attempt = registry.create_retry(conn, run_id=parent.run_id)

    assert attempt.run_id != parent.run_id
    assert attempt.thread_id != parent.thread_id
    assert attempt.thread_id == attempt.run_id
    assert attempt.workspace_id == parent.workspace_id
    assert attempt.retry_of == parent.run_id
    assert attempt.attempt_no == 2
    assert attempt.status is RunStatus.QUEUED
    # Inherited, so a retry is a rerun of the same work for the same owner.
    assert attempt.workflow == parent.workflow
    assert attempt.client_slug == parent.client_slug
    assert attempt.config_json == parent.config_json


@pytest.mark.integration
def test_the_parents_event_trail_names_its_retry(blank_dsn: str) -> None:
    """An operator looking at a failed run must be able to see it was retried
    without going hunting for a child row."""
    _migrated(blank_dsn)
    parent = _failed_run(blank_dsn)
    with registry.connect(blank_dsn) as conn:
        attempt = registry.create_retry(conn, run_id=parent.run_id)
        events = registry.list_events(conn, parent.run_id)

    retried = [e for e in events if e["kind"] == "retried"]
    assert len(retried) == 1
    assert retried[0]["detail"] == {"attempt": attempt.run_id, "attempt_no": 2}


@pytest.mark.integration
def test_repeated_retries_number_sequentially(blank_dsn: str) -> None:
    _migrated(blank_dsn)
    parent = _failed_run(blank_dsn)
    numbers = []
    current = parent.run_id
    for _ in range(3):
        with registry.connect(blank_dsn) as conn:
            attempt = registry.create_retry(conn, run_id=current)
            registry.claim_run(conn, attempt.run_id)
            registry.mark_failed(conn, attempt.run_id, "again")
        numbers.append(attempt.attempt_no)
        current = attempt.run_id
    assert numbers == [2, 3, 4]


@pytest.mark.integration
def test_retrying_an_earlier_attempt_does_not_reuse_a_number(blank_dsn: str) -> None:
    """``attempt_no`` comes from the whole workspace, not ``parent + 1``.

    Retrying attempt 1 again after its retry also failed is legitimate, and
    ``parent.attempt_no + 1`` would mint a second attempt 2 — leaving two rows
    claiming the same position in one workspace's history.
    """
    _migrated(blank_dsn)
    parent = _failed_run(blank_dsn)
    with registry.connect(blank_dsn) as conn:
        second = registry.create_retry(conn, run_id=parent.run_id)
        registry.claim_run(conn, second.run_id)
        registry.mark_failed(conn, second.run_id, "again")
        third = registry.create_retry(conn, run_id=parent.run_id)

    assert (second.attempt_no, third.attempt_no) == (2, 3)
    assert third.retry_of == parent.run_id


@pytest.mark.integration
def test_a_workspace_admits_only_one_live_attempt(blank_dsn: str) -> None:
    """The partial unique index, exercised through the retry path.

    Two live attempts on one mutable folder interleave writes into each other's
    stage outputs, and the failure survives no amount of application-level care.
    """
    _migrated(blank_dsn)
    parent = _failed_run(blank_dsn)
    with registry.connect(blank_dsn) as conn:
        registry.create_retry(conn, run_id=parent.run_id)
    with registry.connect(blank_dsn) as conn, pytest.raises(ActiveAttemptExists):
        registry.create_retry(conn, run_id=parent.run_id)


@pytest.mark.integration
@pytest.mark.parametrize("status", ["queued", "running", "paused", "completed", "cancelled"])
def test_only_a_failed_run_is_retryable(blank_dsn: str, status: str) -> None:
    """A paused run resumes; a completed one has its results. Retrying either
    would abandon a checkpoint or duplicate finished work."""
    _migrated(blank_dsn)
    with registry.connect(blank_dsn) as conn:
        run = registry.create_run(conn, workflow="demo", config={})
        if status != "queued":
            registry.claim_run(conn, run.run_id)
        if status == "paused":
            registry.mark_paused(conn, run.run_id, {"ask": "confirm"})
        elif status == "completed":
            registry.mark_completed(conn, run.run_id, {"published": []})
        elif status == "cancelled":
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE runs SET status = 'cancelled' WHERE run_id = %s",
                    (run.run_id,),
                )

    with registry.connect(blank_dsn) as conn, pytest.raises(RetryNotAllowed, match=status):
        registry.create_retry(conn, run_id=run.run_id)


@pytest.mark.integration
def test_retrying_an_unknown_run_is_a_named_error(blank_dsn: str) -> None:
    _migrated(blank_dsn)
    with registry.connect(blank_dsn) as conn, pytest.raises(RegistryError, match="not found"):
        registry.create_retry(conn, run_id="00000000-0000-0000-0000-000000000000")


@pytest.mark.integration
def test_an_override_replaces_config_but_never_the_owner(blank_dsn: str) -> None:
    """The one non-pure rerun: an operator raising a limit after diagnosing the
    failure. Authorization still comes from the parent row, so an override cannot
    move the run to another client."""
    _migrated(blank_dsn)
    parent = _failed_run(blank_dsn, client_slug="duke")
    with registry.connect(blank_dsn) as conn:
        attempt = registry.create_retry(
            conn, run_id=parent.run_id, config={"depth": 5}, image_tag="v2"
        )
    assert attempt.config_json == {"depth": 5}
    assert attempt.image_tag == "v2"
    assert attempt.client_slug == "duke"


@pytest.mark.integration
def test_a_retry_does_not_inherit_the_idempotency_key(blank_dsn: str) -> None:
    """Inheriting it would make ``create_run`` return the PARENT, so the retry
    would silently be a no-op reporting the failed run back as a new attempt."""
    _migrated(blank_dsn)
    parent = _failed_run(blank_dsn, idempotency_key="once")
    with registry.connect(blank_dsn) as conn:
        attempt = registry.create_retry(conn, run_id=parent.run_id)
    assert attempt.run_id != parent.run_id
    assert attempt.idempotency_key is None


@pytest.mark.integration
def test_retry_on_v1_is_the_named_schema_error(blank_dsn: str) -> None:
    """Not a KeyError in a background worker: the shape the fork analysis named."""
    _migrated(blank_dsn, target=1)
    parent = _failed_run(blank_dsn)
    with registry.connect(blank_dsn) as conn, pytest.raises(SchemaVersionMismatch):
        registry.create_retry(conn, run_id=parent.run_id)


# --- ordered retention --------------------------------------------------------


@pytest.mark.integration
def test_a_candidate_carries_everything_cleanup_addresses(blank_dsn: str) -> None:
    _migrated(blank_dsn)
    run = _failed_run(blank_dsn, client_slug="duke")
    _age(blank_dsn, run.run_id, days=31)

    with registry.connect(blank_dsn) as conn:
        candidates = registry.retention_candidates(conn)

    assert [c.run_id for c in candidates] == [run.run_id]
    only = candidates[0]
    assert only.client_slug == "duke"
    assert only.workspace_id == run.workspace_id
    assert only.thread_id == run.thread_id
    assert only.status is RunStatus.FAILED


@pytest.mark.integration
def test_a_run_inside_the_window_is_not_a_candidate(blank_dsn: str) -> None:
    _migrated(blank_dsn)
    run = _failed_run(blank_dsn)
    _age(blank_dsn, run.run_id, days=5)
    with registry.connect(blank_dsn) as conn:
        assert registry.retention_candidates(conn) == []


@pytest.mark.integration
def test_a_live_attempt_protects_its_whole_ancestry(blank_dsn: str) -> None:
    """The rule that makes retention safe: retention deletes a *workspace*, and a
    workspace is shared. An old failed attempt whose retry is still running must
    survive, or the pass deletes the folder out from under the live run."""
    _migrated(blank_dsn)
    parent = _failed_run(blank_dsn)
    with registry.connect(blank_dsn) as conn:
        attempt = registry.create_retry(conn, run_id=parent.run_id)
        registry.claim_run(conn, attempt.run_id)  # still running
    _age(blank_dsn, parent.run_id, days=400)
    _age(blank_dsn, attempt.run_id, days=400)

    with registry.connect(blank_dsn) as conn:
        assert registry.retention_candidates(conn) == []
        # And the guarded delete refuses too, so a stale candidate list from an
        # earlier pass cannot be replayed into the live workspace.
        assert registry.delete_run(conn, parent.run_id) is False


@pytest.mark.integration
def test_a_settled_lineage_is_returned_leaf_first(blank_dsn: str) -> None:
    """``retry_of`` is a foreign key with no cascade, so an ancestor cannot be
    deleted while its child exists. Ordering by attempt descending is what lets one
    pass clear a whole chain instead of one link per night."""
    _migrated(blank_dsn)
    parent = _failed_run(blank_dsn)
    with registry.connect(blank_dsn) as conn:
        attempt = registry.create_retry(conn, run_id=parent.run_id)
        registry.claim_run(conn, attempt.run_id)
        registry.mark_failed(conn, attempt.run_id, "again")
    _age(blank_dsn, parent.run_id, days=90)
    _age(blank_dsn, attempt.run_id, days=90)

    with registry.connect(blank_dsn) as conn:
        candidates = registry.retention_candidates(conn)
    # Only the leaf is offered; the parent becomes eligible once it is gone.
    assert [c.run_id for c in candidates] == [attempt.run_id]

    with registry.connect(blank_dsn) as conn:
        assert registry.delete_run(conn, attempt.run_id) is True
        assert [c.run_id for c in registry.retention_candidates(conn)] == [parent.run_id]
        assert registry.delete_run(conn, parent.run_id) is True
        assert registry.get_run(conn, parent.run_id) is None


@pytest.mark.integration
def test_the_guarded_delete_loses_no_race_with_a_retry(blank_dsn: str) -> None:
    """Minutes pass between selection and deletion while a workspace and its
    checkpoints are cleaned. An operator retrying in that window must win."""
    _migrated(blank_dsn)
    parent = _failed_run(blank_dsn)
    _age(blank_dsn, parent.run_id, days=90)
    with registry.connect(blank_dsn) as conn:
        stale = registry.retention_candidates(conn)
        assert [c.run_id for c in stale] == [parent.run_id]
        registry.create_retry(conn, run_id=parent.run_id)  # the race
        assert registry.delete_run(conn, parent.run_id) is False
        assert registry.get_run(conn, parent.run_id) is not None


@pytest.mark.integration
def test_the_bulk_prune_is_lineage_guarded_too(blank_dsn: str) -> None:
    """Without the guard this statement either deletes the row naming a live run's
    workspace, or aborts entirely on the ``retry_of`` foreign key — one client's
    live lineage stopping retention for every other client."""
    _migrated(blank_dsn)
    parent = _failed_run(blank_dsn)
    with registry.connect(blank_dsn) as conn:
        attempt = registry.create_retry(conn, run_id=parent.run_id)
        registry.claim_run(conn, attempt.run_id)  # live, and recent
    _age(blank_dsn, parent.run_id, days=400)

    with registry.connect(blank_dsn) as conn:
        assert registry.prune_terminal_runs(conn) == 0
        assert registry.get_run(conn, parent.run_id) is not None


@pytest.mark.integration
def test_the_bulk_prune_still_clears_an_unretried_run(blank_dsn: str) -> None:
    _migrated(blank_dsn)
    run = _failed_run(blank_dsn)
    _age(blank_dsn, run.run_id, days=31)
    with registry.connect(blank_dsn) as conn:
        assert registry.prune_terminal_runs(conn) == 1
        assert registry.get_run(conn, run.run_id) is None


@pytest.mark.integration
def test_the_bulk_prune_on_v1_is_unchanged(blank_dsn: str) -> None:
    """v1 has no lineage columns to guard on — and no lineages either. The
    expansion window must see byte-identical behaviour."""
    _migrated(blank_dsn, target=1)
    run = _failed_run(blank_dsn)
    _age(blank_dsn, run.run_id, days=31)
    with registry.connect(blank_dsn) as conn:
        assert registry.prune_terminal_runs(conn) == 1


@pytest.mark.integration
def test_ordered_retention_on_v1_refuses_by_name(blank_dsn: str) -> None:
    """Returning "no candidates" would let a caller report a clean pass against a
    registry where ordered cleanup simply does not apply."""
    _migrated(blank_dsn, target=1)
    with registry.connect(blank_dsn) as conn, pytest.raises(SchemaVersionMismatch):
        registry.retention_candidates(conn)


@pytest.mark.integration
def test_a_zero_retention_window_is_refused(blank_dsn: str) -> None:
    """``older_than_days=0`` would make every terminal run eligible the instant it
    finished — a plausible typo with unrecoverable consequences."""
    _migrated(blank_dsn)
    with registry.connect(blank_dsn) as conn, pytest.raises(RegistryError):
        registry.retention_candidates(conn, older_than_days=0)


@pytest.mark.integration
def test_the_pass_is_bounded_and_the_bound_is_the_callers(blank_dsn: str) -> None:
    _migrated(blank_dsn)
    for _ in range(3):
        run = _failed_run(blank_dsn)
        _age(blank_dsn, run.run_id, days=31)
    with registry.connect(blank_dsn) as conn:
        assert len(registry.retention_candidates(conn, limit=2)) == 2
        assert len(registry.retention_candidates(conn)) == 3
