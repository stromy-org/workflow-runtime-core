"""External-send outcomes — and the honest ``uncertain`` state.

The outbox answers "did we hand this to our own broker". This answers the
harder question: "did the PROVIDER accept it" — where the provider is Twilio, an
SMTP server, a WhatsApp Business API. That boundary has a failure mode the
internal one does not:

    We wrote the request. We never learned the outcome.

A timeout, a connection dropped after the bytes went out, or a crash while the
row said ``sending`` all leave a send whose result nobody can observe. There are
three things a system can do with that, and only one of them is defensible:

* **Retry blindly** — double-sends a WhatsApp message to a real person. The
  provider already has it; we just cannot prove it.
* **Assume success** — silently drops messages whenever the provider was the
  thing that broke.
* **Record it as what it is** — ``uncertain``, surfaced on an operator worklist,
  reconciled against the provider's own records.

This module does the third. That is the entire reason ``uncertain`` exists as a
first-class status rather than a comment on ``failed``, and it is why nothing in
this codebase claims exactly-once delivery.

A *definitive* rejection (the provider replied "no") is different and safely
retryable, because we know it did not go out.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..registry import DbConnection
from ._backoff import next_delay_seconds

RECEIPT_STATUSES = ("pending", "sending", "delivered", "failed", "uncertain")


@dataclass(frozen=True)
class DeliveryReceipt:
    service_namespace: str
    destination: str
    message_id: str
    status: str
    attempts: int
    next_attempt_at: datetime
    provider_ref: str | None
    lease_owner: str | None
    lease_expires_at: datetime | None
    last_error: str | None
    updated_at: datetime

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> DeliveryReceipt:
        return cls(
            service_namespace=row["service_namespace"],
            destination=row["destination"],
            message_id=row["message_id"],
            status=row["status"],
            attempts=int(row["attempts"]),
            next_attempt_at=row["next_attempt_at"],
            provider_ref=row["provider_ref"],
            lease_owner=row["lease_owner"],
            lease_expires_at=row["lease_expires_at"],
            last_error=row["last_error"],
            updated_at=row["updated_at"],
        )


def open_receipt(
    conn: DbConnection, *, service_namespace: str, destination: str, message_id: str
) -> None:
    """Register a send we are about to attempt, idempotently.

    Called before the first attempt so that a crash *between* registering and
    sending still leaves a row — an unattempted row is recoverable, whereas an
    unrecorded send is invisible.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO delivery_receipts (
                service_namespace, destination, message_id, status
            ) VALUES (%s, %s, %s, 'pending')
            ON CONFLICT (service_namespace, destination, message_id) DO NOTHING
            """,
            (service_namespace, destination, message_id),
        )


def claim_due(
    conn: DbConnection,
    *,
    service_namespace: str,
    destination: str,
    owner: str,
    lease_seconds: int,
    limit: int = 20,
) -> list[DeliveryReceipt]:
    """Claim receipts due for a send attempt on one destination.

    ``uncertain`` rows are deliberately NOT claimable. They are waiting on a
    human or a reconciliation job that can consult the provider; sweeping them
    back into the retry loop would be the blind retry this module exists to
    prevent.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH due AS (
                SELECT service_namespace, destination, message_id
                  FROM delivery_receipts
                 WHERE service_namespace = %s AND destination = %s
                   AND status IN ('pending', 'failed')
                   AND next_attempt_at <= now()
                 ORDER BY next_attempt_at
                 FOR UPDATE SKIP LOCKED
                 LIMIT %s
            )
            UPDATE delivery_receipts r
               SET status = 'sending',
                   attempts = r.attempts + 1,
                   lease_owner = %s,
                   lease_expires_at = now() + make_interval(secs => %s),
                   updated_at = now()
              FROM due
             WHERE r.service_namespace = due.service_namespace
               AND r.destination = due.destination
               AND r.message_id = due.message_id
            RETURNING r.*
            """,
            (service_namespace, destination, limit, owner, lease_seconds),
        )
        return [DeliveryReceipt.from_row(row) for row in cur.fetchall()]


def get_receipt(
    conn: DbConnection, *, service_namespace: str, destination: str, message_id: str
) -> DeliveryReceipt | None:
    """Read one receipt. The caller of :func:`claim` uses this to learn WHY."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT * FROM delivery_receipts
             WHERE service_namespace = %s AND destination = %s AND message_id = %s
            """,
            (service_namespace, destination, message_id),
        )
        row = cur.fetchone()
    return None if row is None else DeliveryReceipt.from_row(row)


def claim(
    conn: DbConnection,
    *,
    service_namespace: str,
    destination: str,
    message_id: str,
    owner: str,
    lease_seconds: int,
) -> DeliveryReceipt | None:
    """Claim ONE receipt by identity, for a consumer the broker pushes to.

    This is the duplicate-suppression seam. A push-based consumer holds the
    message in its hand and cannot choose which row to work on, so
    :func:`claim_due` — which picks whatever is due — cannot express it: it
    would claim rows whose payload the caller does not have and strand them
    under a lease until reconciliation wrongly called them ``uncertain``.

    ``None`` means "do not send", and the four reasons are all correct outcomes:

    * ``delivered`` — this is a redelivery of the at-least-once outbound queue.
      Refusing it here is precisely what stops a real person receiving a second
      copy. Nothing else in the pipeline can make that call, because nothing
      else knows the provider already accepted it.
    * ``uncertain`` — a human owns this now (see the module docstring).
    * ``sending`` — another consumer holds a live lease on it.
    * absent — the caller skipped :func:`open_receipt`.

    Call :func:`get_receipt` to tell those apart; ``delivered``/``uncertain``
    mean acknowledge the delivery, the other two mean leave it alone.

    Unlike :func:`claim_due` this deliberately IGNORES ``next_attempt_at``. On a
    push path the broker is already the retry scheduler — it decided this
    delivery was due — and honouring the row's backoff as well would leave two
    schedulers disagreeing about one message: the consumer would refuse the
    delivery it was just handed, ack nothing, and spin. The row's ``attempts``
    and backoff stay meaningful for the pull path and for operators; on the push
    path the queue's ``x-delivery-limit`` is what bounds the retries.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE delivery_receipts
               SET status = 'sending',
                   attempts = attempts + 1,
                   lease_owner = %s,
                   lease_expires_at = now() + make_interval(secs => %s),
                   updated_at = now()
             WHERE service_namespace = %s AND destination = %s AND message_id = %s
               AND (
                     status IN ('pending', 'failed')
                     -- A lease that has lapsed is reclaimable in the same
                     -- statement, so a consumer that died mid-send does not
                     -- block the redelivery behind a reconciliation pass.
                     OR (status = 'sending' AND lease_expires_at < now())
                   )
            RETURNING *
            """,
            (owner, lease_seconds, service_namespace, destination, message_id),
        )
        row = cur.fetchone()
    return None if row is None else DeliveryReceipt.from_row(row)


def _settle(
    conn: DbConnection,
    *,
    service_namespace: str,
    destination: str,
    message_id: str,
    owner: str,
    status: str,
    provider_ref: str | None,
    error: str | None,
    next_attempt_delay: float | None,
) -> bool:
    delay_clause = (
        "next_attempt_at = now() + make_interval(secs => %s),"
        if next_attempt_delay is not None
        else ""
    )
    params: list[Any] = [status, provider_ref, error[:2000] if error else None]
    if next_attempt_delay is not None:
        params.append(next_attempt_delay)
    params += [service_namespace, destination, message_id, owner]

    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE delivery_receipts
               SET status = %s,
                   provider_ref = COALESCE(%s, provider_ref),
                   last_error = %s,
                   {delay_clause}
                   lease_owner = NULL,
                   lease_expires_at = NULL,
                   updated_at = now()
             WHERE service_namespace = %s AND destination = %s AND message_id = %s
               AND lease_owner = %s AND status = 'sending'
            RETURNING message_id
            """,  # noqa: S608 - delay_clause is a fixed literal, never caller input
            tuple(params),
        )
        return cur.fetchone() is not None


