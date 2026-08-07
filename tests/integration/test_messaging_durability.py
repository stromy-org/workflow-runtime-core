"""Durability of the v3 messaging boundary, against a real PostgreSQL.

These are the Gate B probes. Each one names the crash it is standing in for,
because "does the happy path work" is not the question this layer exists to
answer — the question is what the database looks like after a process dies at
the worst possible instant.
"""

from __future__ import annotations

import pytest

from workflow_runtime_core import registry
from workflow_runtime_core.messaging import (
    AttachmentRef,
    Envelope,
    EnvelopeTooLarge,
    OutboxMessage,
    inbox,
    launches,
    outbox,
    receipts,
)
from workflow_runtime_core.migrations import apply_migrations

NS = "gmf-pilot"


def _migrated(dsn: str) -> None:
    with registry.connect(dsn) as conn:
        apply_migrations(conn)


def _envelope(message_id: str = "wamid.1", **overrides: object) -> Envelope:
    base: dict[str, object] = {
        "service_namespace": NS,
        "source": "whatsapp",
        "source_message_id": message_id,
        "workflow": "triage",
        "payload": {"text": "my order never arrived"},
    }
    base.update(overrides)
    return Envelope(**base)  # pyright: ignore[reportArgumentType]


def _submit(dsn: str, envelope: Envelope) -> inbox.SubmitResult:
    with registry.connect(dsn) as conn:
        return inbox.submit_event(
            conn, envelope, launcher="subprocess", params_hash="h0"
        )


# --- 1. ingress atomicity + de-duplication ------------------------------------


@pytest.mark.integration
def test_submit_creates_inbox_run_and_launch_together(blank_dsn: str) -> None:
    _migrated(blank_dsn)
    result = _submit(blank_dsn, _envelope())

    assert result.duplicate is False
    with registry.connect(blank_dsn) as conn:
        run = registry.get_run(conn, result.run_id)
        launch = launches.get_launch(conn, result.run_id)
        envelope = inbox.get_envelope(conn, result.run_id)

    assert run is not None and run.workflow == "triage"
    assert launch is not None and launch.state == "pending"
    # The envelope is re-readable from the database alone: recovery has no
    # in-memory copy, so a run that cannot be reconstructed here is unrunnable.
    assert envelope is not None
    assert envelope.payload["text"] == "my order never arrived"


@pytest.mark.integration
def test_a_redelivered_message_resolves_to_the_same_run(blank_dsn: str) -> None:
    """The crash between commit and ack. The broker redelivers; we must not
    start a second run for one customer message."""
    _migrated(blank_dsn)
    first = _submit(blank_dsn, _envelope())
    second = _submit(blank_dsn, _envelope())

    assert second.duplicate is True
    assert second.run_id == first.run_id
    assert second.inbox_id == first.inbox_id

    with registry.connect(blank_dsn) as conn:
        assert len(registry.list_runs(conn)) == 1


@pytest.mark.integration
def test_distinct_source_ids_create_distinct_runs(blank_dsn: str) -> None:
    _migrated(blank_dsn)
    a = _submit(blank_dsn, _envelope("wamid.a"))
    b = _submit(blank_dsn, _envelope("wamid.b"))
    assert a.run_id != b.run_id


@pytest.mark.integration
def test_the_same_id_from_another_namespace_is_not_a_duplicate(blank_dsn: str) -> None:
    """Namespace is part of every uniqueness boundary, so two services sharing a
    database cannot collide on a channel id neither of them controls."""
    _migrated(blank_dsn)
    a = _submit(blank_dsn, _envelope())
    b = _submit(blank_dsn, _envelope(service_namespace="other-svc"))
    assert a.run_id != b.run_id


# --- 2. envelope bounds are refused, never truncated ---------------------------


@pytest.mark.integration
def test_an_oversized_envelope_is_refused_before_any_row_exists(blank_dsn: str) -> None:
    _migrated(blank_dsn)
    huge = _envelope(payload={"text": "x" * (256 * 1024)})

    with pytest.raises(EnvelopeTooLarge):
        _submit(blank_dsn, huge)

    with registry.connect(blank_dsn) as conn:
        assert registry.list_runs(conn) == []


