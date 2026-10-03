"""Real PostgreSQL crash/fencing/atomicity tests for the effect boundary."""

from __future__ import annotations

from typing import Any

import psycopg
import pytest

from workflow_runtime_core import registry
from workflow_runtime_core.messaging import receipts as r
from workflow_runtime_core.migrations import apply_migrations

pytestmark = pytest.mark.integration
IDENTITY = {"service_namespace": "a", "destination": "draft", "message_id": "m1"}
ACTOR = r.ReceiptActor("service", "connector-a")


def _setup(conn: registry.DbConnection) -> r.DeliveryReceipt:
    apply_migrations(conn)
    r.open_receipt(conn, **IDENTITY)
    row = r.claim(conn, **IDENTITY, owner=ACTOR.subject, lease_seconds=60)
    assert row is not None
    return row


def _row(conn: registry.DbConnection) -> r.DeliveryReceipt:
    row = r.get_receipt(conn, **IDENTITY)
    assert row is not None
    return row


def _worker(epoch: int = 1) -> dict[str, Any]:
    return {**IDENTITY, "owner": ACTOR.subject, "claim_epoch": epoch}


def test_ref_is_committed_before_settlement_and_never_replaced(blank_dsn: str) -> None:
    with registry.connect(blank_dsn) as conn:
        _setup(conn)
        assert r.start_effect(conn, **_worker())
    with registry.connect(blank_dsn) as conn:
        assert not r.mark_delivered(conn, **_worker(), provider_ref="p1")
        assert r.record_provider_ref(conn, **_worker(), provider_ref="p1")
    with registry.connect(blank_dsn) as conn:
        recorded_at = _row(conn).provider_ref_recorded_at
        assert recorded_at is not None
        assert r.record_provider_ref(conn, **_worker(), provider_ref="p1")
        assert _row(conn).provider_ref_recorded_at == recorded_at
        assert not r.record_provider_ref(conn, **_worker(), provider_ref="p2")
        assert not r.mark_delivered(conn, **_worker(), provider_ref="p2")
        assert r.mark_delivered(conn, **_worker(), provider_ref="p1")


@pytest.mark.parametrize(
    "mutation", ["start", "ref", "renew", "delivered", "failed", "uncertain", "review", "defer", "block"]
)
def test_every_worker_write_is_fenced_by_live_lease_and_epoch(blank_dsn: str, mutation: str) -> None:
    with registry.connect(blank_dsn) as conn:
        _setup(conn)
        operations = {
            "start": lambda args: r.start_effect(conn, **args),
            "ref": lambda args: r.record_provider_ref(conn, **args, provider_ref="p1"),
            "renew": lambda args: r.renew_lease(conn, **args, lease_seconds=60),
            "delivered": lambda args: r.mark_delivered(conn, **args),
            "failed": lambda args: r.mark_failed(
                conn,
                **args,
                error="rejected",
                attempts=1,
                evidence={"outcome": "definitive_no_effect", "adapter_code": "422"},
            ),
            "uncertain": lambda args: r.mark_uncertain(conn, **args, reason="unknown"),
            "review": lambda args: r.mark_needs_review(conn, **args, reason="review", evidence={}),
            "defer": lambda args: r.defer_unstarted(conn, **args, reason="later", delay_seconds=10),
            "block": lambda args: r.block_unstarted(conn, **args, reason="revoked"),
        }
        assert not operations[mutation](_worker(0))
        conn.execute("UPDATE delivery_receipts SET lease_expires_at=now()-interval '1 second'")
        assert not operations[mutation](_worker())
        assert _row(conn).event_version == 2


def test_unstarted_defer_increments_epoch_on_reclaim_with_same_owner(blank_dsn: str) -> None:
    with registry.connect(blank_dsn) as conn:
        _setup(conn)
        assert r.defer_unstarted(conn, **_worker(), reason="temporary prerequisite", delay_seconds=30)
        assert (
            r.claim_due(conn, service_namespace="a", destination="draft", owner=ACTOR.subject, lease_seconds=60) == []
        )
        newer = r.claim(conn, **IDENTITY, owner=ACTOR.subject, lease_seconds=60)
        assert newer is not None and newer.claim_epoch == 2
        assert not r.start_effect(conn, **_worker(1))
        assert r.start_effect(conn, **_worker(2))
        assert not r.defer_unstarted(conn, **_worker(2), reason="too late", delay_seconds=30)
        assert not r.block_unstarted(conn, **_worker(2), reason="too late")


