"""Fenced external effects and atomic, append-only transition evidence.

Commit ``start_effect`` before calling a provider. Commit its reference before
finalization. Every worker mutation needs the live owner and claim epoch. Lost
outcomes leave automation permanently; only read-only proof or audited resolution
can settle them. This module never creates a replacement domain message.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, LiteralString, cast

from ..registry import DbConnection
from ..schema import require_compatible_schema
from ._backoff import next_delay_seconds
from ._receipt_sql import CLAIM_DUE_QUERY, OPEN_QUERY, OPERATIONS, STALE_QUERY
from .receipt_capabilities import ReceiptCapabilityConnection, execute_capability

RECEIPT_STATUSES = ("pending", "sending", "delivered", "failed", "uncertain", "blocked", "needs_review")
_EVIDENCE_KEYS = frozenset({"evidence_ref", "expected_digest", "observed_digest", "outcome", "adapter_code"})


@dataclass(frozen=True)
class ReceiptActor:
    """Verified principal supplied by the adapter; app-only identities are services."""

    kind: Literal["service", "human"]
    subject: str

    def __post_init__(self) -> None:
        if self.kind not in {"service", "human"} or not self.subject.strip() or len(self.subject) > 256:
            raise ValueError("actor requires a service/human kind and a bounded nonempty subject")


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
    claim_epoch: int
    event_version: int
    effect_started_at: datetime | None
    provider_ref_recorded_at: datetime | None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> DeliveryReceipt:
        return cls(**{name: row[name] for name in cls.__dataclass_fields__})


_OPEN_ACTOR = ReceiptActor("service", "receipt-opener")


def _audit(actor: object, reason: str, evidence: dict[str, str] | None) -> str:
    if not isinstance(actor, ReceiptActor):
        raise ValueError("a verified actor is required")
    if not reason.strip() or len(reason) > 2000:
        raise ValueError("reason must be nonempty and at most 2000 characters")
    evidence = {} if evidence is None else evidence
    if set(evidence) - _EVIDENCE_KEYS or any(not isinstance(cast("object", v), str) for v in evidence.values()):
        raise ValueError("evidence accepts only redacted references, digests and adapter outcome codes")
    encoded = json.dumps(evidence, ensure_ascii=True)
    if len(encoded.encode()) > 4000:
        raise ValueError("evidence exceeds 4000 bytes")
    return encoded


def _change(
    conn: DbConnection | ReceiptCapabilityConnection,
    *,
    service_namespace: str,
    destination: str,
    message_id: str,
    values: tuple[Any, ...],
    fences: tuple[Any, ...],
    transition: str,
    actor: ReceiptActor,
    reason: str,
    evidence: dict[str, str] | None = None,
) -> DeliveryReceipt | None:
    """Internal SQL fragments are fixed literals; all adapter values are parameters."""
    encoded = _audit(actor, reason, evidence)
    require_compatible_schema(conn.native if isinstance(conn, ReceiptCapabilityConnection) else conn, minimum=5)
    with conn.cursor() as cur:
        parameters = (
            service_namespace,
            destination,
            message_id,
            *values,
            *fences,
            transition,
            actor.kind,
            actor.subject,
            reason,
            encoded,
        )
        if isinstance(conn, ReceiptCapabilityConnection):
            execute_capability(cur, transition, parameters)
        else:
            cur.execute(cast(LiteralString, OPERATIONS[transition].query()), parameters)
        row = cur.fetchone()
    return None if row is None else DeliveryReceipt.from_row(row)


def open_receipt(
    conn: DbConnection | ReceiptCapabilityConnection,
    *,
    service_namespace: str,
    destination: str,
    message_id: str,
    actor: ReceiptActor = _OPEN_ACTOR,
) -> None:
    encoded = _audit(actor, "Receipt opened", None)
    require_compatible_schema(conn.native if isinstance(conn, ReceiptCapabilityConnection) else conn, minimum=5)
    with conn.cursor() as cur:
        parameters = (service_namespace, destination, message_id, actor.kind, actor.subject, encoded)
        if isinstance(conn, ReceiptCapabilityConnection):
            execute_capability(cur, "open", parameters)
        else:
            cur.execute(OPEN_QUERY, parameters)


def get_receipt(
    conn: DbConnection | ReceiptCapabilityConnection,
    *,
    service_namespace: str,
    destination: str,
    message_id: str,
) -> DeliveryReceipt | None:
    require_compatible_schema(conn.native if isinstance(conn, ReceiptCapabilityConnection) else conn, minimum=5)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM delivery_receipts WHERE service_namespace=%s AND destination=%s AND message_id=%s",
            (service_namespace, destination, message_id),
        )
        row = cur.fetchone()
    return None if row is None else DeliveryReceipt.from_row(row)


def _expire(
    conn: DbConnection | ReceiptCapabilityConnection, *, service_namespace: str, destination: str, message_id: str
) -> bool:
    return (
        _change(
            conn,
            service_namespace=service_namespace,
            destination=destination,
            message_id=message_id,
            values=("Sender lease expired; provider outcome unobservable",),
            fences=(),
            transition="lease_expired",
            actor=ReceiptActor("service", "receipt-reconciler"),
            reason="Sender lease expired; provider outcome unobservable",
        )
        is not None
    )


def claim(
    conn: DbConnection | ReceiptCapabilityConnection,
    *,
    service_namespace: str,
    destination: str,
    message_id: str,
    owner: str,
    lease_seconds: int,
) -> DeliveryReceipt | None:
    if lease_seconds < 0:
        raise ValueError("lease_seconds must be nonnegative")
    _expire(conn, service_namespace=service_namespace, destination=destination, message_id=message_id)
    return _change(
        conn,
        service_namespace=service_namespace,
        destination=destination,
        message_id=message_id,
        values=(owner, lease_seconds),
        fences=(),
        transition="claim",
        actor=ReceiptActor("service", owner),
        reason="Worker claimed effect",
    )


def _message_selection(value: object) -> list[str] | None:
    if value is None:
        return None
    error = "message_ids requires at most 1000 distinct bounded identifiers"
    if not isinstance(value, tuple):
        raise ValueError(error)
    identifiers = cast(tuple[object, ...], value)
    if len(identifiers) > 1000:
        raise ValueError(error)
    selected: list[str] = []
    for identifier in identifiers:
        if not isinstance(identifier, str) or not identifier.strip() or len(identifier.encode()) > 2048:
            raise ValueError(error)
        selected.append(identifier)
    if len(set(selected)) != len(selected):
        raise ValueError(error)
    return selected


def claim_due(
    conn: DbConnection | ReceiptCapabilityConnection,
    *,
    service_namespace: str,
    destination: str,
    owner: str,
    lease_seconds: int,
    limit: int = 20,
    message_ids: tuple[str, ...] | None = None,
) -> list[DeliveryReceipt]:
    _limit(limit)
    selection = _message_selection(message_ids)
    require_compatible_schema(conn.native if isinstance(conn, ReceiptCapabilityConnection) else conn, minimum=5)
    with conn.cursor() as cur:
        parameters = (service_namespace, destination, selection, selection, limit)
        if isinstance(conn, ReceiptCapabilityConnection):
            execute_capability(cur, "select_due", parameters)
        else:
            cur.execute(CLAIM_DUE_QUERY, parameters)
        ids = [str(row["message_id"]) for row in cur.fetchall()]
    claimed = [
        claim(
            conn,
            service_namespace=service_namespace,
            destination=destination,
            message_id=message_id,
            owner=owner,
            lease_seconds=lease_seconds,
        )
        for message_id in ids
    ]
    return [row for row in claimed if row is not None]


def _worker(
    conn: DbConnection | ReceiptCapabilityConnection,
    *,
    service_namespace: str,
    destination: str,
    message_id: str,
    owner: str,
    claim_epoch: int,
    values: tuple[Any, ...],
    transition: str,
    reason: str,
    extra_values: tuple[Any, ...] = (),
    evidence: dict[str, str] | None = None,
) -> bool:
    return (
        _change(
            conn,
            service_namespace=service_namespace,
            destination=destination,
            message_id=message_id,
            values=values,
            fences=(owner, claim_epoch, *extra_values),
            transition=transition,
            actor=ReceiptActor("service", owner),
            reason=reason,
            evidence=evidence,
        )
        is not None
    )


def renew_lease(
    conn: DbConnection | ReceiptCapabilityConnection,
    *,
    service_namespace: str,
    destination: str,
    message_id: str,
    owner: str,
    claim_epoch: int,
    lease_seconds: int,
) -> bool:
    if lease_seconds <= 0:
        raise ValueError("renewal requires a positive lease")
    return _worker(
        conn,
        service_namespace=service_namespace,
        destination=destination,
        message_id=message_id,
        owner=owner,
        claim_epoch=claim_epoch,
        values=(lease_seconds,),
        transition="renew_lease",
        reason="Worker renewed lease",
    )


def start_effect(
    conn: DbConnection | ReceiptCapabilityConnection,
    *,
    service_namespace: str,
    destination: str,
    message_id: str,
    owner: str,
    claim_epoch: int,
) -> bool:
    """Commit the returned marker before the first provider mutation; false means stop."""
    return _worker(
        conn,
        service_namespace=service_namespace,
        destination=destination,
        message_id=message_id,
        owner=owner,
        claim_epoch=claim_epoch,
        values=(),
        transition="start_effect",
        reason="Provider effect authorized to start",
    )


def record_provider_ref(
    conn: DbConnection | ReceiptCapabilityConnection,
    *,
    service_namespace: str,
    destination: str,
    message_id: str,
    owner: str,
    claim_epoch: int,
    provider_ref: str,
) -> bool:
    if not provider_ref.strip() or len(provider_ref) > 2000:
        raise ValueError("provider reference must be nonempty and bounded")
    return _worker(
        conn,
        service_namespace=service_namespace,
        destination=destination,
        message_id=message_id,
        owner=owner,
        claim_epoch=claim_epoch,
        values=(provider_ref,),
        extra_values=(provider_ref,),
        transition="record_provider_ref",
        reason="Provider reference persisted",
    )


def _settle(
    conn: DbConnection | ReceiptCapabilityConnection,
    *,
    service_namespace: str,
    destination: str,
    message_id: str,
    owner: str,
    claim_epoch: int,
    status: str,
    reason: str,
    provider_ref: str | None = None,
    extra_values: tuple[Any, ...] = (),
    evidence: dict[str, str] | None = None,
    delay: float = 0,
) -> bool:
    return _worker(
        conn,
        service_namespace=service_namespace,
        destination=destination,
        message_id=message_id,
        owner=owner,
        claim_epoch=claim_epoch,
        values=(status, provider_ref, provider_ref, None if status == "delivered" else reason, delay, status),
        extra_values=(*extra_values, provider_ref, provider_ref),
        transition=status,
        reason=reason,
        evidence=evidence,
    )


def mark_delivered(
    conn: DbConnection | ReceiptCapabilityConnection,
    *,
    service_namespace: str,
    destination: str,
    message_id: str,
    owner: str,
    claim_epoch: int,
    provider_ref: str | None = None,
) -> bool:
    return _settle(
        conn,
        service_namespace=service_namespace,
        destination=destination,
        message_id=message_id,
        owner=owner,
        claim_epoch=claim_epoch,
        status="delivered",
        reason="Provider confirmed effect",
        provider_ref=provider_ref,
        extra_values=(provider_ref,),
    )


def mark_failed(
    conn: DbConnection | ReceiptCapabilityConnection,
    *,
    service_namespace: str,
    destination: str,
    message_id: str,
    owner: str,
    claim_epoch: int,
    error: str,
    attempts: int,
    evidence: dict[str, str],
) -> bool:
    if evidence.get("outcome") != "definitive_no_effect" or not evidence.get("adapter_code"):
        raise ValueError("retry requires adapter evidence of definitive no-effect rejection")
    return _settle(
        conn,
        service_namespace=service_namespace,
        destination=destination,
        message_id=message_id,
        owner=owner,
        claim_epoch=claim_epoch,
        status="failed",
        reason=error,
        evidence=evidence,
        delay=next_delay_seconds(attempts),
    )


def mark_uncertain(
    conn: DbConnection | ReceiptCapabilityConnection,
    *,
    service_namespace: str,
    destination: str,
    message_id: str,
    owner: str,
    claim_epoch: int,
    reason: str,
    provider_ref: str | None = None,
) -> bool:
    return _settle(
        conn,
        service_namespace=service_namespace,
        destination=destination,
        message_id=message_id,
        owner=owner,
        claim_epoch=claim_epoch,
        status="uncertain",
        reason=reason,
        provider_ref=provider_ref,
    )


def mark_needs_review(
    conn: DbConnection | ReceiptCapabilityConnection,
    *,
    service_namespace: str,
    destination: str,
    message_id: str,
    owner: str,
    claim_epoch: int,
    reason: str,
    evidence: dict[str, str],
) -> bool:
    return _settle(
        conn,
        service_namespace=service_namespace,
        destination=destination,
        message_id=message_id,
        owner=owner,
        claim_epoch=claim_epoch,
        status="needs_review",
        reason=reason,
        evidence=evidence,
    )


def block(
    conn: DbConnection | ReceiptCapabilityConnection,
    *,
    service_namespace: str,
    destination: str,
    message_id: str,
    reason: str,
    actor: ReceiptActor,
) -> bool:
    return (
        _change(
            conn,
            service_namespace=service_namespace,
            destination=destination,
            message_id=message_id,
            values=(reason,),
            fences=(),
            transition="block",
            actor=actor,
            reason=reason,
        )
        is not None
    )


def block_unstarted(
    conn: DbConnection | ReceiptCapabilityConnection,
    *,
    service_namespace: str,
    destination: str,
    message_id: str,
    owner: str,
    claim_epoch: int,
    reason: str,
) -> bool:
    return _settle(
        conn,
        service_namespace=service_namespace,
        destination=destination,
        message_id=message_id,
        owner=owner,
        claim_epoch=claim_epoch,
        status="blocked",
        reason=reason,
    )


def defer_unstarted(
    conn: DbConnection | ReceiptCapabilityConnection,
    *,
    service_namespace: str,
    destination: str,
    message_id: str,
    owner: str,
    claim_epoch: int,
    reason: str,
    delay_seconds: float,
) -> bool:
    if not 0 < delay_seconds <= 3600:
        raise ValueError("backoff must be within (0,3600] seconds")
    return _settle(
        conn,
        service_namespace=service_namespace,
        destination=destination,
        message_id=message_id,
        owner=owner,
        claim_epoch=claim_epoch,
        status="pending",
        reason=reason,
        delay=delay_seconds,
    )


def reconcile_stale(
    conn: DbConnection | ReceiptCapabilityConnection, *, service_namespace: str, limit: int = 100
) -> list[str]:
    _limit(limit)
    require_compatible_schema(conn.native if isinstance(conn, ReceiptCapabilityConnection) else conn, minimum=5)
    with conn.cursor() as cur:
        parameters = (service_namespace, limit)
        if isinstance(conn, ReceiptCapabilityConnection):
            execute_capability(cur, "select_stale", parameters)
        else:
            cur.execute(STALE_QUERY, parameters)
        rows = cur.fetchall()
    return [
        str(row["message_id"])
        for row in rows
        if _expire(
            conn,
            service_namespace=service_namespace,
            destination=str(row["destination"]),
            message_id=str(row["message_id"]),
        )
    ]


def _limit(limit: int) -> None:
    if not 1 <= limit <= 1000:
        raise ValueError("limit must be between 1 and 1000")


def list_deliveries(
    conn: DbConnection | ReceiptCapabilityConnection,
    *,
    service_namespace: str,
    status: str | None = None,
    limit: int = 100,
    after: tuple[str, str] | None = None,
) -> list[DeliveryReceipt]:
    _limit(limit)
    if status is not None and status not in RECEIPT_STATUSES:
        raise ValueError("unknown receipt status")
    require_compatible_schema(conn.native if isinstance(conn, ReceiptCapabilityConnection) else conn, minimum=5)
    with conn.cursor() as cur:
        cur.execute(
            """SELECT * FROM delivery_receipts WHERE service_namespace=%s
            AND (%s::text IS NULL OR status=%s)
            AND (%s::text IS NULL OR (destination,message_id)>(%s,%s))
            ORDER BY destination,message_id LIMIT %s""",
            (
                service_namespace,
                status,
                status,
                None if after is None else after[0],
                "" if after is None else after[0],
                "" if after is None else after[1],
                limit,
            ),
        )
        return [DeliveryReceipt.from_row(row) for row in cur.fetchall()]


def list_uncertain(
    conn: DbConnection | ReceiptCapabilityConnection, *, service_namespace: str, limit: int = 100
) -> list[DeliveryReceipt]:
    return list_deliveries(conn, service_namespace=service_namespace, status="uncertain", limit=limit)


def resolve(
    conn: DbConnection | ReceiptCapabilityConnection,
    *,
    service_namespace: str,
    destination: str,
    message_id: str,
    disposition: Literal["delivered", "blocked", "retry_new_generation"],
    actor: ReceiptActor,
    note: str,
    expected_status: str,
    expected_version: int,
    evidence: dict[str, str] | None = None,
) -> bool:
    """Close an ambiguous generation. Domain code opens its successor in this transaction.

    Never re-enable this message_id. A failed CAS means the domain must not open
    a successor. Review authorization belongs to the verified adapter/database role.
    """
    if disposition not in {"delivered", "blocked", "retry_new_generation"}:
        raise ValueError("unknown resolution disposition; retrying the same message is forbidden")
    if expected_status not in {"uncertain", "needs_review"}:
        raise ValueError("resolution requires an ambiguous expected state")
    return (
        _change(
            conn,
            service_namespace=service_namespace,
            destination=destination,
            message_id=message_id,
            values=("delivered" if disposition == "delivered" else "blocked", note),
            fences=(expected_status, expected_version),
            transition=f"resolve:{disposition}",
            actor=actor,
            reason=note,
            evidence=evidence,
        )
        is not None
    )


def review_uncertain(
    conn: DbConnection | ReceiptCapabilityConnection,
    *,
    service_namespace: str,
    destination: str,
    message_id: str,
    expected_version: int,
    actor: ReceiptActor,
    reason: str,
    evidence: dict[str, str],
) -> bool:
    """An unknown effect needs review; this grants neither retry nor completion.

    Works without a provider reference, including a lost create response. The
    adapter supplies a verified service actor and redacted inspection evidence.
    A newer event or any other status loses the CAS without changing the row.
    """
    if actor.kind != "service" or expected_version < 1 or not evidence.get("evidence_ref"):
        raise ValueError("unknown-effect review requires a service and inspection evidence")
    return (
        _change(
            conn,
            service_namespace=service_namespace,
            destination=destination,
            message_id=message_id,
            values=(reason,),
            fences=(expected_version,),
            transition="review_uncertain",
            actor=actor,
            reason=reason,
            evidence=evidence,
        )
        is not None
    )


def reconcile_known_ref(
    conn: DbConnection | ReceiptCapabilityConnection,
    *,
    service_namespace: str,
    destination: str,
    message_id: str,
    provider_ref: str,
    expected_version: int,
    actor: ReceiptActor,
    expected_digest: str,
    observed_digest: str,
    evidence_ref: str,
) -> bool:
    """Read-only recovery: exact final-object digest proof, never a provider mutation."""
    if actor.kind != "service" or not all((provider_ref, expected_digest, observed_digest, evidence_ref)):
        raise ValueError("recovery requires a service principal and final-object evidence")
    matches = expected_digest == observed_digest
    return (
        _change(
            conn,
            service_namespace=service_namespace,
            destination=destination,
            message_id=message_id,
            values=("delivered" if matches else "needs_review", None if matches else "Final object digest mismatch"),
            fences=(expected_version, provider_ref),
            transition="reconcile_known_ref",
            actor=actor,
            reason="Final object verified" if matches else "Final object digest mismatch",
            evidence={
                "evidence_ref": evidence_ref,
                "expected_digest": expected_digest,
                "observed_digest": observed_digest,
            },
        )
        is not None
    )
