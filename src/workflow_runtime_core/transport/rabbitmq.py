"""RabbitMQ ingress and egress — delivery-bearing in, confirmed out.

Two rules shape this whole module, and both were learned the expensive way.

**Acknowledgement lives on the delivery, never on the envelope.** A consumer can
only acknowledge through the object that carries the delivery tag. An earlier
design passed an ``AsyncIterator[Envelope]`` around and offered a separate
``ack(envelope)``; a real-broker probe on 2026-07-24 showed that cannot work —
the tag is not in the envelope, and reconstructing the association is a bug
waiting to happen. So the handler receives the delivery, and the *only* thing
that may acknowledge it is code holding it.

**An unroutable publish is a failure.** With ``mandatory=True`` the broker
confirms it accepted the message and then returns it because no queue was bound.
``on_return_raises=True`` turns that return into an exception. Without it, the
publish looks successful, the outbox row is marked delivered, and the reply is
gone while every dashboard stays green.

Requires the ``rabbitmq`` extra.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, TypeVar

from ..exceptions import DependencyError
from ..messaging.envelope import Envelope, EnvelopeError
from ..messaging.outbox import OutboxRecord, PublishError
from .topology import BrokerTopology

if TYPE_CHECKING:  # pragma: no cover - typing only
    from aio_pika.abc import AbstractIncomingMessage, AbstractRobustConnection

logger = logging.getLogger(__name__)

#: Redeliveries before the broker dead-letters a message. Finite by design: an
#: unbounded retry of a message that always fails is an infinite loop with a
#: queue in the middle.
DEFAULT_DELIVERY_LIMIT = 5

#: Unacknowledged messages one consumer will hold. Bounded so a single consumer
#: cannot claim the whole backlog and then die with it invisible.
DEFAULT_PREFETCH = 16


_T = TypeVar("_T")


class NotConnected(RuntimeError):
    """A transport method was used before ``connect()`` established the channel."""


def _require(value: _T | None, what: str) -> _T:
    """Unwrap a post-connect attribute, or fail with a message that names it.

    Not an ``assert``: asserts vanish under ``python -O``, which would turn a
    clear "you forgot to connect" into an ``AttributeError`` on ``None`` deep
    inside a publish path.
    """
    if value is None:
        raise NotConnected(f"{what} is unavailable — call connect() first")
    return value


def _aio_pika() -> Any:
    try:
        import aio_pika
    except ImportError as exc:  # pragma: no cover - exercised by the extras test
        raise DependencyError("rabbitmq", "aio-pika") from exc
    return aio_pika


def _quorum_args(*, delivery_limit: int, dlx: str | None = None) -> dict[str, Any]:
    """Arguments making a queue durable, quorum-backed and poison-bounded.

    Quorum rather than classic: these queues carry the only record of an inbound
    message between the broker accepting it and the database committing it, so
    surviving a node failure is the entire point.
    """
    args: dict[str, Any] = {
        "x-queue-type": "quorum",
        "x-delivery-limit": delivery_limit,
    }
    if dlx is not None:
        args["x-dead-letter-exchange"] = dlx
    return args


class RabbitMQIngress:
    """Consumes inbound deliveries and hands each to a durable handler.

    The handler is expected to persist and commit (``submit_event``). The
    delivery is acknowledged only after it returns — that ordering IS the
    at-least-once guarantee, and reversing it is how messages get lost.
    """

    def __init__(
        self,
        url: str,
        topology: BrokerTopology,
        *,
        prefetch: int = DEFAULT_PREFETCH,
        delivery_limit: int = DEFAULT_DELIVERY_LIMIT,
    ) -> None:
        self.url = url
        self.topology = topology
        self.prefetch = prefetch
        self.delivery_limit = delivery_limit
        self._connection: AbstractRobustConnection | None = None
        self._channel: Any = None
        self._queue: Any = None
        self._error_exchange: Any = None
        self._stopping = asyncio.Event()

    async def connect(self) -> None:
        aio_pika = _aio_pika()
        connection = await aio_pika.connect_robust(self.url)
        self._connection = connection
        self._channel = await connection.channel()
        await self._channel.set_qos(prefetch_count=self.prefetch)

        t = self.topology
        dlx = await self._channel.declare_exchange(
            t.dead_letter_exchange, aio_pika.ExchangeType.FANOUT, durable=True
        )
        dlq = await self._channel.declare_queue(
            t.dead_letter_queue,
            durable=True,
            arguments=_quorum_args(delivery_limit=self.delivery_limit),
        )
        await dlq.bind(dlx)

        inbound = await self._channel.declare_exchange(
            t.inbound_exchange, aio_pika.ExchangeType.TOPIC, durable=True
        )
        self._queue = await self._channel.declare_queue(
            t.inbound_queue,
            durable=True,
            arguments=_quorum_args(
                delivery_limit=self.delivery_limit, dlx=t.dead_letter_exchange
            ),
        )
        await self._queue.bind(inbound, routing_key="#")

        # The error queue is declared on the DEFAULT exchange so a parse failure
        # can be parked by routing key alone, without depending on a binding
        # that may itself be misconfigured.
        self._error_exchange = self._channel.default_exchange
        await self._channel.declare_queue(
            t.error_queue,
            durable=True,
            arguments=_quorum_args(delivery_limit=self.delivery_limit),
        )

    async def close(self) -> None:
        self._stopping.set()
        if self._connection is not None:
            await self._connection.close()
            self._connection = None

    async def _park_unparseable(
        self, message: AbstractIncomingMessage, *, code: str, reason: str
    ) -> None:
        """Republish a message we cannot parse, then let the caller ack it.

        Published with confirmation BEFORE the original is acknowledged: parking
        it is the whole reason we are allowed to drop the original, so an
        unconfirmed park would trade a poison message for a lost one.
        """
        aio_pika = _aio_pika()
        error_exchange = _require(self._error_exchange, "ingress error exchange")
        await error_exchange.publish(
            aio_pika.Message(
                body=message.body,
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                headers={
                    "x-error-code": code,
                    "x-error-reason": reason[:500],
                    "x-original-routing-key": message.routing_key or "",
                },
            ),
            routing_key=self.topology.error_queue,
        )

    async def consume(
        self, handler: Callable[[Envelope], Awaitable[None]]
    ) -> None:
        """Consume until :meth:`close`. Acknowledges only after ``handler`` returns.

        Three outcomes, three different behaviours:

        * **parsed and handled** — acknowledge; the work is durable.
        * **unparseable** — park it on the error queue with a machine-readable
          code, confirmed, then acknowledge. Retrying a malformed payload can
          never succeed.
        * **handler raised** — do NOT acknowledge and do NOT ``nack(requeue=True)``.
          The transient case is a database that is down, and an immediate requeue
          spins the broker at full speed against it. Leaving the delivery
          unacknowledged lets the broker redeliver on its own terms, and the
          delivery limit eventually dead-letters a message that never succeeds.
        """
        if self._queue is None:
            await self.connect()
        queue = _require(self._queue, "ingress queue")

        async with queue.iterator() as messages:
            async for message in messages:
                if self._stopping.is_set():
                    break
                await self._handle_one(message, handler)

    async def _handle_one(
        self,
        message: AbstractIncomingMessage,
        handler: Callable[[Envelope], Awaitable[None]],
    ) -> None:
        try:
            raw = json.loads(message.body)
            envelope = Envelope.from_dict(raw)
            envelope.validate()
        except (ValueError, KeyError, TypeError, EnvelopeError) as exc:
            logger.warning(
                "parking unparseable delivery on %s: %s",
                self.topology.error_queue,
                exc,
            )
            await self._park_unparseable(
                message, code="ENVELOPE_INVALID", reason=str(exc)
            )
            await message.ack()
            return

        try:
            await handler(envelope)
        except Exception:
            # Deliberately no ack and no nack. See the docstring: an immediate
            # requeue hot-loops against whatever is already broken.
            logger.exception(
                "handler failed for %s/%s; leaving the delivery unacknowledged",
                envelope.source,
                envelope.source_message_id,
            )
            return

        await message.ack()


class RabbitMQEgress:
    """Publishes outbox messages with mandatory routing and publisher confirms."""

    def __init__(self, url: str, topology: BrokerTopology) -> None:
        self.url = url
        self.topology = topology
        self._connection: AbstractRobustConnection | None = None
        self._channel: Any = None
        self._exchange: Any = None

    async def connect(self) -> None:
        aio_pika = _aio_pika()
        connection = await aio_pika.connect_robust(self.url)
        self._connection = connection
        # publisher_confirms: the broker must acknowledge it took the message.
        # on_return_raises: an unroutable mandatory publish becomes an exception
        # instead of a silent success. Both are required for `delivered` to mean
        # anything.
        self._channel = await connection.channel(
            publisher_confirms=True, on_return_raises=True
        )
        self._exchange = await self._channel.declare_exchange(
            self.topology.outbound_exchange, aio_pika.ExchangeType.TOPIC, durable=True
        )

    async def declare_destination(self, destination: str) -> None:
        """Bind a queue for one destination. Unbound destinations are unroutable."""
        channel = _require(self._channel, "egress channel")
        queue = await channel.declare_queue(
            self.topology.destination_queue(destination),
            durable=True,
            arguments=_quorum_args(delivery_limit=DEFAULT_DELIVERY_LIMIT),
        )
        await queue.bind(_require(self._exchange, "egress exchange"), routing_key=destination)

    async def close(self) -> None:
        if self._connection is not None:
            await self._connection.close()
            self._connection = None

    async def publish(self, record: OutboxRecord) -> None:
        """Publish one outbox row, or raise :class:`PublishError`.

        Returning normally means the broker confirmed a ROUTABLE publish. Only
        then may the caller mark the row delivered.
        """
        aio_pika = _aio_pika()
        if self._exchange is None:
            await self.connect()
        exchange = _require(self._exchange, "egress exchange")

        body = json.dumps(record.payload, default=str).encode("utf-8")
        try:
            await exchange.publish(
                aio_pika.Message(
                    body=body,
                    delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                    # The stable identity downstream consumers deduplicate on.
                    # At-least-once transport is only honest if this survives a
                    # redelivery unchanged.
                    message_id=record.message_id,
                    content_type="application/json",
                ),
                routing_key=record.destination,
                mandatory=True,
            )
        except aio_pika.exceptions.DeliveryError as exc:
            raise PublishError(
                f"message {record.message_id!r} was returned as unroutable for "
                f"destination {record.destination!r}: no queue is bound. The broker "
                "accepted and then returned it, so this is a FAILED attempt — the "
                "row stays retryable and must not be marked delivered."
            ) from exc
        except Exception as exc:  # connection loss, channel error, timeout
            raise PublishError(
                f"publishing {record.message_id!r} to {record.destination!r} failed: {exc}"
            ) from exc
