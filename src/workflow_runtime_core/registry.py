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

from .exceptions import (
    ActiveAttemptExists,
    RegistryError,
    RetryNotAllowed,
    SchemaVersionMismatch,
)
from .models import (
    TERMINAL_STATUSES,
    RetentionCandidate,
    RunRecord,
    RunStatus,
    utcnow,
)

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


def create_retry(
    conn: DbConnection,
    *,
    run_id: str,
    new_run_id: str | None = None,
    job_template: dict[str, Any] | None = None,
    image_tag: str | None = None,
    config: dict[str, Any] | None = None,
) -> RunRecord:
    """Mint the next attempt of a failed run: new run, new thread, same workspace.

    The lineage contract in one place, because both consumers need it and both
    would otherwise write their own version of "what a retry is":

    * **New run id and new thread id.** A retry does not resume the failed
      LangGraph thread — it starts a clean one. Resuming would replay the failed
      node's state, which is exactly what a retry is trying to get away from. The
      new thread defaults to the new run id, so the two never alias.
    * **Same workspace.** ``workspace_id`` carries over, so whatever completed
      stages already wrote to the durable folder survives into the new attempt and
      an expensive run is not recomputed from zero.
    * **Same input set.** The evidence was verified and attached once; re-attaching
      would re-run an ownership check that already passed and could now fail for an
      unrelated reason (an expired session).
    * **``attempt_no`` from the whole lineage**, not ``parent.attempt_no + 1``.
      Retrying attempt 2 twice — legitimate, if the first retry also failed —
      would otherwise mint two attempt 3s and make the audit trail ambiguous.

    The parent's ``idempotency_key`` is deliberately NOT inherited. Reusing it
    would make ``create_run`` return the parent — the retry would silently become
    a no-op that reports the failed run back as if it were a new attempt.

    ``config`` overrides the inherited configuration. It exists for the one case
    that is not a pure rerun: an operator raising a limit or switching a provider
    pin after diagnosing the failure. Everything about *authorization* still comes
    from the parent row (owner, workflow), so an override cannot widen scope.

    Concurrency: the parent is locked ``FOR UPDATE`` and the workspace's live-attempt
    index is the real arbiter. Two operators clicking retry at once produce one new
    run and one :class:`~workflow_runtime_core.exceptions.ActiveAttemptExists`.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM runs WHERE run_id = %s FOR UPDATE", (run_id,))
        row = cur.fetchone()
    if row is None:
        raise RegistryError(f"run {run_id} not found")
    parent = RunRecord.from_row(row)

    if parent.status is not RunStatus.FAILED:
        raise RetryNotAllowed(
            f"run {run_id} is {parent.status}, and only a failed run can be "
            "retried. A paused run resumes; a completed one has its results."
        )
    if parent.workspace_id is None:
        # Unreachable on a migrated database (migration 0002 backfills the column
        # and makes it NOT NULL), so this is the v1 case stated plainly rather
        # than a KeyError deep in the INSERT.
        raise SchemaVersionMismatch(
            f"run {run_id} carries no workspace; retry lineage requires schema "
            "v2. Run `wrc migrate` first."
        )

    with conn.cursor() as cur:
        cur.execute(
            "SELECT max(attempt_no) AS highest FROM runs WHERE workspace_id = %s",
            (parent.workspace_id,),
        )
        highest = (cur.fetchone() or {}).get("highest") or parent.attempt_no

    attempt = create_run(
        conn,
        workflow=parent.workflow,
        config=config if config is not None else dict(parent.config_json),
        client_slug=parent.client_slug,
        run_id=new_run_id,
        image_tag=image_tag if image_tag is not None else parent.image_tag,
        job_template=job_template if job_template is not None else parent.job_template_json,
        workspace_id=parent.workspace_id,
        retry_of=parent.run_id,
        attempt_no=int(highest) + 1,
        input_set_id=parent.input_set_id,
    )
    # Recorded against the PARENT as well, so the failed run's own event trail says
    # what became of it. An operator reading a failed run should not have to search
    # for a child to find out whether anyone retried it.
    _emit(conn, parent.run_id, "retried", {"attempt": attempt.run_id, "attempt_no": attempt.attempt_no})
    return attempt


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


def set_input_set(conn: DbConnection, run_id: str, input_set_id: str) -> None:
    """Bind an attached input set to a run.

    Sibling of :func:`set_dispatch`, and here for the same reason: ``input_set_id``
    is a column on ``runs``, so the core owns writes to it. A consumer that owns
    the *upload* tables (the workflow facade does) still must not reach across and
    update a core column with its own SQL — that is the coupling the 2026-08-06
    ownership rule exists to prevent, and the direction it fails in is silent.

    Called inside the caller's transaction, after the run row exists: the run
    cannot reference an input set it does not own, because ownership is verified
    in that same transaction and a failure rolls both back.
    """
    try:
        with conn.transaction(), conn.cursor() as cur:
            cur.execute(
                "UPDATE runs SET input_set_id = %s, updated_at = now() WHERE run_id = %s",
                (input_set_id, run_id),
            )
    except psycopg.errors.UndefinedColumn as exc:
        _require_data_plane_column(exc, "set_input_set")


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
#
# Two eligibility rules, and both are lineage rules rather than age rules:
#
# 1. **No live attempt on the workspace.** Retention deletes a *workspace*, not
#    just a row, and a workspace is shared across attempts. A failed attempt 1
#    whose retry is still running must survive, or the pass would delete the
#    folder out from under the run that is using it.
# 2. **No surviving descendant.** ``runs.retry_of`` is a real foreign key with no
#    cascade, so an ancestor cannot be deleted while a child references it. That
#    is deliberate — the alternative silently erases audit history — which means
#    retention has to clear a lineage leaf-first. Candidates come back ordered so
#    one pass clears a whole chain, and the guard is re-checked at the delete.

# Interpolated into three statements below. It is a module constant with no
# caller-reachable input, which is why those three carry ``noqa: S608``: the rule
# cannot distinguish a shared predicate from a spliced value, and duplicating this
# clause three times to satisfy it would be the actual hazard — three copies of one
# safety rule, drifting.
_LINEAGE_SAFE = """
      AND NOT EXISTS (
            SELECT 1 FROM runs live
             WHERE live.workspace_id = r.workspace_id
               AND live.status <> ALL(%(terminal)s)
          )
      AND NOT EXISTS (
            SELECT 1 FROM runs child WHERE child.retry_of = r.run_id
          )
