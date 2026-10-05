"""Real PostgreSQL selection and conservative unknown-effect review controls."""

from __future__ import annotations

from typing import Any

import pytest

from workflow_runtime_core import registry
from workflow_runtime_core.messaging import receipts as r
from workflow_runtime_core.migrations import apply_migrations

pytestmark = pytest.mark.integration


def test_domain_selection_never_claims_earlier_foreign_target(blank_dsn: str) -> None:
    with registry.connect(blank_dsn) as conn:
        apply_migrations(conn)
        for namespace, destination, message in (
            ("a", "draft", "early-foreign-mailbox"),
            ("a", "draft", "selected"),
            ("b", "draft", "selected"),
            ("a", "other", "selected"),
        ):
            r.open_receipt(conn, service_namespace=namespace, destination=destination, message_id=message)
        rows = r.claim_due(conn, service_namespace="a", destination="draft", owner="connector",
                           lease_seconds=60, limit=1, message_ids=("selected",))
        assert [row.message_id for row in rows] == ["selected"]
        assert rows[0].service_namespace == "a" and rows[0].destination == "draft"
        for namespace, destination, message in (
            ("a", "draft", "early-foreign-mailbox"), ("b", "draft", "selected"), ("a", "other", "selected"),
        ):
            row = r.get_receipt(conn, service_namespace=namespace, destination=destination, message_id=message)
            assert row is not None and row.status == "pending" and row.attempts == 0
        assert r.claim_due(conn, service_namespace="a", destination="draft", owner="connector",
                           lease_seconds=60, message_ids=()) == []
        assert r.claim_due(conn, service_namespace="a", destination="draft", owner="connector",
                           lease_seconds=60, message_ids=("absent",)) == []
        # Default behavior remains compatible with existing unfiltered consumers.
        assert len(r.claim_due(conn, service_namespace="a", destination="draft", owner="connector",
                               lease_seconds=60)) == 1


@pytest.mark.parametrize("ids", [("",), (" ",), ("x" * 2049,), ("same", "same"), (1,),
                                  ("m",) * 1001, ["m"]])
def test_invalid_domain_selection_is_refused_before_database_io(ids: Any) -> None:
    class NoDatabase:
        def cursor(self):
            raise AssertionError("Invalid selection touched the database")
    with pytest.raises(ValueError, match="message_ids"):
        r.claim_due(NoDatabase(), service_namespace="a", destination="draft", owner="connector",  # type: ignore[arg-type]
                    lease_seconds=60, message_ids=ids)


@pytest.mark.parametrize("has_ref", [False, True])
def test_review_without_create_response_is_audited_and_never_reclaimed(blank_dsn: str, has_ref: bool) -> None:
    key = {"service_namespace": "a", "destination": "draft", "message_id": "lost-response"}
    worker = {**key, "owner": "connector", "claim_epoch": 1}
    actor = r.ReceiptActor("service", "reconciler")
    with registry.connect(blank_dsn) as conn:
        apply_migrations(conn)
        r.open_receipt(conn, **key)
        assert r.claim(conn, **key, owner="connector", lease_seconds=60)
        assert r.start_effect(conn, **worker)
        if has_ref:
            assert r.record_provider_ref(conn, **worker, provider_ref="immutable-draft")
        assert r.mark_uncertain(conn, **worker, reason="lost_response")
        row = r.get_receipt(conn, **key)
        assert row is not None
        args = {**key, "expected_version": row.event_version, "actor": actor,
                "reason": "insufficient_evidence", "evidence": {"evidence_ref": "scan:bounded"}}
        assert not r.review_uncertain(conn, **{**args, "expected_version": row.event_version - 1})
        assert not r.review_uncertain(conn, **{**args, "service_namespace": "b"})
        assert r.review_uncertain(conn, **args)
        assert not r.review_uncertain(conn, **args)
    with registry.connect(blank_dsn) as conn:
        after = r.get_receipt(conn, **key)
        assert after is not None and after.status == "needs_review"
        assert after.provider_ref == ("immutable-draft" if has_ref else None)
        assert after.effect_started_at is not None and after.lease_owner is None
        assert r.claim_due(conn, service_namespace="a", destination="draft", owner="another",
                           lease_seconds=60, message_ids=(key["message_id"],)) == []
        event = conn.execute("SELECT * FROM delivery_receipt_events WHERE transition='review_uncertain'").fetchone()
        assert event is not None and event["actor_kind"] == "service" and event["actor"] == "reconciler"
        assert event["from_status"] == "uncertain" and event["to_status"] == "needs_review"
        assert event["evidence"] == {"evidence_ref": "scan:bounded"}


@pytest.mark.parametrize("invalid", ["human", "missing_evidence", "private_body", "version"])
def test_review_requires_service_version_and_redacted_inspection_evidence(blank_dsn: str, invalid: str) -> None:
    with registry.connect(blank_dsn) as conn:
        apply_migrations(conn)
        args = {"service_namespace": "a", "destination": "draft", "message_id": "m",
                "expected_version": 0 if invalid == "version" else 1,
                "actor": r.ReceiptActor("human" if invalid == "human" else "service", "principal"),
                "reason": "inspect", "evidence": {} if invalid == "missing_evidence" else {"evidence_ref": "scan"}}
        if invalid == "private_body":
            args["evidence"]["mailbox_body"] = "private"
        with pytest.raises(ValueError):
            r.review_uncertain(conn, **args)
