"""Actual PostgreSQL restricted roles, atomic audit and fixed operation bounds."""

from __future__ import annotations

import uuid

import psycopg
import pytest
from psycopg import sql
from psycopg.types.json import Jsonb

from workflow_runtime_core import registry
from workflow_runtime_core.exceptions import MigrationRoleRequired
from workflow_runtime_core.messaging import receipts
from workflow_runtime_core.messaging._receipt_capability_ddl import render
from workflow_runtime_core.messaging.receipt_capabilities import (
    CAPABILITY_NAMES,
    CAPABILITY_NAMESPACE,
    ReceiptCapabilityConnection,
    function_name,
    migrations,
)
from workflow_runtime_core.migrations import apply_app_migrations, apply_migrations

pytestmark = pytest.mark.integration
KEY = {"service_namespace": "test", "destination": "mailbox", "message_id": "message"}
ACTOR = receipts.ReceiptActor("service", "worker")


@pytest.fixture
def restricted(blank_dsn):
    role = "receipt_" + uuid.uuid4().hex
    with registry.connect(blank_dsn) as conn:
        apply_migrations(conn)
        apply_app_migrations(conn, CAPABILITY_NAMESPACE, migrations())
        conn.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(role)))
        conn.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sql.Identifier(role)))
        conn.execute(
            sql.SQL(
                "GRANT SELECT ON delivery_receipts,delivery_receipt_events,schema_meta,schema_migrations TO {}"
            ).format(sql.Identifier(role))
        )
        for operation in CAPABILITY_NAMES:
            conn.execute(
                sql.SQL("GRANT EXECUTE ON FUNCTION public.{}(jsonb) TO {}").format(
                    sql.Identifier(function_name(operation)), sql.Identifier(role)
                )
            )
    try:
        with registry.connect(blank_dsn) as conn:
            conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role)))
            conn.commit()
            yield conn, ReceiptCapabilityConnection(conn), role
    finally:
        with registry.connect(blank_dsn) as conn:
            conn.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
            conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


def claimed(native, cap, *, start=False, reference=False):
    receipts.open_receipt(cap, **KEY)
    native.commit()
    rows = receipts.claim_due(
        cap, service_namespace="test", destination="mailbox", owner="worker", lease_seconds=60, message_ids=("message",)
    )
    assert len(rows) == 1
    epoch = rows[0].claim_epoch
    assert receipts.renew_lease(cap, **KEY, owner="worker", claim_epoch=epoch, lease_seconds=60)
    if start:
        assert receipts.start_effect(cap, **KEY, owner="worker", claim_epoch=epoch)
    if reference:
        assert receipts.record_provider_ref(cap, **KEY, owner="worker", claim_epoch=epoch, provider_ref="draft-ref")
    native.commit()
    return epoch


def test_frozen_migration_matches_single_query_owner():
    assert render() == migrations()[0].sql


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO delivery_receipts(service_namespace,destination,message_id) VALUES ('test','mailbox','forged')",
        "UPDATE delivery_receipts SET status='delivered'",
        "DELETE FROM delivery_receipts",
        "TRUNCATE delivery_receipts",
        "INSERT INTO delivery_receipt_events(service_namespace) VALUES ('test')",
        "UPDATE delivery_receipt_events SET actor='forged'",
        "DELETE FROM delivery_receipt_events",
        "TRUNCATE delivery_receipt_events",
        "CREATE TABLE public.forbidden (id int)",
    ],
)
def test_direct_mutations_denied(restricted, statement):
    native, cap, _ = restricted
    receipts.open_receipt(cap, **KEY)
    native.commit()
    with pytest.raises(psycopg.errors.InsufficientPrivilege), native.transaction():
        native.execute(statement)
    assert receipts.get_receipt(cap, **KEY).status == "pending"


def test_atomic_idempotent_open(restricted):
    native, cap, _ = restricted
    with pytest.raises(RuntimeError), native.transaction():
        receipts.open_receipt(cap, **KEY)
        raise RuntimeError("rollback proposal transaction")
    assert receipts.get_receipt(cap, **KEY) is None
    receipts.open_receipt(cap, **KEY)
    receipts.open_receipt(cap, **KEY)
    assert native.execute("SELECT count(*) AS n FROM delivery_receipt_events").fetchone()["n"] == 1