"""


def retention_candidates(
    conn: DbConnection,
    *,
    older_than_days: int = 30,
    limit: int = 200,
) -> list[RetentionCandidate]:
    """Runs whose whole lineage is settled and past retention, leaf-first.

    Read-only. The driver that owns the durable share and the checkpoint store
    performs the ordered cleanup and calls :func:`delete_run` last, so that a
    failure anywhere in the middle leaves a row the next pass retries rather than
    an orphaned workspace nobody will ever look for again.

    ``limit`` bounds one pass. A backlog is worked down over successive passes
    instead of one run holding a long transaction over thousands of rows — and the
    driver reports what it left, because a silent cap reads as "everything was
    cleaned".
    """
    if older_than_days < 1:
        raise RegistryError("older_than_days must be at least 1")
    if not _data_plane_live(conn):
        # Not a degraded read: on v1 there is no workspace to delete, so ordered
        # cleanup has nothing to order. Saying so by name is what stops a caller
        # from concluding "no candidates" and reporting a clean pass.
        raise SchemaVersionMismatch(
            "ordered retention requires schema v2 (runs own a durable workspace "
            "only from the data plane onward); on v1 use prune_terminal_runs."
        )
    terminal = [s.value for s in TERMINAL_STATUSES]
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT r.run_id, r.thread_id, r.client_slug, r.workspace_id,
                   r.status, r.updated_at
              FROM runs r
             WHERE r.status = ANY(%(terminal)s)
               AND r.updated_at < now() - make_interval(days => %(days)s)
               {_LINEAGE_SAFE}
             ORDER BY r.workspace_id, r.attempt_no DESC, r.updated_at
             LIMIT %(limit)s
            """,  # noqa: S608 - _LINEAGE_SAFE is a constant, never caller input
            {"terminal": terminal, "days": older_than_days, "limit": limit},
        )
        return [
            RetentionCandidate(
                run_id=str(row["run_id"]),
                thread_id=str(row["thread_id"]),
                client_slug=row["client_slug"],
                workspace_id=str(row["workspace_id"]) if row["workspace_id"] else None,
                status=RunStatus(row["status"]),
                updated_at=row["updated_at"],
            )
            for row in cur.fetchall()
        ]


