"""Run registry — the durable record of every run, and the only writer of it.

Extracted verbatim-in-behaviour from ``Stromy/stromy/runtime/registry.py``
(ORG-PLAN-070 C1) so that the three consumers — the Stromy runtime, the public
workflow facade, and client executors — share ONE lifecycle instead of three
drifting copies of the same DML. What changed in the move:

1. **DDL left.** It lives in :mod:`workflow_runtime_core.migrations` and is
   applied only by ``wrc migrate``. This module is DML-only, which is what the
   facade was already restricted to; now the restriction is structural.
2. **The version gate moved** to :mod:`workflow_runtime_core.schema`, where it
   became a *range* check so readers can be deployed ahead of a migration.

Everything else — transition rules, the idempotency-key contract, the savepoint
around the racing INSERT, event emission — is unchanged, and the extracted
consumer test suites are the proof.

Two data planes — do not cross them
-----------------------------------
This registry is RUN STATE: small, mutable, transactional rows. Bulk artifacts
stay blob-canonical and are referenced, never inlined.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from typing import Any, NoReturn, cast

import psycopg
from psycopg.rows import dict_row

from .exceptions import ActiveAttemptExists, RegistryError, SchemaVersionMismatch
from .models import TERMINAL_STATUSES, RunRecord, RunStatus, utcnow

#: Every connection this module hands out uses ``dict_row``. The alias makes that
#: a type-level fact, so a consumer writing ``row["status"]`` type-checks.
DbConnection = psycopg.Connection[dict[str, Any]]

_DSN_ENV = "STROMY_PG_DSN"

#: Key under which a resume value rides in ``config_json``. Exported because the
#: runner pops it and the facade writes it — the same literal in two repos is
#: exactly the drift this extraction exists to remove.
RESUME_KEY = "_resume"


def dsn_from_env(env_var: str = _DSN_ENV) -> str:
    """Read the registry DSN from the environment, or fail loudly.

    There is deliberately no local-file fallback: run state must be durable and
    shared, so a missing DSN is a misconfiguration, not a cue to degrade.
    """
    dsn = os.environ.get(env_var, "").strip()
    if not dsn:
        raise RegistryError(
            f"{env_var} is unset. The runtime needs a Postgres DSN; there is no "
            "local-file fallback by design (run state must be durable and shared)."
        )
    return dsn


@contextmanager
def connect(dsn: str | None = None) -> Generator[DbConnection]:
    """Open a registry connection. Commits on clean exit, rolls back on error."""
    try:
        # psycopg's `connect` overloads do not carry the row_factory's row type
        # through to the returned Connection, so the cast restates what
        # `row_factory=dict_row` already guarantees at runtime.
        conn = cast(
            DbConnection,
            psycopg.connect(
                dsn or dsn_from_env(),
                row_factory=dict_row,  # pyright: ignore[reportArgumentType]
            ),
        )
    except psycopg.Error as exc:  # pragma: no cover - connection-time failure
        raise RegistryError(f"cannot reach the run registry: {exc}") from exc
    try:
        with conn:
            yield conn
    finally:
        conn.close()


# --- internals ---------------------------------------------------------------


def _require_returned(row: dict[str, Any] | None, what: str) -> dict[str, Any]:
    """Unwrap a RETURNING row, or fail with a useful message.

    Not an assert: asserts are stripped under ``python -O``, which would turn a
    contract violation into a confusing NoneType error inside ``from_row``.
    """
    if row is None:  # pragma: no cover - would mean the DB broke its contract
        raise RegistryError(f"{what} returned no row")
    return row


def _emit(conn: DbConnection, run_id: str, kind: str, detail: Any = None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO run_events (run_id, kind, detail) VALUES (%s, %s, %s)",
            (run_id, kind, json.dumps(detail) if detail is not None else None),
        )


def new_run_id() -> str:
    return str(uuid.uuid4())


def _data_plane_live(conn: DbConnection) -> bool:
    """Whether the live schema carries the v2 data-plane columns.

    Probed per call, not cached: terminal writes and run creation are rare
    relative to their round trips, and a cached answer would go stale across the
    one moment it matters — the migration itself.
    """
    from .schema import read_schema_version

    live = read_schema_version(conn)
    return live is not None and live >= 2


def _require_data_plane_column(exc: psycopg.errors.UndefinedColumn, feature: str) -> NoReturn:
    """Turn a raw UndefinedColumn into the named error the caller can act on.

    Without this, a v2-only call against a v1 database surfaces as a cryptic
    column error in a background worker AFTER the startup gate went green —
    exactly the silent-collision shape the 2026-08-06 fork analysis documented.
    """
    raise SchemaVersionMismatch(
        f"{feature} requires schema v2 (the workflow data plane); the live "
        "registry is still v1. Run `wrc migrate` before enabling this path."
    ) from exc


# --- accessors ---------------------------------------------------------------


def create_run(
    conn: DbConnection,
    *,
    workflow: str,
    config: dict[str, Any],
    client_slug: str | None = None,
    thread_id: str | None = None,
    run_id: str | None = None,
    image_tag: str | None = None,
    job_template: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
    workspace_id: str | None = None,
    retry_of: str | None = None,
    attempt_no: int = 1,
    input_set_id: str | None = None,
) -> RunRecord:
    """Register a queued run. Returns the EXISTING run on idempotency-key reuse.

    ``run_id`` may be supplied by a caller that must know the id before the row
    exists (the facade mints one to render its job template). It stays optional
    so the Stromy path keeps its original signature.

    The v2 lineage arguments: a fresh run owns a brand-new workspace; a retry
    passes the prior one in so completed stage outputs survive into the new
    attempt. On a v1 database they must be left at their defaults — passing any
    of them raises the named schema error rather than silently dropping lineage.
    """
    if idempotency_key:
        existing = find_by_idempotency_key(conn, idempotency_key)
        if existing is not None:
            return existing

    data_plane = _data_plane_live(conn)
    lineage_requested = (
        workspace_id is not None
        or retry_of is not None
        or attempt_no != 1
        or input_set_id is not None
    )
    if lineage_requested and not data_plane:
        raise SchemaVersionMismatch(
            "workspace/attempt lineage requires schema v2; the live registry is "
            "still v1. Run `wrc migrate` before creating lineage-bearing runs."
        )

    resolved_run_id = run_id or new_run_id()
    try:
        # Savepoint: the find_by_idempotency_key pre-check above is a TOCTOU race
        # — two concurrent callers with the same key both see "not found" and
        # both INSERT; the loser hits runs_idempotency_key_uniq. Isolating the
        # INSERT in a savepoint means that UniqueViolation rolls back only this
        # statement, leaving the caller's outer transaction usable for the
        # re-fetch below (a bare INSERT would poison the whole transaction).
        with conn.transaction(), conn.cursor() as cur:
            if data_plane:
                cur.execute(
                    """
                    INSERT INTO runs (run_id, workflow, thread_id, status,
                                      client_slug, config_json, image_tag,
                                      job_template_json, idempotency_key,
                                      workspace_id, retry_of, attempt_no,
                                      input_set_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING *
                    """,
                    (
                        resolved_run_id,
                        workflow,
                        thread_id or resolved_run_id,
                        RunStatus.QUEUED.value,
                        client_slug,
                        json.dumps(config),
                        image_tag,
                        json.dumps(job_template) if job_template is not None else None,
                        idempotency_key,
                        workspace_id or str(uuid.uuid4()),
                        retry_of,
                        attempt_no,
                        input_set_id,
                    ),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO runs (run_id, workflow, thread_id, status,
                                      client_slug, config_json, image_tag,
                                      job_template_json, idempotency_key)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING *
                    """,
                    (
                        resolved_run_id,
                        workflow,
                        thread_id or resolved_run_id,  # one thread per run unless resuming
                        RunStatus.QUEUED.value,
                        client_slug,
                        json.dumps(config),
                        image_tag,
                        json.dumps(job_template) if job_template is not None else None,
                        idempotency_key,
                    ),
                )
            row = _require_returned(cur.fetchone(), "create_run")
    except psycopg.errors.UniqueViolation as exc:
        # The concurrent winner already created the run under this key — return
        # theirs, which is exactly what an idempotency key promises. Only
        # reachable with a key set (the unique index is partial on NOT NULL).
        existing = find_by_idempotency_key(conn, idempotency_key) if idempotency_key else None
        if existing is not None:
            return existing
        # Otherwise this is the OTHER partial unique index: a second live
        # attempt on one workspace. Name it, because "duplicate key value
        # violates unique constraint" tells an operator nothing about the
        # actual rule ("finish or fail the running attempt first").
        if "runs_active_workspace_uniq" in str(exc):
            raise ActiveAttemptExists(
                f"workspace {workspace_id} already has a queued/running/paused "
                "attempt; a workspace carries at most one live attempt"
            ) from exc
        raise
    _emit(conn, resolved_run_id, "created", {"workflow": workflow, "client_slug": client_slug})
    return RunRecord.from_row(row)