@pytest.mark.integration
def test_too_many_attachment_descriptors_are_refused(blank_dsn: str) -> None:
    _migrated(blank_dsn)
    attachment = AttachmentRef(
        media_type="image/jpeg", size_bytes=10, digest="a" * 64, reference="s3://x"
    )
    with pytest.raises(EnvelopeTooLarge):
        _submit(blank_dsn, _envelope(attachments=tuple([attachment] * 21)))


# --- 3. launch leases: one claim at a time, recovered after a crash ------------


@pytest.mark.integration
def test_two_dispatchers_never_claim_the_same_launch(blank_dsn: str) -> None:
    _migrated(blank_dsn)
    _submit(blank_dsn, _envelope("m1"))
    _submit(blank_dsn, _envelope("m2"))

    # Two live connections claiming concurrently: SKIP LOCKED must partition the
    # rows, not hand both dispatchers the same one.
    with registry.connect(blank_dsn) as ca, registry.connect(blank_dsn) as cb:
        a = launches.claim_due(ca, owner="disp-a", lease_seconds=60, limit=10)
        b = launches.claim_due(cb, owner="disp-b", lease_seconds=60, limit=10)

    ids_a = {r.run_id for r in a}
    ids_b = {r.run_id for r in b}
    assert ids_a.isdisjoint(ids_b)
    assert len(ids_a | ids_b) == 2


@pytest.mark.integration
def test_a_launched_run_records_its_execution_reference(blank_dsn: str) -> None:
    _migrated(blank_dsn)
    result = _submit(blank_dsn, _envelope())

    with registry.connect(blank_dsn) as conn:
        claimed = launches.claim_due(conn, owner="disp-a", lease_seconds=60)
        assert [r.run_id for r in claimed] == [result.run_id]
        assert launches.record_launched(
            conn, result.run_id, owner="disp-a", execution_ref="pid:4242"
        )
        launch = launches.get_launch(conn, result.run_id)

    assert launch is not None
    assert launch.state == "launched"
    assert launch.execution_ref == "pid:4242"
    assert launch.lease_owner is None


@pytest.mark.integration
def test_a_dispatcher_that_lost_its_lease_cannot_overwrite_the_new_owner(
    blank_dsn: str,
) -> None:
    """The crash-and-come-back case: a dispatcher pauses long enough for its
    lease to lapse, another claims the row, then the first wakes up and tries to
    record its outcome. It must be refused."""
    _migrated(blank_dsn)
    result = _submit(blank_dsn, _envelope())

    with registry.connect(blank_dsn) as conn:
        launches.claim_due(conn, owner="disp-a", lease_seconds=0)
        reset = launches.reconcile_stale(conn)
        assert reset == [result.run_id]
        launches.claim_due(conn, owner="disp-b", lease_seconds=60)

        # disp-a is a zombie now.
        assert not launches.record_launched(
            conn, result.run_id, owner="disp-a", execution_ref="pid:stale"
        )
        assert launches.record_launched(
            conn, result.run_id, owner="disp-b", execution_ref="pid:live"
        )
        launch = launches.get_launch(conn, result.run_id)

    assert launch is not None and launch.execution_ref == "pid:live"


@pytest.mark.integration
def test_a_failed_launch_backs_off_instead_of_spinning(blank_dsn: str) -> None:
    _migrated(blank_dsn)
    result = _submit(blank_dsn, _envelope())

    with registry.connect(blank_dsn) as conn:
        launches.claim_due(conn, owner="disp-a", lease_seconds=60)
        assert launches.record_failed(
            conn, result.run_id, owner="disp-a", error="ECS throttled", attempts=6
        )
        launch = launches.get_launch(conn, result.run_id)
        # Backed off into the future, so an immediate re-poll finds nothing.
        assert launches.claim_due(conn, owner="disp-a", lease_seconds=60) == []

    assert launch is not None
    assert launch.state == "failed"
    assert launch.last_error == "ECS throttled"