def mark_retention_started(conn: DbConnection, run_id: str) -> None:
    """Record that ordered cleanup has begun on this run. The audit mark.

    A narrow, named operation rather than a general "write me an event" API: the
    core owns the event vocabulary, and a consumer free to invent kinds is how two
    services end up logging the same fact under two names.

    It earns its place by covering the partial-failure case. If the workspace is
    deleted and the row deletion then fails, the row looks entirely normal — and a
    retry of it would find an empty folder with nothing to explain why. This event
    is that explanation, and it is written before anything is destroyed.
    """
    _emit(conn, run_id, "retention_started")


def delete_run(conn: DbConnection, run_id: str, *, older_than_days: int = 30) -> bool:
    """Delete ONE run row, re-checking every eligibility rule in the statement.

    The last step of ordered cleanup, and the guard is repeated here rather than
    trusted from selection time: minutes can pass between the two while a workspace
    is deleted and checkpoints are pruned, and in that window an operator can retry
    the very run being reaped. Re-checking in the ``DELETE``'s own ``WHERE`` makes
    the race impossible to lose — the row simply is not deleted, and ``False`` comes
    back for the report.

    ``run_events`` cascades. The workspace and the checkpoint thread do not, which
    is why they are the driver's earlier steps.
    """
    terminal = [s.value for s in TERMINAL_STATUSES]
    try:
        with conn.transaction(), conn.cursor() as cur:
            cur.execute(
                f"""
                DELETE FROM runs r
                 WHERE r.run_id = %(run_id)s
                   AND r.status = ANY(%(terminal)s)
                   AND r.updated_at < now() - make_interval(days => %(days)s)
                   {_LINEAGE_SAFE}
                """,  # noqa: S608 - _LINEAGE_SAFE is a constant, never caller input
                {"run_id": run_id, "terminal": terminal, "days": older_than_days},
            )
            return cur.rowcount == 1
    except psycopg.errors.UndefinedColumn as exc:
        _require_data_plane_column(exc, "delete_run")


def stale_terminal_thread_ids(
    conn: DbConnection,
    *,
    older_than_days: int = 30,
) -> list[str]:
    """Return checkpoint thread IDs whose terminal runs exceeded retention.

    Kept for the workspace-less lane (a v1 registry, where there is no durable
    folder to delete and retention is only rows plus checkpoints). The ordered
    driver built on :func:`retention_candidates` supersedes it wherever a run owns
    a workspace, because only that path can delete the three artifacts in an order
    that fails closed.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT thread_id FROM runs WHERE status = ANY(%s) "
            "AND updated_at < now() - make_interval(days => %s)",
            ([s.value for s in TERMINAL_STATUSES], older_than_days),
        )
        return [str(row["thread_id"]) for row in cur.fetchall()]


def prune_terminal_runs(conn: DbConnection, *, older_than_days: int = 30) -> int:
    """Bulk-delete settled registry rows past retention, after checkpoint cleanup.

    The maintenance entrypoint deletes checkpoint data first and calls this only
    once every stale thread has been cleared. Keeping registry deletion separate
    makes a checkpoint failure fail CLOSED: the run row remains available for a
    later retry instead of becoming an orphaned, untraceable checkpoint.

    Lineage-guarded for a concrete reason, not symmetry with the ordered driver:
    without the guard this statement takes out a failed attempt 1 whose retry is
    still *running* — deleting the row that names the workspace the live attempt is
    writing into. And if the surviving descendant is merely newer than the
    retention window, ``retry_of``'s foreign key aborts the whole statement, so one
    live lineage stops retention for every unrelated client. The guard turns both
    into a converging pass: leaves go now, their ancestors on a later one.

    On a v1 registry the guard is not merely unnecessary but unexpressible — there
    are no lineage columns to read, and no lineages either, since a workspace per
    attempt is a v2 concept. So v1 keeps the original statement exactly, which is
    the same expansion-window rule the rest of this module follows.
    """
    terminal = [s.value for s in TERMINAL_STATUSES]
    if not _data_plane_live(conn):
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM runs WHERE status = ANY(%s) "
                "AND updated_at < now() - make_interval(days => %s)",
                (terminal, older_than_days),
            )
            return cur.rowcount
    with conn.cursor() as cur:
        cur.execute(
            f"""
            DELETE FROM runs r
             WHERE r.status = ANY(%(terminal)s)
               AND r.updated_at < now() - make_interval(days => %(days)s)
               {_LINEAGE_SAFE}
            """,  # noqa: S608 - _LINEAGE_SAFE is a constant, never caller input
            {"terminal": terminal, "days": older_than_days},
        )
        return cur.rowcount