def find_by_idempotency_key(conn: DbConnection, key: str) -> RunRecord | None:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM runs WHERE idempotency_key = %s", (key,))
        row = cur.fetchone()
    return RunRecord.from_row(row) if row else None


def get_run(conn: DbConnection, run_id: str) -> RunRecord | None:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM runs WHERE run_id = %s", (run_id,))
        row = cur.fetchone()
    return RunRecord.from_row(row) if row else None


def list_runs(
    conn: DbConnection,
    *,
    client_slug: str | None = None,
    client_slugs: Sequence[str] | None = None,
    limit: int = 50,
) -> list[RunRecord]:
    """List runs, optionally scoped to one client or a set of clients.

    Both scoping arguments default to ``None``, which is UNSCOPED (operator
    view). A caller serving a client token must pass its scope — requesting the
    filter is the caller's job, and both consumers' tests assert it.

    ``client_slugs=[]`` is an explicitly EMPTY scope and returns nothing. That
    distinction matters: a token whose scope resolved to zero clients must see
    zero runs, never the operator-wide list.
    """
    if client_slug is not None and client_slugs is not None:
        raise RegistryError("pass client_slug or client_slugs, not both")

    sql = "SELECT * FROM runs"
    params: list[Any] = []
    if client_slug is not None:
        sql += " WHERE client_slug = %s"
        params.append(client_slug)
    elif client_slugs is not None:
        if not client_slugs:
            return []
        sql += " WHERE client_slug = ANY(%s)"
        params.append(list(client_slugs))
    sql += " ORDER BY created_at DESC LIMIT %s"
    params.append(limit)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [RunRecord.from_row(r) for r in cur.fetchall()]