@pytest.mark.integration
def test_a_live_execution_is_adopted_rather_than_relaunched(blank_dsn: str) -> None:
    """A dispatcher died but its child survived. Relaunching would start a second
    process for one run and lean entirely on the runner claim to tidy up."""
    _migrated(blank_dsn)
    result = _submit(blank_dsn, _envelope())

    with registry.connect(blank_dsn) as conn:
        launches.claim_due(conn, owner="disp-a", lease_seconds=0)
        assert launches.adopt_live_execution(
            conn, result.run_id, execution_ref="pid:survivor"
        )
        launch = launches.get_launch(conn, result.run_id)
        assert launches.reconcile_stale(conn) == []

    assert launch is not None
    assert launch.state == "launched"
    assert launch.execution_ref == "pid:survivor"


# --- 4. outbox: stable identity across re-finalization -------------------------


@pytest.mark.integration
def test_refinalizing_a_run_produces_one_message_not_two(blank_dsn: str) -> None:
    """Recovery re-projects a terminal snapshot by design. The outbox insert has
    to be idempotent or every recovered run double-replies."""
    _migrated(blank_dsn)
    result = _submit(blank_dsn, _envelope())
    message = OutboxMessage(
        service_namespace=NS,
        message_id=f"reply:{result.run_id}",
        destination="whatsapp.reply",
        payload={"text": "we are on it"},
        run_id=result.run_id,
    )

    with registry.connect(blank_dsn) as conn:
        first = outbox.enqueue(conn, message)
        second = outbox.enqueue(conn, message)

    assert first == second
    with registry.connect(blank_dsn) as conn:
        assert outbox.pending_depth(conn, service_namespace=NS) == 1


@pytest.mark.integration
def test_delivery_requires_holding_the_lease(blank_dsn: str) -> None:
    _migrated(blank_dsn)
    result = _submit(blank_dsn, _envelope())
    with registry.connect(blank_dsn) as conn:
        outbox_id = outbox.enqueue(
            conn,
            OutboxMessage(
                service_namespace=NS,
                message_id="m-1",
                destination="whatsapp.reply",
                run_id=result.run_id,
            ),
        )
        claimed = outbox.claim_due(
            conn, service_namespace=NS, owner="egress-a", lease_seconds=60
        )
        assert [r.outbox_id for r in claimed] == [outbox_id]

        # A replica that does not hold the lease cannot mark it delivered.
        assert not outbox.mark_delivered(conn, outbox_id, owner="egress-b")
        assert outbox.mark_delivered(conn, outbox_id, owner="egress-a")
        assert outbox.pending_depth(conn, service_namespace=NS) == 0


@pytest.mark.integration
def test_an_unroutable_publish_leaves_the_message_retryable(blank_dsn: str) -> None:
    """V6. The broker confirmed it accepted the message and then returned it as
    unroutable. That is a failure; marking it delivered loses the reply."""
    _migrated(blank_dsn)
    with registry.connect(blank_dsn) as conn:
        outbox_id = outbox.enqueue(
            conn,
            OutboxMessage(
                service_namespace=NS, message_id="m-x", destination="typo.queue"
            ),
        )
        claimed = outbox.claim_due(
            conn, service_namespace=NS, owner="egress-a", lease_seconds=60
        )
        assert outbox.mark_failed(
            conn,
            outbox_id,
            owner="egress-a",
            error="returned unroutable",
            attempts=claimed[0].attempts,
        )
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, delivered_at FROM event_outbox WHERE outbox_id = %s",
                (outbox_id,),
            )
            row = cur.fetchone()

    assert row is not None
    assert row["status"] == "failed"
    assert row["delivered_at"] is None


@pytest.mark.integration
def test_a_crashed_publisher_releases_its_message(blank_dsn: str) -> None:
    _migrated(blank_dsn)
    with registry.connect(blank_dsn) as conn:
        outbox_id = outbox.enqueue(
            conn,
            OutboxMessage(
                service_namespace=NS, message_id="m-c", destination="whatsapp.reply"
            ),
        )
        outbox.claim_due(
            conn, service_namespace=NS, owner="egress-dead", lease_seconds=0
        )
        assert outbox.reconcile_stale(conn) == [outbox_id]
        # Immediately re-claimable by a live replica, same message id.
        again = outbox.claim_due(
            conn, service_namespace=NS, owner="egress-live", lease_seconds=60
        )

    assert [r.outbox_id for r in again] == [outbox_id]
    assert again[0].message_id == "m-c"


# --- 5. delivery receipts: uncertain is not retried ----------------------------


