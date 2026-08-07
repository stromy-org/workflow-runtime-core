"""The dispatcher's durable record of asking a launcher to start a run.

Why a table and not just "start it when you submit it": launching is an *effect
on another system* with its own failure modes — a container service that is
throttling, a subprocess that cannot fork, an API that times out after it
already accepted the request. Effects like that need a retry schedule, an
attempt count and a lease, and none of those belong on the run itself (ORG-191
decision 2: a launch has its own retry lifecycle; a retry must be a child row,
not a mutation of the run).

**The launch is never the only idempotency barrier.** A launcher can time out
*after* the work started, so the same run can legitimately be launched twice.
The second barrier is the runner's own claim
(:func:`workflow_runtime_core.registry.claim_run`), which is a single-row
transition: whichever process claims first executes, the other exits cleanly.
This is also why the future ECS launcher passes ``run_id`` as its
``RunTask.clientToken`` — it collapses most double-launches — while never being
*trusted* to, because those tokens expire.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from ..registry import DbConnection
from ._backoff import next_delay_seconds

#: States a launch row can hold. ``pending`` and ``failed`` are both claimable —
#: the difference is only whether ``next_attempt_at`` has come round.
LAUNCH_STATES = ("pending", "launching", "launched", "failed")


def params_hash(params: dict[str, Any]) -> str:
    """Stable digest of the parameters a launch was made with.

    ``sort_keys`` so the same logical parameters hash identically regardless of
    dict order. Recorded so a redundant dispatch is *detectable*: two launch
    attempts with the same hash are a retry of one intent, whereas a differing
    hash means something changed the launch shape between attempts, which is a
    bug worth surfacing rather than silently accepting.
    """
    encoded = json.dumps(params, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@runtime_checkable
class Launcher(Protocol):
    """Starts the process that will execute one run.

    Implementations must be safe to call twice for the same ``run_id``: the
    dispatcher retries on ambiguous outcomes, and the runner claim is what
    actually enforces single execution.
    """

    #: Identifier recorded on the launch row, so a mixed fleet can be reconciled
    #: per launcher.
    name: str

    async def launch(self, run_id: str, params: dict[str, Any]) -> str:
        """Start the run and return an execution reference.

        The reference is whatever this launcher can later be *asked about* — a
        pid, a task ARN. Returning something unqueryable makes
        :meth:`is_alive` useless and forces recovery to wait for lease expiry.
        """
        ...

    async def is_alive(self, execution_ref: str) -> bool | None:
        """Whether that execution is still running.

        ``None`` means "cannot tell" — the honest answer for an expired ECS
        token or an unreachable control plane. Recovery treats unknown as
        *not recoverable yet* and waits for the lease, because assuming death
        is how a second writer gets onto a live run.
        """
        ...


@dataclass(frozen=True)
class LaunchRecord:
    run_id: str
    launcher: str
    state: str
    params_hash: str
    attempts: int
    next_attempt_at: datetime
    lease_owner: str | None
    lease_expires_at: datetime | None
    execution_ref: str | None
    last_error: str | None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> LaunchRecord:
        return cls(
            run_id=str(row["run_id"]),
            launcher=row["launcher"],
            state=row["state"],
            params_hash=row["params_hash"],
            attempts=int(row["attempts"]),
            next_attempt_at=row["next_attempt_at"],
            lease_owner=row["lease_owner"],
            lease_expires_at=row["lease_expires_at"],
            execution_ref=row["execution_ref"],
            last_error=row["last_error"],
        )


def get_launch(conn: DbConnection, run_id: str) -> LaunchRecord | None:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM run_launches WHERE run_id = %s", (run_id,))
        row = cur.fetchone()
    return None if row is None else LaunchRecord.from_row(row)


def claim_due(
    conn: DbConnection,
    *,
    owner: str,
    lease_seconds: int,
    limit: int = 10,
) -> list[LaunchRecord]:
    """Claim up to ``limit`` due launches for this dispatcher.

    ``FOR UPDATE SKIP LOCKED`` is what lets N dispatchers run concurrently
    without coordinating: each takes rows the others have not locked, and none
    of them block. A plain ``FOR UPDATE`` would serialise the whole fleet behind
    the slowest claim.

    The claim moves the row to ``launching`` and stamps a lease *in the same
    statement* as the selection, so a crash immediately after claiming leaves a
    row that reconciliation can recognise and recover rather than one that looks
    perpetually in-flight.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH due AS (
                SELECT run_id FROM run_launches
                 WHERE state IN ('pending', 'failed')
                   AND next_attempt_at <= now()
                 ORDER BY next_attempt_at
                 FOR UPDATE SKIP LOCKED
                 LIMIT %s
            )
            UPDATE run_launches l
               SET state = 'launching',
                   attempts = l.attempts + 1,
                   lease_owner = %s,
                   lease_expires_at = now() + make_interval(secs => %s),
                   updated_at = now()
              FROM due
             WHERE l.run_id = due.run_id
            RETURNING l.*
            """,
            (limit, owner, lease_seconds),
        )
        return [LaunchRecord.from_row(row) for row in cur.fetchall()]


def record_launched(
    conn: DbConnection, run_id: str, *, owner: str, execution_ref: str
) -> bool:
    """Record a successful launch and release the lease.

    Guarded on ``lease_owner``: a dispatcher whose lease already expired and was
    reclaimed must not overwrite the new owner's record. ``False`` means exactly
    that happened — the caller should stop, not retry.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE run_launches
               SET state = 'launched',
                   execution_ref = %s,
                   lease_owner = NULL,
                   lease_expires_at = NULL,
                   last_error = NULL,
                   updated_at = now()
             WHERE run_id = %s AND lease_owner = %s AND state = 'launching'
            RETURNING run_id
            """,
            (execution_ref, run_id, owner),
        )
        return cur.fetchone() is not None


def record_failed(
    conn: DbConnection,
    run_id: str,
    *,
    owner: str,
    error: str,
    attempts: int | None = None,
) -> bool:
    """Record a failed launch and schedule the retry.

    The next attempt is scheduled with jittered exponential backoff rather than
    immediately: a launcher that just failed is usually failing for everyone,
    and an unbacked-off retry loop turns one downstream problem into a second.
    """
    if attempts is None:
        current = get_launch(conn, run_id)
        attempts = current.attempts if current else 1
    delay = next_delay_seconds(attempts)
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE run_launches
               SET state = 'failed',
                   last_error = %s,
                   next_attempt_at = now() + make_interval(secs => %s),
                   lease_owner = NULL,
                   lease_expires_at = NULL,
                   updated_at = now()
             WHERE run_id = %s AND lease_owner = %s AND state = 'launching'
            RETURNING run_id
            """,
            (error[:2000], delay, run_id, owner),
        )
        return cur.fetchone() is not None