def claim_run(conn: DbConnection, run_id: str) -> RunRecord | None:
    """Single-flight claim: ``queued`` -> ``running``. None if the race was lost.

    ``FOR UPDATE`` serialises concurrent claimers; ``SKIP LOCKED`` is deliberately
    NOT used — we want the loser to observe the winner's committed status and
    exit cleanly, not to skip the row and think it vanished.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM runs WHERE run_id = %s FOR UPDATE", (run_id,))
        row = cur.fetchone()
        if row is None:
            return None
        if row["status"] not in (RunStatus.QUEUED.value,):
            return None  # someone else claimed it, or it is not startable
        cur.execute(
            "UPDATE runs SET status = %s, updated_at = now() WHERE run_id = %s RETURNING *",
            (RunStatus.RUNNING.value, run_id),
        )
        claimed = _require_returned(cur.fetchone(), "claim_run")
    _emit(conn, run_id, "claimed")
    return RunRecord.from_row(claimed)


# --- v2: queue dispatch and leases (ORG-PLAN-164 WS2) -------------------------
#
# Everything in this section requires the data-plane columns and raises the
# NAMED SchemaVersionMismatch on a v1 database instead of a KeyError or
# UndefinedColumn in a background worker after the startup gate went green.


def set_dispatch(conn: DbConnection, run_id: str, dispatch_id: str) -> None:
    """Record the dispatch id BEFORE the message is enqueued.

    Order matters: the row must already know its dispatch id when the message
    lands, or a very fast runner could claim against a row that has not been
    told which dispatch it belongs to and reject its own message.
    """
    try:
        with conn.transaction(), conn.cursor() as cur:
            cur.execute(
                "UPDATE runs SET dispatch_id = %s, updated_at = now() WHERE run_id = %s",
                (dispatch_id, run_id),
            )
    except psycopg.errors.UndefinedColumn as exc:
        _require_data_plane_column(exc, "set_dispatch")


def mark_dispatch_failed(conn: DbConnection, run_id: str, reason: str) -> None:
    """Enqueue failed after the row was committed.

    The row is deliberately NOT deleted. A run that exists but was never
    dispatched is recoverable by an operator retry; a deleted row is a run the
    client was told about and can never be told anything more about.
    """
    failure = {
        "stage": "dispatch",
        "error_type": "DispatchEnqueueFailed",
        "message": reason[:2000],
        "retryable": True,
    }
    try:
        with conn.transaction(), conn.cursor() as cur:
            cur.execute(
                "UPDATE runs SET error_json = %s, updated_at = now() WHERE run_id = %s",
                (json.dumps(failure), run_id),
            )
    except psycopg.errors.UndefinedColumn as exc:
        _require_data_plane_column(exc, "mark_dispatch_failed")
    _emit(conn, run_id, "dispatch_failed", {"reason": reason[:2000]})


def claim_dispatch(
    conn: DbConnection,
    *,
    run_id: str,
    dispatch_id: str,
    owner: str,
    lease_seconds: int,
) -> RunRecord | None:
    """Atomically claim a queue-dispatched run, or return None.

    The queue body is a REFERENCE, never the source of truth, so every guard
    lives here in one transaction:

    * the row must exist and still be ``queued``;
    * its ``dispatch_id`` must match the message — a stale message from a prior
      dispatch of the same run cannot start a second writer;
    * any existing lease must have expired — which is what makes crash recovery
      safe rather than a race. A replacement worker becomes eligible only after
      the dead worker's lease lapses, not merely because the queue made the
      message visible again.

    Returning None is a normal outcome (the message is a duplicate, or another
    worker won); the caller deletes the message and exits cleanly.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM runs WHERE run_id = %s FOR UPDATE", (run_id,))
        row = cur.fetchone()
        if row is None:
            return None
        if "lease_expires_at" not in row:
            raise SchemaVersionMismatch(
                "claim_dispatch requires schema v2 (the workflow data plane); "
                "the live registry is still v1. Run `wrc migrate` first."
            )
        if row["status"] != RunStatus.QUEUED.value:
            return None
        if str(row["dispatch_id"]) != str(dispatch_id):
            return None
        lease_expires_at = row["lease_expires_at"]
        if lease_expires_at is not None and lease_expires_at > utcnow():
            return None
        cur.execute(
            """
            UPDATE runs
               SET status = %s,
                   lease_owner = %s,
                   lease_expires_at = now() + make_interval(secs => %s),
                   heartbeat_at = now(),
                   delivery_count = delivery_count + 1,
                   updated_at = now()
             WHERE run_id = %s
            RETURNING *
            """,
            (RunStatus.RUNNING.value, owner, lease_seconds, run_id),
        )
        claimed = _require_returned(cur.fetchone(), "claim_dispatch")
    _emit(conn, run_id, "claimed", {"owner": owner, "dispatch_id": str(dispatch_id)})
    return RunRecord.from_row(claimed)