def test_post_ref_failures_cannot_be_retryable(blank_dsn: str) -> None:
    with registry.connect(blank_dsn) as conn:
        _setup(conn)
        assert r.start_effect(conn, **_worker())
        assert r.record_provider_ref(conn, **_worker(), provider_ref="p1")
        assert not r.mark_failed(
            conn,
            **_worker(),
            error="PATCH failed",
            attempts=1,
            evidence={"outcome": "definitive_no_effect", "adapter_code": "422"},
        )
        with pytest.raises(ValueError, match="adapter evidence"):
            r.mark_failed(conn, **_worker(), error="timeout", attempts=1, evidence={})
        assert r.mark_needs_review(conn, **_worker(), reason="PATCH outcome unknown", evidence={})
        assert r.claim(conn, **IDENTITY, owner="replacement", lease_seconds=60) is None


@pytest.mark.parametrize("matches", [True, False])
def test_expired_known_ref_recovery_is_read_only_and_versioned(blank_dsn: str, matches: bool) -> None:
    with registry.connect(blank_dsn) as conn:
        _setup(conn)
        assert r.start_effect(conn, **_worker())
        assert r.record_provider_ref(conn, **_worker(), provider_ref="p1")
        conn.execute("UPDATE delivery_receipts SET lease_expires_at=now()-interval '1 second'")
        assert r.claim(conn, **IDENTITY, owner="replacement", lease_seconds=60) is None
        before = _row(conn)
        assert before.status == "uncertain" and before.provider_ref == "p1"
        assert not r.mark_delivered(conn, **_worker(), provider_ref="p1")
        recovery = {
            **IDENTITY,
            "provider_ref": "p1",
            "actor": ACTOR,
            "expected_digest": "digest",
            "observed_digest": "digest" if matches else "changed",
            "evidence_ref": "proof:1",
        }
        assert not r.reconcile_known_ref(conn, **recovery, expected_version=before.event_version - 1)
        assert r.reconcile_known_ref(conn, **recovery, expected_version=before.event_version)
        assert _row(conn).status == ("delivered" if matches else "needs_review")


def test_resolution_is_audited_and_never_reuses_the_old_message(blank_dsn: str) -> None:
    with registry.connect(blank_dsn) as conn:
        _setup(conn)
        assert r.mark_needs_review(conn, **_worker(), reason="inspect", evidence={})
        version = _row(conn).event_version
        args = {
            **IDENTITY,
            "expected_status": "needs_review",
            "expected_version": version,
            "actor": r.ReceiptActor("human", "reviewer-oid"),
            "note": "Confirmed duplicate risk",
        }
        with pytest.raises(ValueError, match="same message"):
            r.resolve(conn, **args, disposition="retry")
        with pytest.raises(ValueError, match="actor"):
            r.resolve(conn, **{**args, "actor": None}, disposition="blocked")
        assert r.resolve(conn, **args, disposition="retry_new_generation")
        assert not r.resolve(conn, **args, disposition="delivered")
        assert _row(conn).status == "blocked"
        assert r.claim(conn, **IDENTITY, owner="retry", lease_seconds=60) is None
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM delivery_receipt_events ORDER BY event_id DESC LIMIT 1")
            event = cur.fetchone()
        assert event is not None and event["transition"] == "resolve:retry_new_generation"
        assert event["actor_kind"] == "human" and event["actor"] == "reviewer-oid"
        assert event["reason"] == args["note"]


@pytest.mark.parametrize(
    "command",
    [
        "UPDATE delivery_receipt_events SET reason='forged'",
        "DELETE FROM delivery_receipt_events",
        "TRUNCATE delivery_receipt_events",
    ],
)
def test_events_cannot_be_mutated_even_with_legacy_dml_grants(blank_dsn: str, command: str) -> None:
    with registry.connect(blank_dsn) as conn:
        _setup(conn)
        with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
            conn.execute(command)
        assert _row(conn).event_version == 2


def test_audit_insert_failure_rolls_back_the_receipt_transition(blank_dsn: str) -> None:
    with registry.connect(blank_dsn) as conn:
        _setup(conn)
        conn.execute("""CREATE FUNCTION reject_audit() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'audit unavailable'; END; $$""")
        conn.execute(
            "CREATE TRIGGER reject_audit BEFORE INSERT ON delivery_receipt_events "
            "FOR EACH ROW EXECUTE FUNCTION reject_audit()"
        )
        with pytest.raises(psycopg.errors.RaiseException, match="audit unavailable"), conn.transaction():
            r.start_effect(conn, **_worker())
        assert _row(conn).effect_started_at is None
        assert _row(conn).event_version == 2


