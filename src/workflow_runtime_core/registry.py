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
from typing import Any, cast

import psycopg
from psycopg.rows import dict_row

from .exceptions import RegistryError
from .models import TERMINAL_STATUSES, RunRecord, RunStatus

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
) -> RunRecord:
    """Register a queued run. Returns the EXISTING run on idempotency-key reuse.

    ``run_id`` may be supplied by a caller that must know the id before the row
    exists (the facade mints one to render its job template). It stays optional
    so the Stromy path keeps its original signature.
    """
    if idempotency_key:
        existing = find_by_idempotency_key(conn, idempotency_key)
        if existing is not None:
            return existing

    resolved_run_id = run_id or new_run_id()
    try:
        # Savepoint: the find_by_idempotency_key pre-check above is a TOCTOU race
        # — two concurrent callers with the same key both see "not found" and
        # both INSERT; the loser hits runs_idempotency_key_uniq. Isolating the
        # INSERT in a savepoint means that UniqueViolation rolls back only this
        # statement, leaving the caller's outer transaction usable for the
        # re-fetch below (a bare INSERT would poison the whole transaction).
        with conn.transaction(), conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO runs (run_id, workflow, thread_id, status, client_slug,
                                  config_json, image_tag, job_template_json,
                                  idempotency_key)
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
    except psycopg.errors.UniqueViolation:
        # The concurrent winner already created the run under this key — return
        # theirs, which is exactly what an idempotency key promises. Only
        # reachable with a key set (the unique index is partial on NOT NULL).
        existing = find_by_idempotency_key(conn, idempotency_key) if idempotency_key else None
        if existing is not None:
            return existing
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


def mark_paused(conn: DbConnection, run_id: str, interrupt_payload: Any) -> None:
    """Run hit an ``interrupt()`` and is exiting. State lives in the checkpoint."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE runs SET status = %s, interrupt_payload = %s, updated_at = now() "
            "WHERE run_id = %s",
            (RunStatus.PAUSED.value, json.dumps(interrupt_payload), run_id),
        )
    _emit(conn, run_id, "paused")


def mark_completed(
    conn: DbConnection, run_id: str, artifacts: dict[str, Any] | None = None
) -> None:
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
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE runs SET status = %s, error = %s, updated_at = now() WHERE run_id = %s",
            (RunStatus.FAILED.value, error[:8000], run_id),
        )
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