def renew_lease(
    conn: DbConnection, *, run_id: str, owner: str, lease_seconds: int
) -> bool:
    """Extend this worker's lease. False means the lease was lost.

    A worker that loses its lease must stop: something else has been told it may
    claim this run, and two writers on one checkpoint thread interleave.
    """
    try:
        with conn.transaction(), conn.cursor() as cur:
            cur.execute(
                """
                UPDATE runs
                   SET lease_expires_at = now() + make_interval(secs => %s),
                       heartbeat_at = now(),
                       updated_at = now()
                 WHERE run_id = %s AND lease_owner = %s AND status = %s
                RETURNING run_id
                """,
                (lease_seconds, run_id, owner, RunStatus.RUNNING.value),
            )
            return cur.fetchone() is not None
    except psycopg.errors.UndefinedColumn as exc:
        _require_data_plane_column(exc, "renew_lease")


def release_lease(conn: DbConnection, run_id: str) -> None:
    """Clear lease bookkeeping once a run reaches a settled state."""
    try:
        with conn.transaction(), conn.cursor() as cur:
            cur.execute(
                "UPDATE runs SET lease_owner = NULL, lease_expires_at = NULL, "
                "updated_at = now() WHERE run_id = %s",
                (run_id,),
            )
    except psycopg.errors.UndefinedColumn as exc:
        _require_data_plane_column(exc, "release_lease")