def reconcile_stale(conn: DbConnection, *, limit: int = 100) -> list[str]:
    """Return launches stranded in ``launching`` past their lease to ``pending``.

    This is the recovery path for a dispatcher that died between claiming a row
    and recording an outcome. It is safe *because* it waits for lease expiry:
    the previous dispatcher either already recorded its outcome (so the row is
    no longer ``launching``) or is genuinely gone.

    It does NOT ask the launcher whether the execution is alive — that check
    belongs to the caller, which has the launcher instance. A caller that can
    answer :meth:`Launcher.is_alive` should do so before calling this, because a
    live execution whose dispatcher died needs its reference preserved, not a
    second launch. Returns the run ids it reset so the caller can log them.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH stale AS (
                SELECT run_id FROM run_launches
                 WHERE state = 'launching'
                   AND lease_expires_at IS NOT NULL
                   AND lease_expires_at <= now()
                 ORDER BY lease_expires_at
                 FOR UPDATE SKIP LOCKED
                 LIMIT %s
            )
            UPDATE run_launches l
               SET state = 'pending',
                   lease_owner = NULL,
                   lease_expires_at = NULL,
                   last_error = COALESCE(l.last_error, 'dispatcher lease expired'),
                   updated_at = now()
              FROM stale
             WHERE l.run_id = stale.run_id
            RETURNING l.run_id
            """,
            (limit,),
        )
        return [str(row["run_id"]) for row in cur.fetchall()]


def adopt_live_execution(
    conn: DbConnection, run_id: str, *, execution_ref: str
) -> bool:
    """Mark a stale-but-alive launch as launched instead of relaunching it.

    The counterpart to :func:`reconcile_stale` for the case where the launcher
    CAN prove the execution survived its dispatcher. Relaunching there would
    create a second process for one run and rely entirely on the runner claim to
    clean up after it — which works, but wastes a container start and muddies
    every metric that counts launches.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE run_launches
               SET state = 'launched',
                   execution_ref = %s,
                   lease_owner = NULL,
                   lease_expires_at = NULL,
                   updated_at = now()
             WHERE run_id = %s AND state IN ('launching', 'pending', 'failed')
            RETURNING run_id
            """,
            (execution_ref, run_id),
        )
        return cur.fetchone() is not None