@pytest.mark.parametrize("operation", ["delivered", "failed", "uncertain", "needs_review", "blocked", "pending"])
def test_worker_settlements_and_fences(restricted, operation):
    native, cap, _ = restricted
    epoch = claimed(native, cap, start=operation not in {"blocked", "pending"}, reference=operation == "delivered")
    assert not receipts.start_effect(cap, **KEY, owner="other", claim_epoch=epoch)
    assert not receipts.start_effect(cap, **KEY, owner="worker", claim_epoch=epoch + 1)
    common = {**KEY, "owner": "worker", "claim_epoch": epoch}
    if operation == "delivered":
        assert receipts.mark_delivered(cap, **common, provider_ref="draft-ref")
    elif operation == "failed":
        assert receipts.mark_failed(
            cap,
            **common,
            error="rejected",
            attempts=1,
            evidence={"outcome": "definitive_no_effect", "adapter_code": "invalid"},
        )
    elif operation == "uncertain":
        assert receipts.mark_uncertain(cap, **common, reason="response lost")
    elif operation == "needs_review":
        assert receipts.mark_needs_review(cap, **common, reason="edited", evidence={"evidence_ref": "inspection"})
    elif operation == "blocked":
        assert receipts.block_unstarted(cap, **common, reason="suppressed")
    else:
        assert receipts.defer_unstarted(cap, **common, reason="partial coverage", delay_seconds=60)
    assert receipts.get_receipt(cap, **KEY).status == operation
    assert not receipts.start_effect(cap, **common)


def test_expiry_never_reclaims_unknown_effect(restricted):
    native, cap, _ = restricted
    receipts.open_receipt(cap, **KEY)
    assert receipts.claim(cap, **KEY, owner="worker", lease_seconds=0)
    assert receipts.reconcile_stale(cap, service_namespace="test") == ["message"]
    assert receipts.get_receipt(cap, **KEY).status == "uncertain"
    assert receipts.claim(cap, **KEY, owner="worker", lease_seconds=60) is None


@pytest.mark.parametrize("matches", [True, False])
def test_exact_reference_recovery(restricted, matches):
    native, cap, _ = restricted
    epoch = claimed(native, cap, start=True, reference=True)
    assert receipts.mark_uncertain(cap, **KEY, owner="worker", claim_epoch=epoch, reason="verify lost")
    row = receipts.get_receipt(cap, **KEY)
    assert receipts.reconcile_known_ref(
        cap,
        **KEY,
        provider_ref="draft-ref",
        expected_version=row.event_version,
        actor=ACTOR,
        expected_digest="digest",
        observed_digest="digest" if matches else "edited",
        evidence_ref="inspect",
    )
    assert receipts.get_receipt(cap, **KEY).status == ("delivered" if matches else "needs_review")


@pytest.mark.parametrize("disposition", ["delivered", "blocked", "retry_new_generation"])
def test_review_then_versioned_resolution(restricted, disposition):
    native, cap, _ = restricted
    epoch = claimed(native, cap, start=True)
    assert receipts.mark_uncertain(cap, **KEY, owner="worker", claim_epoch=epoch, reason="create lost")
    row = receipts.get_receipt(cap, **KEY)
    assert receipts.review_uncertain(
        cap,
        **KEY,
        expected_version=row.event_version,
        actor=ACTOR,
        reason="bounded scan inconclusive",
        evidence={"evidence_ref": "scan"},
    )
    row = receipts.get_receipt(cap, **KEY)
    common = {
        **KEY,
        "disposition": disposition,
        "actor": receipts.ReceiptActor("human", "reviewer"),
        "note": "verified",
        "expected_status": "needs_review",
        "expected_version": row.event_version,
    }
    assert receipts.resolve(cap, **common)
    assert not receipts.resolve(cap, **common)
    assert receipts.get_receipt(cap, **KEY).status == ("delivered" if disposition == "delivered" else "blocked")


def test_pending_block_and_empty_selection(restricted):
    native, cap, _ = restricted
    receipts.open_receipt(cap, **KEY)
    assert (
        receipts.claim_due(
            cap, service_namespace="test", destination="mailbox", owner="worker", lease_seconds=60, message_ids=()
        )
        == []
    )
    assert receipts.block(cap, **KEY, reason="suppressed", actor=ACTOR)
    assert receipts.list_deliveries(cap, service_namespace="test")[0].status == "blocked"