def requeue_expired_lease(conn: DbConnection, run_id: str) -> bool:
    """Return a crashed run to ``queued`` once its lease has lapsed."""
    try:
        with conn.transaction(), conn.cursor() as cur:
            cur.execute(
                """
                UPDATE runs
                   SET status = %s, lease_owner = NULL, lease_expires_at = NULL,
                       updated_at = now()
                 WHERE run_id = %s AND status = %s
                   AND lease_expires_at IS NOT NULL AND lease_expires_at <= now()
                RETURNING run_id
                """,
                (RunStatus.QUEUED.value, run_id, RunStatus.RUNNING.value),
            )
            found = cur.fetchone() is not None
    except psycopg.errors.UndefinedColumn as exc:
        _require_data_plane_column(exc, "requeue_expired_lease")
    if found:
        _emit(conn, run_id, "lease_expired")
    return found


def record_progress(conn: DbConnection, run_id: str, progress: dict[str, Any]) -> None:
    """Persist a progress snapshot + heartbeat.

    Rate limiting is the WORKER's job, not this function's: a graph emits node
    events far faster than Postgres should be written, and the decision about
    what is worth a round trip belongs where the event stream is.
    """
    try:
        with conn.transaction(), conn.cursor() as cur:
            cur.execute(
                "UPDATE runs SET progress_json = %s, heartbeat_at = now(), "
                "updated_at = now() WHERE run_id = %s",
                (json.dumps(progress), run_id),
            )
    except psycopg.errors.UndefinedColumn as exc:
        _require_data_plane_column(exc, "record_progress")


def mark_failed_structured(
    conn: DbConnection, run_id: str, failure: dict[str, Any]
) -> None:
    """Terminal failure with a structured, client-safe payload.

    ``failure`` carries {stage, error_type, message, retryable, correlation_id}.
    The traceback stays in server logs keyed by correlation id — a client
    payload is not the place for internal frames or paths.
    """
    try:
        with conn.transaction(), conn.cursor() as cur:
            cur.execute(
                """
                UPDATE runs
                   SET status = %s, error = %s, error_json = %s,
                       lease_owner = NULL, lease_expires_at = NULL, updated_at = now()
                 WHERE run_id = %s
                """,
                (
                    RunStatus.FAILED.value,
                    str(failure.get("message", ""))[:8000],
                    json.dumps(failure),
                    run_id,
                ),
            )
    except psycopg.errors.UndefinedColumn as exc:
        _require_data_plane_column(exc, "mark_failed_structured")
    _emit(conn, run_id, "failed", failure)


# --- terminal transitions -----------------------------------------------------


def mark_paused(conn: DbConnection, run_id: str, interrupt_payload: Any) -> None:
    """Run hit an ``interrupt()`` and is exiting. State lives in the checkpoint."""
    if _data_plane_live(conn):
        sql = (
            "UPDATE runs SET status = %s, interrupt_payload = %s, "
            "lease_owner = NULL, lease_expires_at = NULL, updated_at = now() "
            "WHERE run_id = %s"
        )
    else:
        sql = (
            "UPDATE runs SET status = %s, interrupt_payload = %s, updated_at = now() "
            "WHERE run_id = %s"
        )
    with conn.cursor() as cur:
        cur.execute(sql, (RunStatus.PAUSED.value, json.dumps(interrupt_payload), run_id))
    _emit(conn, run_id, "paused")


def mark_completed(
    conn: DbConnection,
    run_id: str,
    artifacts: dict[str, Any] | None = None,
    *,
    artifacts_published: bool = False,
) -> None:
    """Flip a run to ``completed``, recording its artifacts in the SAME statement.

    One UPDATE, not two: a client that saw ``completed`` with the descriptors not
    yet written would poll a finished run and find nothing to download. Setting
    ``artifacts_published`` stamps ``artifacts_published_at``, which is how the
    maintenance pass tells "this run's outputs are in the container" from "this
    run completed before publication existed".
    """
    if _data_plane_live(conn):
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE runs SET status = %s, artifacts_json = %s, "
                "artifacts_published_at = CASE WHEN %s THEN now() "
                "ELSE artifacts_published_at END, "
                "lease_owner = NULL, lease_expires_at = NULL, updated_at = now() "
                "WHERE run_id = %s",
                (
                    RunStatus.COMPLETED.value,
                    json.dumps(artifacts) if artifacts is not None else None,
                    artifacts_published,
                    run_id,
                ),
            )
    else:
        if artifacts_published:
            raise SchemaVersionMismatch(
                "artifacts_published requires schema v2; the live registry is "
                "still v1 — a publication stamp silently dropped here would lie "
                "to the maintenance pass."
            )
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE runs SET status = %s, artifacts_json = %s, updated_at = now() "
                "WHERE run_id = %s",
                (
                    RunStatus.COMPLETED.value,
                    json.dumps(artifacts) if artifacts is not None else None,
                    run_id,
                ),
            )
    _emit(conn, run_id, "completed")