def test_reconciliation_listing_and_resolution_are_namespace_scoped(blank_dsn: str) -> None:
    with registry.connect(blank_dsn) as conn:
        _setup(conn)
        r.open_receipt(conn, **{**IDENTITY, "service_namespace": "b"})
        r.claim(conn, **{**IDENTITY, "service_namespace": "b"}, owner="b", lease_seconds=0)
        assert r.reconcile_stale(conn, service_namespace="a") == []
        assert r.reconcile_stale(conn, service_namespace="b") == ["m1"]
        assert [row.status for row in r.list_deliveries(conn, service_namespace="a")] == ["sending"]
        assert not r.resolve(
            conn,
            **IDENTITY,
            disposition="blocked",
            actor=ACTOR,
            note="wrong tenant",
            expected_status="uncertain",
            expected_version=3,
        )


def test_blocking_and_explicit_definitive_rejection_are_safe_terminal_and_retry_paths(blank_dsn: str) -> None:
    with registry.connect(blank_dsn) as conn:
        _setup(conn)
        assert r.block_unstarted(conn, **_worker(), reason="connection revoked")
        assert r.claim(conn, **IDENTITY, owner="retry", lease_seconds=60) is None
        second = {**IDENTITY, "message_id": "m2"}
        r.open_receipt(conn, **second)
        assert r.block(conn, **second, actor=ACTOR, reason="campaign stopped")
        third = {**IDENTITY, "message_id": "m3"}
        r.open_receipt(conn, **third)
        assert r.claim(conn, **third, owner=ACTOR.subject, lease_seconds=60) is not None
        assert r.start_effect(conn, **{**third, "owner": ACTOR.subject, "claim_epoch": 1})
        assert r.mark_failed(
            conn,
            **third,
            owner=ACTOR.subject,
            claim_epoch=1,
            error="Rejected without create",
            attempts=1,
            evidence={"outcome": "definitive_no_effect", "adapter_code": "422"},
        )
        assert r.claim(conn, **third, owner=ACTOR.subject, lease_seconds=60) is not None
        assert r.start_effect(conn, **{**third, "owner": ACTOR.subject, "claim_epoch": 2})


def test_evidence_cannot_contain_payloads_and_pagination_is_bounded(blank_dsn: str) -> None:
    with registry.connect(blank_dsn) as conn:
        _setup(conn)
        with pytest.raises(ValueError, match="redacted"):
            r.mark_needs_review(conn, **_worker(), reason="inspect", evidence={"mailbox_body": "private"})
        with pytest.raises(ValueError, match="limit"):
            r.list_deliveries(conn, service_namespace="a", limit=1001)
        assert r.list_deliveries(conn, service_namespace="a", after=("draft", "m1")) == []


def test_v4_migration_backfills_unknown_history_without_losing_provider_reference(blank_dsn: str) -> None:
    with registry.connect(blank_dsn) as conn:
        apply_migrations(conn, target=4)
        conn.execute("""INSERT INTO delivery_receipts(service_namespace,destination,message_id,status,provider_ref)
            VALUES ('a','draft','m1','uncertain','p1')""")
        apply_migrations(conn)
        row = _row(conn)
        assert row.provider_ref == "p1" and row.provider_ref_recorded_at is not None
        assert row.effect_started_at is not None and row.claim_epoch == row.event_version == 0
        event = conn.execute("SELECT transition,actor_kind FROM delivery_receipt_events").fetchone()
        assert event == {"transition": "migration_backfill", "actor_kind": "service"}


def test_expired_push_negative_control_detects_the_restored_unsafe_predicate(
    blank_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def assert_no_resend(conn: registry.DbConnection) -> None:
        assert r.claim(conn, **IDENTITY, owner="replacement", lease_seconds=60) is None

    with registry.connect(blank_dsn) as conn:
        _setup(conn)
        conn.execute("UPDATE delivery_receipts SET lease_expires_at=now()-interval '1 second'")
        assert_no_resend(conn)
        assert _row(conn).status == "uncertain"
        # Restore the exact pre-C2 expired-sending retry condition in the real
        # mutation seam. The same regression assertion must fail under it.
        conn.execute("UPDATE delivery_receipts SET status='sending',lease_expires_at=now()-interval '1 second'")
        change = r._change

        def unsafe_change(conn: registry.DbConnection, **kwargs: Any) -> r.DeliveryReceipt | None:
            if kwargs["transition"] == "claim":
                kwargs["predicate"] = (
                    "p.status IN ('pending','failed') OR (p.status='sending' AND p.lease_expires_at<now())"
                )
            return change(conn, **kwargs)

        monkeypatch.setattr(r, "_expire", lambda *args, **kwargs: False)
        monkeypatch.setattr(r, "_change", unsafe_change)
        with pytest.raises(AssertionError):
            assert_no_resend(conn)
