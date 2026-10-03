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
    conn: DbConnection,
    *,
    service_namespace: str,
    destination: str,
    message_id: str,
    assignments: str,
    values: tuple[Any, ...],
    predicate: str,
    fences: tuple[Any, ...],
    transition: str,
    actor: ReceiptActor,
    reason: str,
    evidence: dict[str, str] | None = None,
) -> DeliveryReceipt | None:
    """Internal SQL fragments are fixed literals; all adapter values are parameters."""
    encoded = _audit(actor, reason, evidence)
    require_compatible_schema(conn, minimum=5)
    with conn.cursor() as cur:
        cur.execute(
            cast(
                LiteralString,
                f"""
            WITH prior AS MATERIALIZED (
                SELECT * FROM delivery_receipts
                 WHERE service_namespace=%s AND destination=%s AND message_id=%s
                 FOR UPDATE
            ), changed AS (
                UPDATE delivery_receipts r SET {assignments},
                    event_version=r.event_version+1, updated_at=now()
                FROM prior p
                WHERE r.service_namespace=p.service_namespace AND r.destination=p.destination
                    AND r.message_id=p.message_id AND ({predicate})
                RETURNING r.*, p.status AS prior_status
            ), audit AS (
                INSERT INTO delivery_receipt_events (
                    service_namespace,destination,message_id,event_version,transition,
                    from_status,to_status,actor_kind,actor,reason,evidence
                ) SELECT service_namespace,destination,message_id,event_version,%s,
                    prior_status,status,%s,%s,%s,%s::jsonb FROM changed
                RETURNING event_id
            ) SELECT changed.* FROM changed CROSS JOIN audit
            """,  # noqa: S608 - fragments are internal fixed SQL, never caller input
            ),
            (
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
            ),
        )
        row = cur.fetchone()
    return None if row is None else DeliveryReceipt.from_row(row)