def mark_delivered(
    conn: DbConnection,
    *,
    service_namespace: str,
    destination: str,
    message_id: str,
    owner: str,
    provider_ref: str | None = None,
) -> bool:
    """Record a provider-confirmed send. ``provider_ref`` is its own id for it."""
    return _settle(
        conn,
        service_namespace=service_namespace,
        destination=destination,
        message_id=message_id,
        owner=owner,
        status="delivered",
        provider_ref=provider_ref,
        error=None,
        next_attempt_delay=None,
    )


def mark_failed(
    conn: DbConnection,
    *,
    service_namespace: str,
    destination: str,
    message_id: str,
    owner: str,
    error: str,
    attempts: int,
) -> bool:
    """Record a DEFINITIVE provider rejection, and schedule a retry.

    Only call this when the provider actually answered. If the outcome is
    unobservable, call :func:`mark_uncertain` — the difference between the two
    is the difference between a safe retry and a duplicate message to a customer.
    """
    return _settle(
        conn,
        service_namespace=service_namespace,
        destination=destination,
        message_id=message_id,
        owner=owner,
        status="failed",
        provider_ref=None,
        error=error,
        next_attempt_delay=next_delay_seconds(attempts),
    )


def mark_uncertain(
    conn: DbConnection,
    *,
    service_namespace: str,
    destination: str,
    message_id: str,
    owner: str,
    reason: str,
    provider_ref: str | None = None,
) -> bool:
    """Record an unobservable outcome for reconciliation.

    Terminal for the automatic path by design: nothing retries this row. It
    leaves the loop and joins an operator worklist, because the only way to
    resolve it correctly is to consult the provider's own record of what it
    received.
    """
    return _settle(
        conn,
        service_namespace=service_namespace,
        destination=destination,
        message_id=message_id,
        owner=owner,
        status="uncertain",
        provider_ref=provider_ref,
        error=reason,
        next_attempt_delay=None,
    )