def _open_and_claim(conn: registry.DbConnection, message_id: str, owner: str):
    receipts.open_receipt(
        conn, service_namespace=NS, destination="whatsapp", message_id=message_id
    )
    return receipts.claim_due(
        conn,
        service_namespace=NS,
        destination="whatsapp",
        owner=owner,
        lease_seconds=60,
    )


@pytest.mark.integration
def test_a_definitive_rejection_is_retried(blank_dsn: str) -> None:
    _migrated(blank_dsn)
    with registry.connect(blank_dsn) as conn:
        claimed = _open_and_claim(conn, "r-1", "sender-a")
        assert receipts.mark_failed(
            conn,
            service_namespace=NS,
            destination="whatsapp",
            message_id="r-1",
            owner="sender-a",
            error="422 invalid template",
            attempts=claimed[0].attempts,
        )
        assert receipts.list_uncertain(conn, service_namespace=NS) == []


@pytest.mark.integration
def test_an_ambiguous_outcome_becomes_uncertain_and_is_never_auto_retried(
    blank_dsn: str,
) -> None:
    """The reason this table exists. A timeout after the request was written
    means the provider may well have sent it — retrying double-sends to a real
    person, and assuming success drops it."""
    _migrated(blank_dsn)
    with registry.connect(blank_dsn) as conn:
        _open_and_claim(conn, "r-2", "sender-a")
        assert receipts.mark_uncertain(
            conn,
            service_namespace=NS,
            destination="whatsapp",
            message_id="r-2",
            owner="sender-a",
            reason="read timeout after request write",
        )
        # Not picked up by the ordinary loop, at any point.
        assert (
            receipts.claim_due(
                conn,
                service_namespace=NS,
                destination="whatsapp",
                owner="sender-a",
                lease_seconds=60,
            )
            == []
        )
        worklist = receipts.list_uncertain(conn, service_namespace=NS)

    assert [r.message_id for r in worklist] == ["r-2"]


@pytest.mark.integration
def test_a_crash_mid_send_becomes_uncertain_not_pending(blank_dsn: str) -> None:
    """Reconciliation must NOT return a half-sent message to the retry queue."""
    _migrated(blank_dsn)
    with registry.connect(blank_dsn) as conn:
        receipts.open_receipt(
            conn, service_namespace=NS, destination="whatsapp", message_id="r-3"
        )
        receipts.claim_due(
            conn,
            service_namespace=NS,
            destination="whatsapp",
            owner="sender-dead",
            lease_seconds=0,
        )
        assert receipts.reconcile_stale(conn) == ["r-3"]
        worklist = receipts.list_uncertain(conn, service_namespace=NS)

    assert [r.message_id for r in worklist] == ["r-3"]


@pytest.mark.integration
def test_an_uncertain_receipt_is_resolved_explicitly(blank_dsn: str) -> None:
    _migrated(blank_dsn)
    with registry.connect(blank_dsn) as conn:
        _open_and_claim(conn, "r-4", "sender-a")
        receipts.mark_uncertain(
            conn,
            service_namespace=NS,
            destination="whatsapp",
            message_id="r-4",
            owner="sender-a",
            reason="timeout",
        )
        assert receipts.resolve_uncertain(
            conn,
            service_namespace=NS,
            destination="whatsapp",
            message_id="r-4",
            delivered=True,
            provider_ref="SM123",
            note="confirmed present in provider log",
        )
        assert receipts.list_uncertain(conn, service_namespace=NS) == []


# --- 6. retention ---------------------------------------------------------------


@pytest.mark.integration
def test_purge_keeps_a_paused_runs_envelope(blank_dsn: str) -> None:
    """Age alone is not a safe predicate: a paused run waits on a human for
    longer than the retention window and still needs its envelope to resume."""
    _migrated(blank_dsn)
    result = _submit(blank_dsn, _envelope())
    with registry.connect(blank_dsn) as conn:
        registry.claim_run(conn, result.run_id)
        registry.mark_paused(conn, result.run_id, {"question": "which order?"})
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE event_inbox SET received_at = now() - interval '400 days'"
            )
        assert inbox.purge_inbox(conn, service_namespace=NS, older_than_days=30) == 0
        assert inbox.get_envelope(conn, result.run_id) is not None
