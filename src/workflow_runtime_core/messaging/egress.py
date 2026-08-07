"""The egress pump — claim, publish, settle.

Transport-agnostic on purpose: it takes an async ``publish`` callable rather
than a broker client, so the claim/settle logic has exactly one implementation
whether the lane is RabbitMQ or a cloud queue. It also keeps this module in the
BASE install — a consumer can drive egress without ``aio-pika`` on the path.

The ordering is the contract, and it is not symmetric:

    publish -> (confirmed) -> mark_delivered

Marking first and crashing loses the message outright. Confirming first and
crashing redelivers it, which is survivable because the ``message_id`` is
stable and consumers deduplicate on it. Given a choice between "might send
twice" and "might never send", a system that talks to customers picks the
former every time — and says so, rather than claiming exactly-once.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from ..registry import DbConnection
from . import outbox
from .outbox import OutboxRecord, PublishError

logger = logging.getLogger(__name__)

#: How long a claim is held. Long enough to cover a slow publish, short enough
#: that a crashed replica's backlog is recovered promptly.
DEFAULT_LEASE_SECONDS = 60


@dataclass(frozen=True)
class DrainResult:
    claimed: int
    delivered: int
    failed: int

    @property
    def idle(self) -> bool:
        """Nothing was due. The caller sleeps instead of spinning."""
        return self.claimed == 0


async def drain_once(
    conn: DbConnection,
    publish: Callable[[OutboxRecord], Awaitable[None]],
    *,
    service_namespace: str,
    owner: str,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    limit: int = 20,
) -> DrainResult:
    """Claim a batch, publish each, and settle each independently.

    One message's failure never aborts the batch: a single unroutable
    destination would otherwise strand every other reply claimed alongside it,
    turning one misconfiguration into a total outage of the lane.
    """
    claimed = outbox.claim_due(
        conn,
        service_namespace=service_namespace,
        owner=owner,
        lease_seconds=lease_seconds,
        limit=limit,
    )
    delivered = 0
    failed = 0

    for record in claimed:
        try:
            await publish(record)
        except PublishError as exc:
            failed += 1
            outbox.mark_failed(
                conn,
                record.outbox_id,
                owner=owner,
                error=str(exc),
                attempts=record.attempts,
            )
            logger.warning(
                "outbox %s -> %s failed: %s",
                record.message_id,
                record.destination,
                exc,
            )
            continue

        # Confirmed and routable. A crash between here and the UPDATE redelivers
        # the same stable message_id, which is the accepted cost.
        if outbox.mark_delivered(conn, record.outbox_id, owner=owner):
            delivered += 1
        else:
            # The lease lapsed mid-publish and someone else owns the row now.
            # It will be published again; nothing to do but say so.
            logger.warning(
                "outbox %s was published but the lease had lapsed; another "
                "replica now owns the row and may publish it again",
                record.message_id,
            )

    return DrainResult(claimed=len(claimed), delivered=delivered, failed=failed)