def mark_failed(conn: DbConnection, run_id: str, error: str) -> None:
    """Terminal failure with a plain-text reason.

    Clears the lease on v2, exactly as the structured variant does: a failed run
    that still names a lease owner reads, to every recovery query and every
    operator, as work someone is still doing.
    """
    if _data_plane_live(conn):
        sql = (
            "UPDATE runs SET status = %s, error = %s, lease_owner = NULL, "
            "lease_expires_at = NULL, updated_at = now() WHERE run_id = %s"
        )
    else:
        sql = "UPDATE runs SET status = %s, error = %s, updated_at = now() WHERE run_id = %s"
    with conn.cursor() as cur:
        cur.execute(sql, (RunStatus.FAILED.value, error[:8000], run_id))
    _emit(conn, run_id, "failed", {"error": error[:2000]})


def request_resume(conn: DbConnection, run_id: str, resume_payload: Any) -> RunRecord:
    """``paused`` -> ``queued``, carrying the operator/client's resume value.

    The payload rides in ``config_json._resume`` so the next worker process finds
    it without a second table; the checkpoint holds the actual graph state.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM runs WHERE run_id = %s FOR UPDATE", (run_id,))
        row = cur.fetchone()
        if row is None:
            raise RegistryError(f"run {run_id} not found")
        if row["status"] != RunStatus.PAUSED.value:
            raise RegistryError(
                f"run {run_id} is {row['status']}, not paused — nothing to resume"
            )
        config = dict(row["config_json"] or {})
        config[RESUME_KEY] = resume_payload
        cur.execute(
            "UPDATE runs SET status = %s, config_json = %s, interrupt_payload = NULL, "
            "updated_at = now() WHERE run_id = %s RETURNING *",
            (RunStatus.QUEUED.value, json.dumps(config), run_id),
        )
        updated = _require_returned(cur.fetchone(), "request_resume")
    _emit(conn, run_id, "resume_requested")
    return RunRecord.from_row(updated)


def cancel_run(conn: DbConnection, run_id: str) -> RunRecord:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM runs WHERE run_id = %s FOR UPDATE", (run_id,))
        row = cur.fetchone()
        if row is None:
            raise RegistryError(f"run {run_id} not found")
        if row["status"] in {s.value for s in TERMINAL_STATUSES}:
            raise RegistryError(f"run {run_id} is already {row['status']}")
        cur.execute(
            "UPDATE runs SET status = %s, updated_at = now() WHERE run_id = %s RETURNING *",
            (RunStatus.CANCELLED.value, run_id),
        )
        updated = _require_returned(cur.fetchone(), "cancel_run")
    _emit(conn, run_id, "cancelled")
    return RunRecord.from_row(updated)


def list_events(conn: DbConnection, run_id: str) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM run_events WHERE run_id = %s ORDER BY created_at, event_id",
            (run_id,),
        )
        return [dict(row) for row in cur.fetchall()]


# --- retention ---------------------------------------------------------------


def stale_terminal_thread_ids(
    conn: DbConnection,
    *,
    older_than_days: int = 30,
) -> list[str]:
    """Return checkpoint thread IDs whose terminal runs exceeded retention."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT thread_id FROM runs WHERE status = ANY(%s) "
            "AND updated_at < now() - make_interval(days => %s)",
            ([s.value for s in TERMINAL_STATUSES], older_than_days),
        )
        return [str(row["thread_id"]) for row in cur.fetchall()]


def prune_terminal_runs(conn: DbConnection, *, older_than_days: int = 30) -> int:
    """Delete terminal registry rows past retention, after checkpoint cleanup.

    The maintenance entrypoint deletes checkpoint data first and calls this only
    once every stale thread has been cleared. Keeping registry deletion separate
    makes a checkpoint failure fail CLOSED: the run row remains available for a
    later retry instead of becoming an orphaned, untraceable checkpoint.
    """
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM runs WHERE status = ANY(%s) "
            "AND updated_at < now() - make_interval(days => %s)",
            ([s.value for s in TERMINAL_STATUSES], older_than_days),
        )
        return cur.rowcount