def open_receipt(
    conn: DbConnection,
    *,
    service_namespace: str,
    destination: str,
    message_id: str,
    actor: ReceiptActor = _OPEN_ACTOR,
) -> None:
    encoded = _audit(actor, "Receipt opened", None)
    require_compatible_schema(conn, minimum=5)
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH opened AS (
                INSERT INTO delivery_receipts(service_namespace,destination,message_id,status,event_version)
                VALUES (%s,%s,%s,'pending',1)
                ON CONFLICT (service_namespace,destination,message_id) DO NOTHING RETURNING *
            ) INSERT INTO delivery_receipt_events (
                service_namespace,destination,message_id,event_version,transition,to_status,
                actor_kind,actor,reason,evidence
            ) SELECT service_namespace,destination,message_id,event_version,'open',status,
                %s,%s,'Receipt opened',%s::jsonb FROM opened
            """,
            (service_namespace, destination, message_id, actor.kind, actor.subject, encoded),
        )


def get_receipt(
    conn: DbConnection,
    *,
    service_namespace: str,
    destination: str,
    message_id: str,
) -> DeliveryReceipt | None:
    require_compatible_schema(conn, minimum=5)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM delivery_receipts WHERE service_namespace=%s AND destination=%s AND message_id=%s",
            (service_namespace, destination, message_id),
        )
        row = cur.fetchone()
    return None if row is None else DeliveryReceipt.from_row(row)


def _expire(conn: DbConnection, *, service_namespace: str, destination: str, message_id: str) -> bool:
    return (
        _change(
            conn,
            service_namespace=service_namespace,
            destination=destination,
            message_id=message_id,
            assignments="status='uncertain', lease_owner=NULL, lease_expires_at=NULL, last_error=%s",
            values=("Sender lease expired; provider outcome unobservable",),
            predicate="p.status='sending' AND p.lease_expires_at<=now()",
            fences=(),
            transition="lease_expired",
            actor=ReceiptActor("service", "receipt-reconciler"),
            reason="Sender lease expired; provider outcome unobservable",
        )
        is not None
    )


def claim(
    conn: DbConnection,
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
        assignments="status='sending', attempts=r.attempts+1, claim_epoch=r.claim_epoch+1, "
        "lease_owner=%s, lease_expires_at=now()+make_interval(secs=>%s)",
        values=(owner, lease_seconds),
        predicate="p.status IN ('pending','failed') AND p.provider_ref IS NULL",
        fences=(),
        transition="claim",
        actor=ReceiptActor("service", owner),
        reason="Worker claimed effect",
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
    _limit(limit)
    require_compatible_schema(conn, minimum=5)
    with conn.cursor() as cur:
        cur.execute(
            """SELECT message_id FROM delivery_receipts
            WHERE service_namespace=%s AND destination=%s AND status IN ('pending','failed')
                AND next_attempt_at<=now() ORDER BY next_attempt_at,message_id LIMIT %s
            FOR UPDATE SKIP LOCKED""",
            (service_namespace, destination, limit),
        )
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


_LIVE = "p.status='sending' AND p.lease_owner=%s AND p.claim_epoch=%s AND p.lease_expires_at>now()"


def _worker(
    conn: DbConnection,
    *,
    service_namespace: str,
    destination: str,
    message_id: str,
    owner: str,
    claim_epoch: int,
    assignments: str,
    values: tuple[Any, ...],
    transition: str,
    reason: str,
    extra: str = "TRUE",
    extra_values: tuple[Any, ...] = (),
    evidence: dict[str, str] | None = None,
) -> bool:
    return (
        _change(
            conn,
            service_namespace=service_namespace,
            destination=destination,
            message_id=message_id,
            assignments=assignments,
            values=values,
            predicate=f"{_LIVE} AND ({extra})",
            fences=(owner, claim_epoch, *extra_values),
            transition=transition,
            actor=ReceiptActor("service", owner),
            reason=reason,
            evidence=evidence,
        )
        is not None
    )


def renew_lease(
    conn: DbConnection,
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
        assignments="lease_expires_at=now()+make_interval(secs=>%s)",
        values=(lease_seconds,),
        transition="renew_lease",
        reason="Worker renewed lease",
    )


def start_effect(
    conn: DbConnection,
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
        assignments="effect_started_at=now()",
        values=(),
        extra="p.effect_started_at IS NULL AND p.provider_ref IS NULL",
        transition="start_effect",
        reason="Provider effect authorized to start",
    )


def record_provider_ref(
    conn: DbConnection,
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
        assignments="provider_ref=%s, provider_ref_recorded_at=COALESCE(r.provider_ref_recorded_at,now())",
        values=(provider_ref,),
        extra="p.effect_started_at IS NOT NULL AND (p.provider_ref IS NULL OR p.provider_ref=%s)",
        extra_values=(provider_ref,),
        transition="record_provider_ref",
        reason="Provider reference persisted",
    )


def _settle(
    conn: DbConnection,
    *,
    service_namespace: str,
    destination: str,
    message_id: str,
    owner: str,
    claim_epoch: int,
    status: str,
    reason: str,
    provider_ref: str | None = None,
    extra: str = "TRUE",
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
        assignments="status=%s, provider_ref=COALESCE(r.provider_ref,%s), "
        "provider_ref_recorded_at=CASE WHEN COALESCE(r.provider_ref,%s) IS NOT NULL "
        "THEN COALESCE(r.provider_ref_recorded_at,now()) ELSE NULL END, "
        "lease_owner=NULL, lease_expires_at=NULL, last_error=%s, "
        "next_attempt_at=now()+make_interval(secs=>%s), "
        "effect_started_at=CASE WHEN %s='failed' THEN NULL ELSE r.effect_started_at END",
        values=(status, provider_ref, provider_ref, None if status == "delivered" else reason, delay, status),
        extra=f"({extra}) AND (p.provider_ref IS NULL OR %s::text IS NULL OR p.provider_ref=%s)",
        extra_values=(*extra_values, provider_ref, provider_ref),
        transition=status,
        reason=reason,
        evidence=evidence,
    )


def mark_delivered(
    conn: DbConnection,
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
        extra="p.effect_started_at IS NOT NULL AND (p.provider_ref IS NOT NULL OR %s::text IS NULL)",
        extra_values=(provider_ref,),
    )


def mark_failed(
    conn: DbConnection,
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
        extra="p.provider_ref IS NULL",
        evidence=evidence,
        delay=next_delay_seconds(attempts),
    )


def mark_uncertain(
    conn: DbConnection,
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
    conn: DbConnection,
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
    conn: DbConnection,
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
            assignments="status='blocked',last_error=%s",
            values=(reason,),
            predicate="p.status IN ('pending','failed')",
            fences=(),
            transition="block",
            actor=actor,
            reason=reason,
        )
        is not None
    )


def block_unstarted(
    conn: DbConnection,
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
        extra="p.effect_started_at IS NULL AND p.provider_ref IS NULL",
    )


def defer_unstarted(
    conn: DbConnection,
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
        extra="p.effect_started_at IS NULL AND p.provider_ref IS NULL",
    )


def reconcile_stale(conn: DbConnection, *, service_namespace: str, limit: int = 100) -> list[str]:
    _limit(limit)
    require_compatible_schema(conn, minimum=5)
    with conn.cursor() as cur:
        cur.execute(
            """SELECT destination,message_id FROM delivery_receipts
            WHERE service_namespace=%s AND status='sending' AND lease_expires_at<=now()
            ORDER BY lease_expires_at,message_id LIMIT %s FOR UPDATE SKIP LOCKED""",
            (service_namespace, limit),
        )
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
    conn: DbConnection,
    *,
    service_namespace: str,
    status: str | None = None,
    limit: int = 100,
    after: tuple[str, str] | None = None,
) -> list[DeliveryReceipt]:
    _limit(limit)
    if status is not None and status not in RECEIPT_STATUSES:
        raise ValueError("unknown receipt status")
    require_compatible_schema(conn, minimum=5)
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


def list_uncertain(conn: DbConnection, *, service_namespace: str, limit: int = 100) -> list[DeliveryReceipt]:
    return list_deliveries(conn, service_namespace=service_namespace, status="uncertain", limit=limit)


def resolve(
    conn: DbConnection,
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
            assignments="status=%s,last_error=%s,lease_owner=NULL,lease_expires_at=NULL",
            values=("delivered" if disposition == "delivered" else "blocked", note),
            predicate="p.status=%s AND p.event_version=%s",
            fences=(expected_status, expected_version),
            transition=f"resolve:{disposition}",
            actor=actor,
            reason=note,
            evidence=evidence,
        )
        is not None
    )


def reconcile_known_ref(
    conn: DbConnection,
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
            assignments="status=%s,last_error=%s,lease_owner=NULL,lease_expires_at=NULL",
            values=("delivered" if matches else "needs_review", None if matches else "Final object digest mismatch"),
            predicate="p.status='uncertain' AND p.event_version=%s AND p.provider_ref=%s",
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
