"""Transactional outbox — the reply survives the crash that follows the work.

A run's terminal state and its outbound messages are written in ONE transaction.
Without that, the window between "the graph finished" and "the reply was sent"
is a place where work is silently lost: the run reads ``completed``, the caller
never hears anything, and nothing in the system records that a message was owed.

What this does NOT claim is exactly-once delivery. Publishing to a broker and
marking the row delivered are two systems, so a crash between them redelivers
the same ``message_id``. That is why the id is *stable* and unique per
namespace: downstream consumers deduplicate on it. At-least-once with a stable
identity is achievable and honest; exactly-once across a process boundary is
not, and the word does not appear in this codebase.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..registry import DbConnection
from ._backoff import next_delay_seconds

OUTBOX_STATUSES = ("pending", "publishing", "delivered", "failed")


def _empty_payload() -> dict[str, Any]:
    """Typed factory — see the note in :mod:`.envelope`."""
    return {}


class PublishError(RuntimeError):
    """A publish attempt failed, including an unroutable confirmed return.

    An unroutable return is a *failure*, not a success with a warning: the
    broker confirmed it accepted the message and then told us there was no queue
    to put it in. Treating that as delivered is how a reply silently evaporates
    while every dashboard stays green.
    """


@dataclass(frozen=True)
class OutboxMessage:
    """One durable outbound message.

    ``message_id`` is supplied by the producer, not generated here, because it
    must be *derivable from the work* — a re-finalized run has to produce the
    same id it produced the first time, or recovery duplicates the reply.
    """

    service_namespace: str
    message_id: str
    destination: str
    payload: dict[str, Any] = field(default_factory=_empty_payload)
    run_id: str | None = None


@dataclass(frozen=True)
class OutboxRecord:
    outbox_id: str
    service_namespace: str
    message_id: str
    destination: str
    payload: dict[str, Any]
    run_id: str | None
    status: str
    attempts: int
    next_attempt_at: datetime
    lease_owner: str | None
    lease_expires_at: datetime | None
    last_error: str | None
    delivered_at: datetime | None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> OutboxRecord:
        return cls(
            outbox_id=str(row["outbox_id"]),
            service_namespace=row["service_namespace"],
            message_id=row["message_id"],
            destination=row["destination"],
            payload=row["payload_json"] or {},
            run_id=str(row["run_id"]) if row["run_id"] is not None else None,
            status=row["status"],
            attempts=int(row["attempts"]),
            next_attempt_at=row["next_attempt_at"],
            lease_owner=row["lease_owner"],
            lease_expires_at=row["lease_expires_at"],
            last_error=row["last_error"],
            delivered_at=row["delivered_at"],
        )


def enqueue(conn: DbConnection, message: OutboxMessage) -> str:
    """Insert one outbound message, idempotently on ``message_id``.

    ``ON CONFLICT DO NOTHING`` is the whole point: finalizing a run twice — which
    recovery does by design, since a terminal snapshot can be re-projected — must
    produce one message, not two. Returns the existing ``outbox_id`` on conflict
    so the caller cannot tell the difference, which is exactly the property that
    makes the finalizer safe to re-run.
    """
    outbox_id = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO event_outbox (
                outbox_id, service_namespace, message_id, run_id,
                destination, payload_json, status
            ) VALUES (%s, %s, %s, %s, %s, %s, 'pending')
            ON CONFLICT (service_namespace, message_id) DO NOTHING
            RETURNING outbox_id
            """,
            (
                outbox_id,
                message.service_namespace,
                message.message_id,
                message.run_id,
                message.destination,
                json.dumps(message.payload, default=str),
            ),
        )
        row = cur.fetchone()
        if row is not None:
            return str(row["outbox_id"])

        cur.execute(
            "SELECT outbox_id FROM event_outbox WHERE service_namespace = %s AND message_id = %s",
            (message.service_namespace, message.message_id),
        )
        existing = cur.fetchone()
    if existing is None:  # pragma: no cover - would mean the row vanished mid-statement
        raise RuntimeError(
            f"outbox message {message.message_id!r} neither inserted nor found"
        )
    return str(existing["outbox_id"])


def enqueue_all(conn: DbConnection, messages: tuple[OutboxMessage, ...]) -> list[str]:
    """Enqueue a projection's messages. Call inside the terminal transaction."""
    return [enqueue(conn, m) for m in messages]


