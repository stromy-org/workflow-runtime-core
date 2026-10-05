"""Deterministic authoring renderer; installed migration bytes stay immutable."""

# All SQL fragments below are authored constants or integer parameter positions.
# No caller, role name or provider data enters the generated DDL.
# ruff: noqa: S608

from __future__ import annotations

from ._receipt_sql import CLAIM_DUE_QUERY, OPEN_QUERY, OPERATIONS, SETTLE_TYPES, STALE_QUERY
from .receipt_capabilities import function_name


def _argument(index: int, kind: str) -> str:
    if kind == "jsonb":
        return f"(args->{index})"
    if kind == "text[]":
        return (
            f"(CASE WHEN args->{index}='null'::jsonb THEN NULL "
            f"ELSE ARRAY(SELECT jsonb_array_elements_text(args->{index})) END)"
        )
    return f"(args->>{index})::{kind}"


def _bind(query: str, kinds: tuple[str, ...]) -> str:
    pieces = query.split("%s")
    if len(pieces) != len(kinds) + 1:
        raise ValueError("receipt capability parameters differ from canonical SQL")
    return "".join(piece + _argument(index, kinds[index]) for index, piece in enumerate(pieces[:-1])) + pieces[-1]


def _text(index: int, maximum: int = 2048) -> str:
    return (
        f"jsonb_typeof(args->{index})='string' AND length(btrim(args->>{index}))>0 "
        f"AND octet_length(args->>{index})<={maximum}"
    )


def _guard(condition: str) -> str:
    return (
        f"IF ({condition}) IS NOT TRUE THEN\n"
        "    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';\n"
        "END IF;\n"
    )


def _evidence(index: int) -> str:
    return _guard(f"jsonb_typeof(args->{index})='object' AND octet_length((args->{index})::text)<=4000") + _guard(
        f"NOT EXISTS (SELECT 1 FROM jsonb_each(args->{index}) e WHERE "
        "e.key NOT IN ('evidence_ref','expected_digest','observed_digest','outcome','adapter_code') "
        "OR jsonb_typeof(e.value)<>'string')"
    )


def _audit_checks(count: int, operation: str) -> str:
    transition, kind, actor, reason, evidence = range(count - 5, count)
    checks = _guard(f"args->>{transition}='{operation}'")
    checks += _guard(
        f"args->>{kind} IN ('service','human')"
        if operation.startswith("resolve:") or operation == "block"
        else f"args->>{kind}='service'"
    )
    checks += _guard(_text(actor, 256)) + _guard(_text(reason, 2000)) + _evidence(evidence)
    spec = OPERATIONS[operation]
    fence = 3 + len(spec.value_types)
    if spec.fence_types[:2] == ("text", "bigint") and not operation.startswith("resolve:"):
        checks += _guard(f"args->>{actor}=args->>{fence} AND (args->>{fence + 1})::bigint>=1")
    if spec.value_types == SETTLE_TYPES:
        checks += _guard(f"args->>3='{operation}' AND args->>8='{operation}'")
        # The optional ref is one value, not independently forgeable copies.
        references = [4, 5, count - 7, count - 6]
        if operation == "delivered":
            references.append(fence + 2)
        checks += _guard(" AND ".join(f"args->{index}=args->4" for index in references))
        checks += _guard("args->4='null'::jsonb OR (" + _text(4, 2000) + ")")
        checks += _guard("(args->>7)::double precision BETWEEN 0 AND 3600")
        if operation == "pending":
            checks += _guard("(args->>7)::double precision>0")
        checks += _guard("args->6='null'::jsonb" if operation == "delivered" else f"args->>6=args->>{reason}")
        if operation == "failed":
            checks += _guard(
                f"args->{evidence}->>'outcome'='definitive_no_effect' "
                f"AND length(btrim(args->{evidence}->>'adapter_code'))>0"
            )
    if operation == "claim":
        checks += _guard(f"args->>3=args->>{actor} AND (args->>4)::double precision BETWEEN 0 AND 86400")
    elif operation == "renew_lease":
        checks += _guard("(args->>3)::double precision>0 AND (args->>3)::double precision<=86400")
    elif operation == "record_provider_ref":
        checks += _guard(_text(3, 2000) + " AND args->3=args->6")
    elif operation == "lease_expired":
        checks += _guard(f"args->>{actor}='receipt-reconciler'")
    elif operation == "review_uncertain":
        checks += _guard(f"(args->>4)::bigint>=1 AND length(btrim(args->{evidence}->>'evidence_ref'))>0")
    elif operation == "reconcile_known_ref":
        checks += _guard(_text(6, 2000) + " AND (args->>5)::bigint>=1")
        checks += _guard(
            " AND ".join(
                f"length(btrim(args->{evidence}->>'{key}'))>0"
                for key in ("evidence_ref", "expected_digest", "observed_digest")
            )
        )
        checks += _guard(
            f"args->>3=CASE WHEN args->{evidence}->>'expected_digest'=args->{evidence}->>'observed_digest' "
            "THEN 'delivered' ELSE 'needs_review' END"
        )
    elif operation.startswith("resolve:"):
        status = "delivered" if operation == "resolve:delivered" else "blocked"
        checks += _guard(f"args->>3='{status}' AND args->>5 IN ('uncertain','needs_review') AND (args->>6)::bigint>=1")
    return checks


