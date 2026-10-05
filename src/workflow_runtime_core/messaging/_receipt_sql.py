"""Fixed receipt operations shared by native DML and optional SQL capabilities.

These definitions are WRC-owned; a consumer supplies values, never SQL fragments.
Changing an installed capability requires a new numbered capability migration.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Operation:
    assignments: str
    predicate: str
    value_types: tuple[str, ...] = ()
    fence_types: tuple[str, ...] = ()

    @property
    def parameter_types(self) -> tuple[str, ...]:
        return ("text", "text", "text", *self.value_types, *self.fence_types, "text", "text", "text", "text", "jsonb")

    def query(self, *, typed_result: bool = False) -> str:
        result = (
            "(jsonb_populate_record(NULL::public.delivery_receipts,to_jsonb(changed))).*"
            if typed_result
            else "changed.*"
        )
        return f"""
        WITH prior AS MATERIALIZED (
            SELECT * FROM public.delivery_receipts
            WHERE service_namespace=%s AND destination=%s AND message_id=%s FOR UPDATE
        ), changed AS (
            UPDATE public.delivery_receipts r SET {self.assignments},
                event_version=r.event_version+1, updated_at=now()
            FROM prior p WHERE r.service_namespace=p.service_namespace AND r.destination=p.destination
                AND r.message_id=p.message_id AND ({self.predicate})
            RETURNING r.*, p.status AS prior_status
        ), audit AS (
            INSERT INTO public.delivery_receipt_events (
                service_namespace,destination,message_id,event_version,transition,
                from_status,to_status,actor_kind,actor,reason,evidence
            ) SELECT service_namespace,destination,message_id,event_version,%s,
                prior_status,status,%s,%s,%s,%s::jsonb FROM changed RETURNING event_id
        ) SELECT {result} FROM changed CROSS JOIN audit
        """  # noqa: S608 - only this module's fixed definitions enter these fragments


LIVE = "p.status='sending' AND p.lease_owner=%s AND p.claim_epoch=%s AND p.lease_expires_at>now()"
SETTLE = (
    "status=%s, provider_ref=COALESCE(r.provider_ref,%s), "
    "provider_ref_recorded_at=CASE WHEN COALESCE(r.provider_ref,%s) IS NOT NULL "
    "THEN COALESCE(r.provider_ref_recorded_at,now()) ELSE NULL END, "
    "lease_owner=NULL, lease_expires_at=NULL, last_error=%s, "
    "next_attempt_at=now()+make_interval(secs=>%s), "
    "effect_started_at=CASE WHEN %s='failed' THEN NULL ELSE r.effect_started_at END"
)
SETTLE_TYPES = ("text", "text", "text", "text", "double precision", "text")


def _settlement(extra: str = "TRUE", extra_types: tuple[str, ...] = ()) -> Operation:
    return Operation(
        SETTLE,
        f"{LIVE} AND (({extra}) AND (p.provider_ref IS NULL OR %s::text IS NULL OR p.provider_ref=%s))",
        SETTLE_TYPES,
        ("text", "bigint", *extra_types, "text", "text"),
    )


OPERATIONS = {
    "lease_expired": Operation(
        "status='uncertain', lease_owner=NULL, lease_expires_at=NULL, last_error=%s",
        "p.status='sending' AND p.lease_expires_at<=now()",
        ("text",),
    ),
    "claim": Operation(
        "status='sending', attempts=r.attempts+1, claim_epoch=r.claim_epoch+1, "
        "lease_owner=%s, lease_expires_at=now()+make_interval(secs=>%s)",
        "p.status IN ('pending','failed') AND p.provider_ref IS NULL",
        ("text", "double precision"),
    ),
    "renew_lease": Operation(
        "lease_expires_at=now()+make_interval(secs=>%s)",
        f"{LIVE} AND (TRUE)",
        ("double precision",),
        ("text", "bigint"),
    ),
    "start_effect": Operation(
        "effect_started_at=now()",
        f"{LIVE} AND (p.effect_started_at IS NULL AND p.provider_ref IS NULL)",
        (),
        ("text", "bigint"),
    ),
    "record_provider_ref": Operation(
        "provider_ref=%s, provider_ref_recorded_at=COALESCE(r.provider_ref_recorded_at,now())",
        f"{LIVE} AND (p.effect_started_at IS NOT NULL AND (p.provider_ref IS NULL OR p.provider_ref=%s))",
        ("text",),
        ("text", "bigint", "text"),
    ),
    "delivered": _settlement(
        "p.effect_started_at IS NOT NULL AND (p.provider_ref IS NOT NULL OR %s::text IS NULL)",
        ("text",),
    ),
    "failed": _settlement("p.provider_ref IS NULL"),
    "uncertain": _settlement(),
    "needs_review": _settlement(),
    "blocked": _settlement("p.effect_started_at IS NULL AND p.provider_ref IS NULL"),
    "pending": _settlement("p.effect_started_at IS NULL AND p.provider_ref IS NULL"),
    "block": Operation("status='blocked',last_error=%s", "p.status IN ('pending','failed')", ("text",)),
    "review_uncertain": Operation(
        "status='needs_review',last_error=%s,lease_owner=NULL,lease_expires_at=NULL",
        "p.status='uncertain' AND p.event_version=%s",
        ("text",),
        ("bigint",),
    ),
    "reconcile_known_ref": Operation(
        "status=%s,last_error=%s,lease_owner=NULL,lease_expires_at=NULL",
        "p.status='uncertain' AND p.event_version=%s AND p.provider_ref=%s",
        ("text", "text"),
        ("bigint", "text"),
    ),
    **{
        f"resolve:{disposition}": Operation(
            "status=%s,last_error=%s,lease_owner=NULL,lease_expires_at=NULL",
            "p.status=%s AND p.event_version=%s",
            ("text", "text"),
            ("text", "bigint"),
        )
        for disposition in ("delivered", "blocked", "retry_new_generation")
    },
}

OPEN_QUERY = """
WITH opened AS (
    INSERT INTO public.delivery_receipts(service_namespace,destination,message_id,status,event_version)
    VALUES (%s,%s,%s,'pending',1)
    ON CONFLICT (service_namespace,destination,message_id) DO NOTHING RETURNING *
) INSERT INTO public.delivery_receipt_events (
    service_namespace,destination,message_id,event_version,transition,to_status,
    actor_kind,actor,reason,evidence
) SELECT service_namespace,destination,message_id,event_version,'open',status,
    %s,%s,'Receipt opened',%s::jsonb FROM opened
"""
CLAIM_DUE_QUERY = """SELECT r.message_id FROM public.delivery_receipts r
WHERE r.service_namespace=%s AND r.destination=%s AND r.status IN ('pending','failed')
    AND r.next_attempt_at<=now() AND (%s::text[] IS NULL OR r.message_id=ANY(%s::text[]))
ORDER BY r.next_attempt_at,r.message_id LIMIT %s FOR UPDATE SKIP LOCKED"""
STALE_QUERY = """SELECT r.destination,r.message_id FROM public.delivery_receipts r
WHERE r.service_namespace=%s AND r.status='sending' AND r.lease_expires_at<=now()
ORDER BY r.lease_expires_at,r.message_id LIMIT %s FOR UPDATE SKIP LOCKED"""