def claim_due(
    conn: DbConnection,
    *,
    service_namespace: str,
    owner: str,
    lease_seconds: int,
    limit: int = 20,
) -> list[OutboxRecord]:
    """Claim due messages for one egress replica.

    Scoped to a namespace so one service's backlog cannot starve another's, and
    ``SKIP LOCKED`` so replicas scale horizontally without coordination.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH due AS (
                SELECT outbox_id FROM event_outbox
                 WHERE service_namespace = %s
                   AND status IN ('pending', 'failed')
                   AND next_attempt_at <= now()
                 ORDER BY next_attempt_at
                 FOR UPDATE SKIP LOCKED
                 LIMIT %s
            )
            UPDATE event_outbox o
               SET status = 'publishing',
                   attempts = o.attempts + 1,
                   lease_owner = %s,
                   lease_expires_at = now() + make_interval(secs => %s)
              FROM due
             WHERE o.outbox_id = due.outbox_id
            RETURNING o.*
            """,
            (service_namespace, limit, owner, lease_seconds),
        )
        return [OutboxRecord.from_row(row) for row in cur.fetchall()]


def mark_delivered(conn: DbConnection, outbox_id: str, *, owner: str) -> bool:
    """Mark delivered. Call ONLY after a routable publisher confirmation.

    Ordering matters and is not negotiable: confirm first, then mark. Marking
    first and crashing loses the message entirely, whereas confirming first and
    crashing redelivers it — and redelivery is survivable because the id is
    stable.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE event_outbox
               SET status = 'delivered',
                   delivered_at = now(),
                   lease_owner = NULL,
                   lease_expires_at = NULL,
                   last_error = NULL
             WHERE outbox_id = %s AND lease_owner = %s AND status = 'publishing'
            RETURNING outbox_id
            """,
            (outbox_id, owner),
        )
        return cur.fetchone() is not None


def mark_failed(
    conn: DbConnection, outbox_id: str, *, owner: str, error: str, attempts: int
) -> bool:
    """Return a message to the retry schedule after a failed publish."""
    delay = next_delay_seconds(attempts)
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE event_outbox
               SET status = 'failed',
                   last_error = %s,
                   next_attempt_at = now() + make_interval(secs => %s),
                   lease_owner = NULL,
                   lease_expires_at = NULL
             WHERE outbox_id = %s AND lease_owner = %s AND status = 'publishing'
            RETURNING outbox_id
            """,
            (error[:2000], delay, outbox_id, owner),
        )
        return cur.fetchone() is not None


def reconcile_stale(conn: DbConnection, *, limit: int = 100) -> list[str]:
    """Recover messages stranded in ``publishing`` past their lease.

    The crashed publisher may or may not have got a confirmation, so the message
    goes back to the retry schedule and may be delivered twice. Stable
    ``message_id`` is what makes that acceptable — and is why this function does
    not try to be cleverer.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH stale AS (
                SELECT outbox_id FROM event_outbox
                 WHERE status = 'publishing'
                   AND lease_expires_at IS NOT NULL
                   AND lease_expires_at <= now()
                 ORDER BY lease_expires_at
                 FOR UPDATE SKIP LOCKED
                 LIMIT %s
            )
            UPDATE event_outbox o
               SET status = 'failed',
                   lease_owner = NULL,
                   lease_expires_at = NULL,
                   last_error = COALESCE(o.last_error, 'publisher lease expired'),
                   next_attempt_at = now()
              FROM stale
             WHERE o.outbox_id = stale.outbox_id
            RETURNING o.outbox_id
            """,
            (limit,),
        )
        return [str(row["outbox_id"]) for row in cur.fetchall()]


def pending_depth(conn: DbConnection, *, service_namespace: str) -> int:
    """Count of messages still owed. Exported as a metric; an alert keys on age."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*) AS n FROM event_outbox
             WHERE service_namespace = %s AND status IN ('pending', 'failed', 'publishing')
            """,
            (service_namespace,),
        )
        row = cur.fetchone()
    return 0 if row is None else int(row["n"])


def oldest_pending_age_seconds(
    conn: DbConnection, *, service_namespace: str
) -> float | None:
    """Age of the oldest undelivered message, or ``None`` when the outbox is clear.

    The metric that actually catches a stuck egress. Depth alone does not: a
    steady depth of 5 is healthy throughput, while a depth of 1 that is four
    hours old is an outage.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT EXTRACT(EPOCH FROM (now() - min(created_at))) AS age
              FROM event_outbox
             WHERE service_namespace = %s AND status IN ('pending', 'failed', 'publishing')
            """,
            (service_namespace,),
        )
        row = cur.fetchone()
    if row is None or row["age"] is None:
        return None
    return float(row["age"])


def purge_delivered(
    conn: DbConnection, *, service_namespace: str, older_than_days: int = 30
) -> int:
    """Delete delivered messages past the retention window."""
    with conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM event_outbox
             WHERE service_namespace = %s
               AND status = 'delivered'
               AND delivered_at < now() - make_interval(days => %s)
            """,
            (service_namespace, older_than_days),
        )
        return cur.rowcount
