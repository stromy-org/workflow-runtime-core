"""Ingress and egress against a REAL RabbitMQ 4.

Mocks are useless for the properties that matter here. "Does an unroutable
mandatory publish raise?" and "is an unacknowledged delivery redelivered?" are
statements about the broker, and a fake that returns whatever we expect proves
only that we wrote the fake to match our belief. The 2026-07-24 probe that
retired the ``AsyncIterator[Envelope]`` interface made exactly that point.

The broker comes from ``STROMY_AMQP_URL`` when set, else testcontainers.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid

import pytest

from workflow_runtime_core import registry
from workflow_runtime_core.messaging import Envelope, OutboxMessage, inbox, outbox
from workflow_runtime_core.messaging.egress import drain_once
from workflow_runtime_core.messaging.outbox import PublishError
from workflow_runtime_core.migrations import apply_migrations
from workflow_runtime_core.transport.rabbitmq import RabbitMQEgress, RabbitMQIngress
from workflow_runtime_core.transport.topology import BrokerTopology

pytestmark = pytest.mark.integration

_AMQP_ENV = "STROMY_AMQP_URL"


@pytest.fixture(scope="session")
def amqp_url():
    provided = os.environ.get(_AMQP_ENV, "").strip()
    if provided:
        yield provided
        return
    testcontainers = pytest.importorskip(
        "testcontainers.rabbitmq",
        reason=f"neither {_AMQP_ENV} nor testcontainers[rabbitmq] is available",
    )
    with testcontainers.RabbitMqContainer("rabbitmq:4-management") as broker:
        host = broker.get_container_host_ip()
        port = broker.get_exposed_port(5672)
        yield f"amqp://guest:guest@{host}:{port}/"


@pytest.fixture
def ns() -> str:
    """A fresh namespace per test, so broker entities never collide."""
    return f"svc-{uuid.uuid4().hex[:10]}"


def _migrated(dsn: str) -> None:
    with registry.connect(dsn) as conn:
        apply_migrations(conn)


def _envelope(ns: str, message_id: str = "wamid.1") -> Envelope:
    return Envelope(
        service_namespace=ns,
        source="whatsapp",
        source_message_id=message_id,
        workflow="triage",
        payload={"text": "hello"},
    )


# --- ingress ------------------------------------------------------------------


async def test_a_delivery_is_acked_only_after_the_run_is_committed(
    amqp_url: str, blank_dsn: str, ns: str
) -> None:
    """The whole at-least-once guarantee in one assertion: the message leaves the
    queue only once its run exists durably."""
    _migrated(blank_dsn)
    topology = BrokerTopology(ns)
    ingress = RabbitMQIngress(amqp_url, topology)
    await ingress.connect()

    handled = asyncio.Event()

    async def handler(envelope: Envelope) -> None:
        with registry.connect(blank_dsn) as conn:
            inbox.submit_event(
                conn, envelope, launcher="subprocess", params_hash="h0"
            )
        handled.set()

    consumer = asyncio.create_task(ingress.consume(handler))
    await _publish_inbound(amqp_url, topology, _envelope(ns))

    await asyncio.wait_for(handled.wait(), timeout=20)
    await asyncio.sleep(0.5)  # let the ack round-trip
    await ingress.close()
    consumer.cancel()

    with registry.connect(blank_dsn) as conn:
        runs = registry.list_runs(conn)
    assert len(runs) == 1
    assert runs[0].workflow == "triage"


async def test_a_redelivered_message_does_not_create_a_second_run(
    amqp_url: str, blank_dsn: str, ns: str
) -> None:
    """Two deliveries carrying the same channel id — what a broker redelivery
    looks like — must converge on one run."""
    _migrated(blank_dsn)
    topology = BrokerTopology(ns)
    ingress = RabbitMQIngress(amqp_url, topology)
    await ingress.connect()

    seen = asyncio.Semaphore(0)

    async def handler(envelope: Envelope) -> None:
        with registry.connect(blank_dsn) as conn:
            inbox.submit_event(
                conn, envelope, launcher="subprocess", params_hash="h0"
            )
        seen.release()

    consumer = asyncio.create_task(ingress.consume(handler))
    await _publish_inbound(amqp_url, topology, _envelope(ns))
    await _publish_inbound(amqp_url, topology, _envelope(ns))

    await asyncio.wait_for(seen.acquire(), timeout=20)
    await asyncio.wait_for(seen.acquire(), timeout=20)
    await ingress.close()
    consumer.cancel()

    with registry.connect(blank_dsn) as conn:
        assert len(registry.list_runs(conn)) == 1


async def test_an_unparseable_payload_is_parked_not_retried_forever(
    amqp_url: str, blank_dsn: str, ns: str
) -> None:
    """A malformed body can never succeed, so it is republished once to the
    namespaced error queue with a machine-readable code and then acknowledged."""
    _migrated(blank_dsn)
    topology = BrokerTopology(ns)
    ingress = RabbitMQIngress(amqp_url, topology)
    await ingress.connect()

    async def handler(envelope: Envelope) -> None:  # pragma: no cover - never called
        raise AssertionError("handler must not see an unparseable payload")

    consumer = asyncio.create_task(ingress.consume(handler))
    await _publish_raw(amqp_url, topology, b"{not json at all")
    await asyncio.sleep(2)
    await ingress.close()
    consumer.cancel()

    parked = await _drain_queue(amqp_url, topology.error_queue)
    assert len(parked) == 1
    assert parked[0]["headers"]["x-error-code"] == "ENVELOPE_INVALID"


async def test_a_failing_handler_leaves_the_delivery_unacknowledged(
    amqp_url: str, blank_dsn: str, ns: str
) -> None:
    """The database is down case. The message must stay on the broker — and must
    NOT be hot-requeued, which would spin against the thing already broken."""
    _migrated(blank_dsn)
    topology = BrokerTopology(ns)
    ingress = RabbitMQIngress(amqp_url, topology)
    await ingress.connect()

    attempted = asyncio.Event()

    async def handler(envelope: Envelope) -> None:
        attempted.set()
        raise RuntimeError("registry unreachable")

    consumer = asyncio.create_task(ingress.consume(handler))
    await _publish_inbound(amqp_url, topology, _envelope(ns))
    await asyncio.wait_for(attempted.wait(), timeout=20)
    await asyncio.sleep(0.5)

    # Closing returns the unacknowledged delivery to the queue.
    await ingress.close()
    consumer.cancel()
    await asyncio.sleep(1)

    remaining = await _queue_depth(amqp_url, topology.inbound_queue)
    assert remaining == 1, "an unhandled delivery must survive on the broker"

    with registry.connect(blank_dsn) as conn:
        assert registry.list_runs(conn) == []


# --- egress -------------------------------------------------------------------


async def test_a_confirmed_routable_publish_marks_the_row_delivered(
    amqp_url: str, blank_dsn: str, ns: str
) -> None:
    _migrated(blank_dsn)
    topology = BrokerTopology(ns)
    egress = RabbitMQEgress(amqp_url, topology)
    await egress.connect()
    await egress.declare_destination("whatsapp.reply")

    with registry.connect(blank_dsn) as conn:
        outbox.enqueue(
            conn,
            OutboxMessage(
                service_namespace=ns,
                message_id="m-1",
                destination="whatsapp.reply",
                payload={"text": "on it"},
            ),
        )

    with registry.connect(blank_dsn) as conn:
        result = await drain_once(
            conn, egress.publish, service_namespace=ns, owner="egress-a"
        )
        assert result.delivered == 1
        assert result.failed == 0
        assert outbox.pending_depth(conn, service_namespace=ns) == 0

    await egress.close()

    published = await _drain_queue(amqp_url, topology.destination_queue("whatsapp.reply"))
    assert len(published) == 1
    # The stable id survives the wire — it is what consumers deduplicate on.
    assert published[0]["message_id"] == "m-1"


async def test_an_unroutable_publish_raises_and_stays_retryable(
    amqp_url: str, blank_dsn: str, ns: str
) -> None:
    """V6. No queue is bound for this destination. The broker confirms it took
    the message and then RETURNS it — which is a failure, not a success with a
    warning. Marking it delivered would silently drop a customer reply."""
    _migrated(blank_dsn)
    topology = BrokerTopology(ns)
    egress = RabbitMQEgress(amqp_url, topology)
    await egress.connect()
    # Deliberately do NOT declare the destination.

    with registry.connect(blank_dsn) as conn:
        outbox.enqueue(
            conn,
            OutboxMessage(
                service_namespace=ns, message_id="m-lost", destination="typo.queue"
            ),
        )

    with registry.connect(blank_dsn) as conn:
        result = await drain_once(
            conn, egress.publish, service_namespace=ns, owner="egress-a"
        )
        assert result.delivered == 0
        assert result.failed == 1
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, delivered_at, last_error FROM event_outbox "
                "WHERE message_id = %s",
                ("m-lost",),
            )
            row = cur.fetchone()

    await egress.close()
    assert row is not None
    assert row["status"] == "failed"
    assert row["delivered_at"] is None
    assert "unroutable" in row["last_error"]


async def test_publish_raises_publish_error_directly_for_an_unbound_destination(
    amqp_url: str, ns: str
) -> None:
    """The same property at the transport seam, so a consumer that drives
    ``publish`` itself gets the typed failure rather than a broker exception."""
    topology = BrokerTopology(ns)
    egress = RabbitMQEgress(amqp_url, topology)
    await egress.connect()
    record = outbox.OutboxRecord(
        outbox_id=str(uuid.uuid4()),
        service_namespace=ns,
        message_id="m-x",
        destination="nowhere",
        payload={},
        run_id=None,
        status="publishing",
        attempts=1,
        next_attempt_at=None,  # pyright: ignore[reportArgumentType]
        lease_owner="egress-a",
        lease_expires_at=None,
        last_error=None,
        delivered_at=None,
    )
    with pytest.raises(PublishError, match="unroutable"):
        await egress.publish(record)
    await egress.close()


async def test_one_bad_destination_does_not_strand_the_rest_of_the_batch(
    amqp_url: str, blank_dsn: str, ns: str
) -> None:
    """A single misconfigured destination must not become a total outage of the
    lane — every other reply in the batch still goes out."""
    _migrated(blank_dsn)
    topology = BrokerTopology(ns)
    egress = RabbitMQEgress(amqp_url, topology)
    await egress.connect()
    await egress.declare_destination("good")

    with registry.connect(blank_dsn) as conn:
        for i, dest in enumerate(["good", "bad", "good"]):
            outbox.enqueue(
                conn,
                OutboxMessage(
                    service_namespace=ns, message_id=f"m-{i}", destination=dest
                ),
            )

    with registry.connect(blank_dsn) as conn:
        result = await drain_once(
            conn, egress.publish, service_namespace=ns, owner="egress-a"
        )

    await egress.close()
    assert result.delivered == 2
    assert result.failed == 1


# --- broker helpers -----------------------------------------------------------


async def _publish_inbound(url: str, topology: BrokerTopology, envelope: Envelope):
    await _publish_raw(url, topology, json.dumps(envelope.as_dict()).encode())


async def _publish_raw(url: str, topology: BrokerTopology, body: bytes):
    import aio_pika

    connection = await aio_pika.connect_robust(url)
    async with connection:
        channel = await connection.channel(publisher_confirms=True)
        exchange = await channel.declare_exchange(
            topology.inbound_exchange, aio_pika.ExchangeType.TOPIC, durable=True
        )
        await exchange.publish(
            aio_pika.Message(
                body=body, delivery_mode=aio_pika.DeliveryMode.PERSISTENT
            ),
            routing_key="inbound",
        )


async def _drain_queue(url: str, queue_name: str) -> list[dict]:
    import aio_pika

    connection = await aio_pika.connect_robust(url)
    out: list[dict] = []
    async with connection:
        channel = await connection.channel()
        queue = await channel.get_queue(queue_name, ensure=False)
        while True:
            message = await queue.get(fail=False)
            if message is None:
                break
            out.append(
                {
                    "body": message.body,
                    "message_id": message.message_id,
                    "headers": dict(message.headers or {}),
                }
            )
            await message.ack()
    return out


async def _queue_depth(url: str, queue_name: str) -> int:
    import aio_pika

    connection = await aio_pika.connect_robust(url)
    async with connection:
        channel = await connection.channel()
        # A passive declare is the only call that returns a message count;
        # `get_queue` hands back a handle with no declaration result attached.
        queue = await channel.declare_queue(queue_name, passive=True)
        return queue.declaration_result.message_count or 0
