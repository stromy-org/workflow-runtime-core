"""Per-chain privilege manifests, and the reconciliation that applies them.

A migration chain knows two things nothing else does: which of its tables hold
OPERATIONAL rows that the application mutates, and which hold the LEDGER that
records what has been migrated. Only the chain can classify its own tables, so
only the chain can grant on them — which is why this lives beside the migration
code rather than in an estate's SQL file.

Why a manifest instead of ``GRANT ... ON ALL TABLES``
-----------------------------------------------------
``ON ALL TABLES`` is a snapshot that reads like a rule. Run it once and the
ledger tables are in it, so the application can rewrite the record of which
migrations ran — telling every other consumer the schema is at a version it is
not. ``ALTER DEFAULT PRIVILEGES`` has the mirror-image problem: it is a rule
that is not retroactive and applies per *creating role*, so it silently misses
whatever another role made.

The manifest below is neither. A table added by a future migration is absent
from it and therefore INACCESSIBLE to the application until someone classifies
it. That failure is the feature: it is loud, it happens in a test, and the
alternative is a ledger that quietly became writable.

Client-neutral
--------------
The role name is an argument. This module never learns an estate's role names,
never reads them from a config file, and refuses one that is not a plain SQL
identifier (see :func:`workflow_runtime_core.auth.validate_identifier`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .auth import scalar, validate_identifier

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .registry import DbConnection

#: DML an application needs on a table whose rows it owns. Deliberately not
#: TRUNCATE (a table-wide delete that bypasses the row predicates every
#: retention path is written against), and not REFERENCES or TRIGGER (both
#: schema-shaped, and a schema change is the migrator's job).
OPERATIONAL_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE")

#: All an application may do to a record of what has been migrated. It reads
#: this on every boot — require_compatible_schema, readiness probes, `wrc
#: status` — and must never write it.
LEDGER_PRIVILEGES = ("SELECT",)

#: USAGE covers nextval, which a SERIAL insert calls; SELECT covers currval.
#: UPDATE is withheld because setval rewrites the sequence position, which is
#: not ordinary use.
SEQUENCE_PRIVILEGES = ("USAGE", "SELECT")

#: Mutations explicitly revoked from a ledger before the SELECT grant, so a
#: re-run REPAIRS a ledger that was widened by hand rather than leaving the
#: widening in place beside a redundant grant.
LEDGER_REVOCATIONS = ("INSERT", "UPDATE", "DELETE", "TRUNCATE")


@dataclass(frozen=True)
class GrantManifest:
    """One chain's classification of the objects it owns."""

    #: Chain name, used only in messages.
    name: str
    #: Tables whose rows the application creates and mutates.
    operational_tables: tuple[str, ...] = ()
    #: Tables recording migration history. Application-readable, never writable.
    ledger_tables: tuple[str, ...] = ()
    #: Sequences the operational inserts advance.
    sequences: tuple[str, ...] = ()
    #: Set at construction; see :meth:`__post_init__`.
    _all: tuple[str, ...] = field(default=(), repr=False, compare=False)

    def __post_init__(self) -> None:
        overlap = set(self.operational_tables) & set(self.ledger_tables)
        if overlap:
            # Not a defensive check: a table classified both ways would receive
            # the ledger REVOKE and the operational GRANT, and which one won
            # would depend on statement order. Refuse to have that ambiguity.
            raise ValueError(
                f"{self.name}: {sorted(overlap)} classified as both operational and ledger — "
                "a table is one or the other, and the ordering of the resulting "
                "GRANT/REVOKE pair would decide the outcome silently"
            )

    def tables(self) -> tuple[str, ...]:
        return self.operational_tables + self.ledger_tables


#: The wrc-core chain's own objects. This is the authoritative classification
#: for everything :mod:`workflow_runtime_core.migrations` creates — the run
#: registry, the event log, the messaging tables, and the two ledgers.
#:
#: schema_meta and schema_migrations are ledgers because they answer "which
#: migrations ran". That is the claim a compromised application must not be able
#: to forge, and it is a different question from "is this row current", which
#: the operational tables answer and the application legitimately rewrites.
CORE_MANIFEST = GrantManifest(
    name="core",
    operational_tables=(
        "runs",
        "run_events",
        "event_inbox",
        "run_launches",
        "event_outbox",
        "delivery_receipts",
    ),
    ledger_tables=(
        "schema_meta",
        "schema_migrations",
    ),
    sequences=("run_events_event_id_seq",),
)

#: LangGraph's checkpoint store, as created by the saver's own ``setup()``.
#: checkpoint_migrations is a migration ledger in exactly the sense that matters
#: here — it is how the saver decides whether its schema is current — so it gets
#: the same treatment as ours. That is also why the hosted runtime runs
#: WRC_CHECKPOINT_SETUP=verify: an application allowed to write this table could
#: claim a migration it never applied.
CHECKPOINT_MANIFEST = GrantManifest(
    name="langgraph-checkpoint",
    operational_tables=(
        "checkpoints",
        "checkpoint_blobs",
        "checkpoint_writes",
    ),
    ledger_tables=("checkpoint_migrations",),
)