def reconcile_stale(conn: DbConnection, *, limit: int = 100) -> list[str]:
    """Move receipts stranded in ``sending`` past their lease to ``uncertain``.

    Note it does NOT return them to ``pending``. A crash mid-send is exactly the
    unobservable case: the request may well have reached the provider. Retrying
    would double-send; ``uncertain`` states the truth and asks for a human.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH stale AS (
                SELECT service_namespace, destination, message_id
                  FROM delivery_receipts
                 WHERE status = 'sending'
                   AND lease_expires_at IS NOT NULL
                   AND lease_expires_at <= now()
                 ORDER BY lease_expires_at
                 FOR UPDATE SKIP LOCKED
                 LIMIT %s
            )
            UPDATE delivery_receipts r
               SET status = 'uncertain',
                   last_error = COALESCE(
                       r.last_error,
                       'sender lease expired mid-send; provider outcome unobservable'),
                   lease_owner = NULL,
                   lease_expires_at = NULL,
                   updated_at = now()
              FROM stale
             WHERE r.service_namespace = stale.service_namespace
               AND r.destination = stale.destination
               AND r.message_id = stale.message_id
            RETURNING r.message_id
            """,
            (limit,),
        )
        return [str(row["message_id"]) for row in cur.fetchall()]


def list_uncertain(
    conn: DbConnection, *, service_namespace: str, limit: int = 100
) -> list[DeliveryReceipt]:
    """The operator worklist. Surfaced by the CLI and alerted on when non-empty."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT * FROM delivery_receipts
             WHERE service_namespace = %s AND status = 'uncertain'
             ORDER BY updated_at
             LIMIT %s
            """,
            (service_namespace, limit),
        )
        return [DeliveryReceipt.from_row(row) for row in cur.fetchall()]


def resolve_uncertain(
    conn: DbConnection,
    *,
    service_namespace: str,
    destination: str,
    message_id: str,
    delivered: bool,
    provider_ref: str | None = None,
    note: str | None = None,
) -> bool:
    """Settle an ``uncertain`` receipt after checking with the provider.

    The only exit from ``uncertain``, and deliberately explicit: someone (or a
    reconciliation job with provider API access) asserted what actually
    happened. Marking it ``failed`` returns it to the retry schedule, which is
    now safe *because* it was verified not to have been sent.
    """
    status = "delivered" if delivered else "failed"
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE delivery_receipts
               SET status = %s,
                   provider_ref = COALESCE(%s, provider_ref),
                   last_error = %s,
                   next_attempt_at = now(),
                   updated_at = now()
             WHERE service_namespace = %s AND destination = %s AND message_id = %s
               AND status = 'uncertain'
            RETURNING message_id
            """,
            (status, provider_ref, note, service_namespace, destination, message_id),
        )
        return cur.fetchone() is not None