@pytest.mark.parametrize(
    "tamper", ["status", "transition", "actor_kind", "fence_actor", "evidence", "length", "reference"]
)
def test_raw_function_cannot_change_semantics(restricted, tamper):
    native, cap, _ = restricted
    epoch = claimed(native, cap, start=True)
    args = [
        "test",
        "mailbox",
        "message",
        "uncertain",
        None,
        None,
        "lost",
        0,
        "uncertain",
        "worker",
        epoch,
        None,
        None,
        "uncertain",
        "service",
        "worker",
        "lost",
        {},
    ]
    if tamper == "status":
        args[3] = args[8] = "delivered"
    elif tamper == "transition":
        args[13] = "delivered"
    elif tamper == "actor_kind":
        args[14] = "human"
    elif tamper == "fence_actor":
        args[15] = "other"
    elif tamper == "evidence":
        args[-1] = {"body": "forbidden payload"}
    elif tamper == "length":
        args.append("extra")
    else:
        args[4] = "ref"
    with pytest.raises(psycopg.errors.InvalidParameterValue), native.transaction():
        native.execute(
            sql.SQL("SELECT * FROM public.{}(%s::jsonb)").format(sql.Identifier(function_name("uncertain"))),
            (Jsonb(args),),
        )
    assert receipts.get_receipt(cap, **KEY).status == "sending"


def test_public_execution_and_application_migration_denied(restricted):
    native, cap, role = restricted
    native.execute("RESET ROLE")
    native.execute(
        sql.SQL("REVOKE EXECUTE ON FUNCTION public.{}(jsonb) FROM {}").format(
            sql.Identifier(function_name("open")), sql.Identifier(role)
        )
    )
    native.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role)))
    native.commit()
    with pytest.raises(psycopg.errors.InsufficientPrivilege), native.transaction():
        receipts.open_receipt(cap, **KEY)
    with pytest.raises(MigrationRoleRequired), native.transaction():
        apply_app_migrations(native, CAPABILITY_NAMESPACE, migrations())


def test_connector_grant_does_not_include_review_resolution(restricted):
    native, cap, role = restricted
    epoch = claimed(native, cap, start=True)
    assert receipts.mark_uncertain(cap, **KEY, owner="worker", claim_epoch=epoch, reason="unknown")
    row = receipts.get_receipt(cap, **KEY)
    native.execute("RESET ROLE")
    for disposition in ("delivered", "blocked", "retry_new_generation"):
        native.execute(
            sql.SQL("REVOKE EXECUTE ON FUNCTION public.{}(jsonb) FROM {}").format(
                sql.Identifier(function_name("resolve:" + disposition)), sql.Identifier(role)
            )
        )
    native.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role)))
    native.commit()
    with pytest.raises(psycopg.errors.InsufficientPrivilege), native.transaction():
        receipts.resolve(
            cap,
            **KEY,
            disposition="delivered",
            actor=ACTOR,
            note="forged decision",
            expected_status="uncertain",
            expected_version=row.event_version,
        )
    assert receipts.get_receipt(cap, **KEY).status == "uncertain"


@pytest.mark.parametrize(
    "args",
    [
        ["test", "mailbox", ["message", "message"], ["message", "message"], 1],
        ["test", "mailbox", ["message"], ["other"], 1],
        ["test", "mailbox", [""], [""], 1],
        ["test", "mailbox", [1], [1], 1],
        ["test", "mailbox", None, None, 1001],
    ],
)
def test_raw_selection_bounds(restricted, args):
    native, _, _ = restricted
    with pytest.raises(psycopg.errors.InvalidParameterValue), native.transaction():
        native.execute(
            sql.SQL("SELECT * FROM public.{}(%s::jsonb)").format(sql.Identifier(function_name("select_due"))),
            (Jsonb(args),),
        )


def test_setup_cli_proves_owner_before_noop_ledger(restricted, blank_dsn):
    from click.testing import CliRunner

    from workflow_runtime_core.cli import main

    _, _, role = restricted
    result = CliRunner().invoke(main, ["receipt-capabilities-setup", "--dsn", blank_dsn, "--owner-role", role])
    assert result.exit_code != 0
    assert isinstance(result.exception, MigrationRoleRequired)