def render() -> str:
    """Generate v1 from the same fixed query definitions used by native callers."""
    definitions: list[str] = []
    for operation in ("open", "select_due", "select_stale", *OPERATIONS):
        if operation == "open":
            kinds = ("text", "text", "text", "text", "text", "jsonb")
            query, returns, prefix = OPEN_QUERY, "void", ""
            checks = _guard("args->>3='service'") + _guard(_text(4, 256)) + _evidence(5)
        elif operation == "select_due":
            kinds = ("text", "text", "text[]", "text[]", "bigint")
            query, returns, prefix = CLAIM_DUE_QUERY, "TABLE(message_id text)", "RETURN QUERY "
            checks = _guard("args->2=args->3 AND (args->>4)::bigint BETWEEN 1 AND 1000")
            checks += _guard("args->2='null'::jsonb OR jsonb_typeof(args->2)='array'")
            checks += "IF args->2<>'null'::jsonb THEN\n" + _guard("jsonb_array_length(args->2)<=1000")
            checks += (
                _guard(
                    "NOT EXISTS (SELECT 1 FROM jsonb_array_elements(args->2) v WHERE "
                    "jsonb_typeof(v)<>'string' OR length(btrim(v#>>'{}'))=0 OR octet_length(v#>>'{}')>2048) "
                    "AND (SELECT count(*)=count(DISTINCT v) FROM jsonb_array_elements(args->2) v)"
                )
                + "END IF;\n"
            )
        elif operation == "select_stale":
            kinds = ("text", "bigint")
            query, returns, prefix = STALE_QUERY, "TABLE(destination text,message_id text)", "RETURN QUERY "
            checks = _guard("(args->>1)::bigint BETWEEN 1 AND 1000")
        else:
            spec = OPERATIONS[operation]
            kinds = spec.parameter_types
            query, returns, prefix = spec.query(typed_result=True), "SETOF public.delivery_receipts", "RETURN QUERY "
            checks = _audit_checks(len(kinds), operation)
        checks = _guard(f"jsonb_typeof(args)='array' AND jsonb_array_length(args)={len(kinds)}") + checks
        for index in range(min(3, len(kinds))):
            if kinds[index] == "text":
                checks += _guard(_text(index))
        name = function_name(operation)
        definitions.append(
            f"CREATE FUNCTION public.{name}(args jsonb) RETURNS {returns}\n"
            "LANGUAGE plpgsql SECURITY DEFINER STRICT SET search_path=pg_catalog,public AS $wrc$\n"
            f"BEGIN\n{checks}{prefix}{_bind(query, kinds)};\nEND;\n$wrc$;\n"
            f"REVOKE ALL ON FUNCTION public.{name}(jsonb) FROM PUBLIC;\n"
        )
    return "-- WRC receipt capabilities v1; explicit setup only. No automatic role grants.\n" + "\n".join(definitions)