def _statements(manifest: GrantManifest, role: str, schema: str) -> list[str]:
    """Render ``manifest`` for ``role`` as ordered SQL.

    Order matters exactly once: each ledger REVOKE precedes its GRANT, so a
    hand-widened ledger is narrowed and then re-granted SELECT rather than left
    widened.
    """
    ops = ", ".join(OPERATIONAL_PRIVILEGES)
    ledger = ", ".join(LEDGER_PRIVILEGES)
    seq = ", ".join(SEQUENCE_PRIVILEGES)
    revoke = ", ".join(LEDGER_REVOCATIONS)

    out: list[str] = [f"GRANT {ops} ON {schema}.{table} TO {role}" for table in manifest.operational_tables]
    for table in manifest.ledger_tables:
        out.append(f"REVOKE {revoke} ON {schema}.{table} FROM {role}")
        out.append(f"GRANT {ledger} ON {schema}.{table} TO {role}")
    out.extend(f"GRANT {seq} ON SEQUENCE {schema}.{sequence} TO {role}" for sequence in manifest.sequences)
    return out


def reconcile(
    conn: DbConnection,
    manifest: GrantManifest,
    *,
    application_role: str,
    schema: str = "public",
) -> list[str]:
    """Apply ``manifest`` for ``application_role``. Returns the statements run.

    Idempotent — every statement restates a desired end state rather than
    diffing — and safe to call after every migration, which is where it belongs:
    a chain that adds a table and forgets to grant on it produces a broken
    application, and running this in the same command is what makes forgetting
    hard.

    Requires the caller to own the objects (or hold GRANT OPTION), so it runs
    after the migration command has elevated to the owner role.
    """
    role = validate_identifier(application_role, what="application role")
    schema_name = validate_identifier(schema, what="schema")
    executed: list[str] = []
    with conn.cursor() as cur:
        for statement in _statements(manifest, role, schema_name):
            # Identifiers, not values: GRANT takes no parameters. Both the role
            # and the schema are validated above; the table and sequence names
            # are module constants, never caller input.
            # No params tuple, deliberately: GRANT/REVOKE take identifiers, not
            # values, so there is nothing to parameterise. pyright resolves the
            # bare-string call to psycopg's t-string overload — the same ignore
            # the migration runner carries for the same reason.
            cur.execute(statement)  # noqa: S608 # pyright: ignore[reportArgumentType, reportCallIssue]
            executed.append(statement)
    return executed


def missing_privileges(
    conn: DbConnection,
    manifest: GrantManifest,
    *,
    application_role: str,
    schema: str = "public",
) -> list[str]:
    """Report where the live grants disagree with ``manifest``. Never mutates.

    Checks BOTH directions, which is the half a "did the grant land" check
    usually misses: a missing operational privilege breaks the application
    loudly, while a ledger the application can *write* breaks nothing visibly
    and is the actual security failure.
    """
    role = validate_identifier(application_role, what="application role")
    schema_name = validate_identifier(schema, what="schema")
    findings: list[str] = []

    with conn.cursor() as cur:
        for table in manifest.operational_tables:
            for priv in OPERATIONAL_PRIVILEGES:
                cur.execute(
                    "SELECT has_table_privilege(%s, %s, %s)",
                    (role, f"{schema_name}.{table}", priv),
                )
                if not scalar(cur.fetchone()):
                    findings.append(f"{role} lacks {priv} on {schema_name}.{table}")

        for table in manifest.ledger_tables:
            cur.execute(
                "SELECT has_table_privilege(%s, %s, 'SELECT')",
                (role, f"{schema_name}.{table}"),
            )
            if not scalar(cur.fetchone()):
                findings.append(f"{role} lacks SELECT on ledger {schema_name}.{table}")
            for priv in ("INSERT", "UPDATE", "DELETE"):
                cur.execute(
                    "SELECT has_table_privilege(%s, %s, %s)",
                    (role, f"{schema_name}.{table}", priv),
                )
                if scalar(cur.fetchone()):
                    findings.append(
                        f"{role} HOLDS {priv} on ledger {schema_name}.{table} — "
                        "an application that can write a migration ledger can claim "
                        "a migration it never applied"
                    )

        for sequence in manifest.sequences:
            for priv in SEQUENCE_PRIVILEGES:
                cur.execute(
                    "SELECT has_sequence_privilege(%s, %s, %s)",
                    (role, f"{schema_name}.{sequence}", priv),
                )
                if not scalar(cur.fetchone()):
                    findings.append(f"{role} lacks {priv} on sequence {schema_name}.{sequence}")

    return findings


__all__ = [
    "CHECKPOINT_MANIFEST",
    "CORE_MANIFEST",
    "LEDGER_PRIVILEGES",
    "LEDGER_REVOCATIONS",
    "OPERATIONAL_PRIVILEGES",
    "SEQUENCE_PRIVILEGES",
    "GrantManifest",
    "missing_privileges",
    "reconcile",
]
