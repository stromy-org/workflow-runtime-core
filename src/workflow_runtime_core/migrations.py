"""Explicit, operator-applied schema migrations.

The rule this module exists to enforce (ORG-PLAN-155 locked decision 5):

    **Application startup never runs DDL.**

Before this extraction, ``Stromy``'s scheduled launcher called ``migrate()`` on
every run so a freshly provisioned Postgres would self-heal. That is convenient
and wrong at three-consumer scale: the shared registry is read by the public
workflow facade too, and an application scheduler that silently upgrades the
shared schema can move it out from under a facade that is still compiled against
the previous version. Migration is therefore an explicit operator act
(``wrc migrate``) run with a migration-scoped identity, while applications only
call :func:`~workflow_runtime_core.schema.require_compatible_schema`.

Phase A shipped migration ``0001`` only, which is byte/behaviour compatible with
the live Stromy schema v1 — applying it to an already-migrated v1 database is a
no-op.

B1a (ORG-191) adds the ``schema_migrations`` checksum ledger ahead of any
``0002``: every applied migration's checksum is recorded on apply and verified
fail-closed on every later apply *and* at application startup. The ledger exists
because two plans once defined incompatible shapes both called "v2" behind the
same ``schema_meta.version`` — a number cannot detect that, a content digest
can (see the 2026-08-06 resolution in ``PLAN_agent-service-scaffold.md``).

The ledger carries a ``namespace`` column for the same reason. The fork happened
because an application held the pen over shared tables, so the ownership rule is
now structural: the ``core`` namespace is this module's linear chain and nothing
else may write it; an application that owns additive tables of its own (for
example the workflow facade's upload sessions) records them through
:func:`apply_app_migrations` under its own namespace, in the same ledger, with
the same fail-closed verification.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .exceptions import MigrationChecksumMismatch, MigrationError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .registry import DbConnection

# A fixed, transaction-scoped advisory lock id. Every migrator competes for this
# one key, so concurrent migrators serialise instead of racing the same DDL.
# The value is arbitrary but must never change: it IS the mutual-exclusion
# identity shared across processes and releases.
MIGRATION_ADVISORY_LOCK_ID = 0x57524301  # "WRC\x01"

#: The ledger namespace owned by this module's ``MIGRATIONS`` chain. Reserved:
#: :func:`apply_app_migrations` refuses it.
CORE_NAMESPACE = "core"

#: Core migrations at or below this version may legitimately predate the ledger
#: itself (v1 databases were migrated by pre-ledger builds, and before the
#: extraction by Stromy's auto-DDL). Their absent ledger rows are backfilled on
#: the next ``wrc migrate`` rather than treated as corruption. Anything ABOVE
#: this version can only have been applied by a ledger-aware build, so a missing
#: row there is unaccounted history and fails closed. This constant never moves:
#: it marks the pre-ledger era, not "the current version".
PRE_LEDGER_MAX_VERSION = 1

#: App namespaces are short, DNS-ish identifiers so they read cleanly in the
#: ledger and can never collide with ``core`` by case or punctuation games.
_APP_NAMESPACE_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")

_LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    namespace  TEXT        NOT NULL,
    version    INTEGER     NOT NULL,
    name       TEXT        NOT NULL,
    checksum   TEXT        NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (namespace, version)
);
"""


_V1_DDL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    version    INTEGER     NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- one row, enforced structurally rather than by discipline
    singleton  BOOLEAN     NOT NULL DEFAULT TRUE UNIQUE CHECK (singleton)
);

CREATE TABLE IF NOT EXISTS runs (
    run_id            UUID        PRIMARY KEY,
    workflow          TEXT        NOT NULL,
    thread_id         TEXT        NOT NULL,
    status            TEXT        NOT NULL CHECK (status IN (
                          'queued','running','paused','completed','failed','cancelled')),
    client_slug       TEXT,
    config_json       JSONB       NOT NULL DEFAULT '{}'::jsonb,
    image_tag         TEXT,
    -- The EXACT rendered job template. Resume replays it byte-identical, so an
    -- infrastructure-side env change can never make a resume diverge from its
    -- original run. Never contains caller-supplied values: starting a job grants
    -- access to all of its secrets.
    job_template_json JSONB,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    interrupt_payload JSONB,
    error             TEXT,
    artifacts_json    JSONB,
    idempotency_key   TEXT
);

-- Double-start protection: same key returns the existing run instead of a second.
CREATE UNIQUE INDEX IF NOT EXISTS runs_idempotency_key_uniq
    ON runs (idempotency_key) WHERE idempotency_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS runs_client_slug_idx ON runs (client_slug);
CREATE INDEX IF NOT EXISTS runs_status_idx      ON runs (status);

CREATE TABLE IF NOT EXISTS run_events (
    event_id   BIGSERIAL   PRIMARY KEY,
    run_id     UUID        NOT NULL REFERENCES runs (run_id) ON DELETE CASCADE,
    kind       TEXT        NOT NULL,
    detail     JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS run_events_run_id_idx ON run_events (run_id, created_at);
"""


@dataclass(frozen=True)
class Migration:
    """One numbered, immutable schema step."""

    version: int
    name: str
    sql: str

    @property
    def checksum(self) -> str:
        """Stable digest of the SQL body.

        Recorded in ``schema_migrations`` on apply and compared fail-closed by
        :func:`verify_ledger`. Recorded and verified by this same function, so
        the two can never drift.
        """
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()


# v2 (ORG-PLAN-164 / 2026-08-06 fork resolution): the workflow data plane —
# workspace/attempt lineage, dispatch leases, and central progress. Purely
# additive; every new column is nullable or defaulted, so a v1-compiled reader
# keeps working against a v2 database. That is what makes the
# expand/migrate/cutover/contract order safe.
#
# Column names are the RECONCILED shape: ``lease_expires_at`` (ORG-PLAN-155's
# name), ``attempt_no``/``retry_of``/``workspace_id`` (ORG-PLAN-164's lineage —
# a per-attempt record, not a counter). ORG-PLAN-155's messaging tables land as
# 0003, and the workflow facade's upload-session tables live in its own
# app-owned ledger namespace, per the ownership rule: this chain carries only
# what THIS library reads and writes.
#
# workspace_id backfills to run_id: every legacy run becomes its own workspace,
# which is exactly what it already was (one ephemeral folder per run). That lets
# the column be NOT NULL from the start rather than carrying a nullable column
# forever with an "is it set yet" branch at every read.
_V2_DDL = """
ALTER TABLE runs ADD COLUMN IF NOT EXISTS workspace_id     UUID;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS retry_of         UUID REFERENCES runs (run_id);
ALTER TABLE runs ADD COLUMN IF NOT EXISTS attempt_no       INTEGER NOT NULL DEFAULT 1;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS dispatch_id      UUID;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS lease_owner      TEXT;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS heartbeat_at     TIMESTAMPTZ;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS progress_json    JSONB;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS error_json       JSONB;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS delivery_count   INTEGER NOT NULL DEFAULT 0;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS artifacts_published_at TIMESTAMPTZ;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS input_set_id     UUID;

UPDATE runs SET workspace_id = run_id WHERE workspace_id IS NULL;
ALTER TABLE runs ALTER COLUMN workspace_id SET NOT NULL;

-- At most ONE non-terminal attempt per workspace. This is the constraint that
-- makes retry safe: two live attempts sharing a mutable folder would interleave
-- writes into each other's stage outputs, and no amount of application-level
-- care survives a crash at the wrong moment.
CREATE UNIQUE INDEX IF NOT EXISTS runs_active_workspace_uniq
    ON runs (workspace_id)
    WHERE status IN ('queued', 'running', 'paused');

-- A dispatch id identifies one enqueue attempt. Redelivery of the same message
-- must not be able to start a second writer, so the claim checks it.
CREATE INDEX IF NOT EXISTS runs_dispatch_id_idx ON runs (dispatch_id)
    WHERE dispatch_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS runs_lease_expires_at_idx ON runs (lease_expires_at)
    WHERE lease_expires_at IS NOT NULL;
"""


# v3 (ORG-PLAN-155 Phase B, renumbered from B1b by the 2026-08-06 resolution):
# the durable messaging boundary — inbox, launch, outbox, delivery receipts.
# Purely additive: it creates four new tables and alters nothing 0002 defined,
# so a v2-compiled reader keeps working against a v3 database untouched.
#
# Why these are CORE tables and not an app chain: the core's own ingress,
# dispatcher, egress and receipt loops read and write them. The ownership rule
# from the fork resolution is "anything the core reads or writes changes only
# through the core's chain", so they belong here. A service's *own* additive
# tables still go through `apply_app_migrations` under its own namespace.
#
# ``service_namespace`` appears in every uniqueness boundary rather than in a
# table name. One database can host several services, and a per-service table
# name would make "list every uncertain delivery" a query you cannot write
# without first enumerating services. It is immutable after the first persisted
# run precisely so these keys stay stable.
#
# Two leases, deliberately distinct:
#   * ``runs.lease_owner``/``lease_expires_at`` (0002) — who is EXECUTING a run.
#   * ``run_launches.lease_owner``/``lease_expires_at`` — who is LAUNCHING it.
# They have different owners, different durations and different recovery rules;
# collapsing them would make a slow launcher look like a dead runner.
#
# DEVIATION from the plan text, deliberate: the plan's 0003 bullet also listed
# ``runs.next_attempt_at`` and ``runs.execution_ref``. Both are omitted. Launch
# retry state is exactly what ``run_launches`` owns (ORG-191 decision 2: "a
# launch has its own retry lifecycle; a retry must be a child row, not a
# mutation of the run"), so putting the same two facts on ``runs`` as well would
# create a second source of truth for them and guarantee they drift. Nothing in
# the core reads them from ``runs``.
_V3_DDL = """
-- Inbound de-duplication. The unique key is the CHANNEL's own message id, so a
-- redelivered broker message resolves to the run it already created instead of
-- starting a second one. Written in the same transaction as the run and its
-- launch; only that commit permits a broker acknowledgement.
CREATE TABLE IF NOT EXISTS event_inbox (
    inbox_id          UUID        PRIMARY KEY,
    service_namespace TEXT        NOT NULL,
    source            TEXT        NOT NULL,
    source_message_id TEXT        NOT NULL,
    run_id            UUID        NOT NULL REFERENCES runs (run_id) ON DELETE CASCADE,
    envelope_json     JSONB       NOT NULL,
    received_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- THE idempotency barrier for ingress. Not a plain index: the INSERT relies on
-- the conflict to return the original run_id.
CREATE UNIQUE INDEX IF NOT EXISTS event_inbox_source_uniq
    ON event_inbox (service_namespace, source, source_message_id);

CREATE INDEX IF NOT EXISTS event_inbox_run_id_idx ON event_inbox (run_id);
-- Retention purge scans by age within a namespace.
CREATE INDEX IF NOT EXISTS event_inbox_received_at_idx
    ON event_inbox (service_namespace, received_at);


-- One launch per run: the dispatcher's durable record of having asked some
-- launcher to start it. ``params_hash`` makes a redundant dispatch detectable
-- rather than merely duplicated, and ``execution_ref`` is whatever the launcher
-- can be asked about later (a subprocess pid, an ECS task ARN).
CREATE TABLE IF NOT EXISTS run_launches (
    run_id           UUID        PRIMARY KEY REFERENCES runs (run_id) ON DELETE CASCADE,
    launcher         TEXT        NOT NULL,
    state            TEXT        NOT NULL CHECK (state IN (
                         'pending','launching','launched','failed')),
    params_hash      TEXT        NOT NULL,
    attempts         INTEGER     NOT NULL DEFAULT 0,
    next_attempt_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    lease_owner      TEXT,
    lease_expires_at TIMESTAMPTZ,
    execution_ref    TEXT,
    last_error       TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The dispatcher's claim query, verbatim: due rows in a claimable state,
-- oldest first. Partial so the index stays small as launched rows accumulate.
CREATE INDEX IF NOT EXISTS run_launches_due_idx
    ON run_launches (next_attempt_at)
    WHERE state IN ('pending', 'failed');

-- The reconciler's query: launches stuck in-flight past their lease.
CREATE INDEX IF NOT EXISTS run_launches_stale_idx
    ON run_launches (lease_expires_at)
    WHERE state = 'launching';


-- Transactional outbox. A terminal run writes its messages here in the SAME
-- transaction that records the terminal state, so a crash between "the work is
-- done" and "the reply was sent" cannot lose the reply.
CREATE TABLE IF NOT EXISTS event_outbox (
    outbox_id         UUID        PRIMARY KEY,
    service_namespace TEXT        NOT NULL,
    message_id        TEXT        NOT NULL,
    run_id            UUID        REFERENCES runs (run_id) ON DELETE CASCADE,
    destination       TEXT        NOT NULL,
    payload_json      JSONB       NOT NULL,
    status            TEXT        NOT NULL CHECK (status IN (
                          'pending','publishing','delivered','failed')),
    attempts          INTEGER     NOT NULL DEFAULT 0,
    next_attempt_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    lease_owner       TEXT,
    lease_expires_at  TIMESTAMPTZ,
    last_error        TEXT,
    delivered_at      TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Stable message identity. Re-projecting a terminal run must reuse its message,
-- not mint a second one, so finalization inserts ON CONFLICT DO NOTHING against
-- this key. It is also the key downstream consumers deduplicate on, which is
-- what makes at-least-once transport honest rather than lossy.
CREATE UNIQUE INDEX IF NOT EXISTS event_outbox_message_uniq
    ON event_outbox (service_namespace, message_id);

CREATE INDEX IF NOT EXISTS event_outbox_due_idx
    ON event_outbox (next_attempt_at)
    WHERE status IN ('pending', 'failed');

CREATE INDEX IF NOT EXISTS event_outbox_stale_idx
    ON event_outbox (lease_expires_at)
    WHERE status = 'publishing';

CREATE INDEX IF NOT EXISTS event_outbox_run_id_idx
    ON event_outbox (run_id) WHERE run_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS event_outbox_delivered_at_idx
    ON event_outbox (service_namespace, delivered_at)
    WHERE delivered_at IS NOT NULL;


-- External-send outcomes, per destination. Separate from event_outbox because
-- they answer a different question: the outbox tracks "did we hand it to our
-- own broker", this tracks "did the PROVIDER accept it".
--
-- ``uncertain`` is the load-bearing status and the reason this table exists. A
-- timeout, a connection lost after the request was written, or a crash during
-- 'sending' leaves an outcome nobody can observe. Blind retry would double-send
-- a WhatsApp message to a real person; blind success would silently drop it.
-- Neither is acceptable, so the state is recorded as what it actually is and
-- raised for reconciliation. This is why no exactly-once claim appears anywhere
-- in this codebase.
CREATE TABLE IF NOT EXISTS delivery_receipts (
    service_namespace TEXT        NOT NULL,
    destination       TEXT        NOT NULL,
    message_id        TEXT        NOT NULL,
    status            TEXT        NOT NULL CHECK (status IN (
                          'pending','sending','delivered','failed','uncertain')),
    attempts          INTEGER     NOT NULL DEFAULT 0,
    next_attempt_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    provider_ref      TEXT,
    lease_owner       TEXT,
    lease_expires_at  TIMESTAMPTZ,
    last_error        TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (service_namespace, destination, message_id)
);

CREATE INDEX IF NOT EXISTS delivery_receipts_due_idx
    ON delivery_receipts (next_attempt_at)
    WHERE status IN ('pending', 'failed');

CREATE INDEX IF NOT EXISTS delivery_receipts_stale_idx
    ON delivery_receipts (lease_expires_at)
    WHERE status = 'sending';

-- The operator's reconciliation worklist. Partial and tiny by construction:
-- if this index is large, something is systematically ambiguous and that is
-- itself the alert.
CREATE INDEX IF NOT EXISTS delivery_receipts_uncertain_idx
    ON delivery_receipts (service_namespace, updated_at)
    WHERE status = 'uncertain';
"""


MIGRATIONS: tuple[Migration, ...] = (
    Migration(version=1, name="run_registry_v1", sql=_V1_DDL),
    Migration(version=2, name="workflow_data_plane_v2", sql=_V2_DDL),
    Migration(version=3, name="durable_messaging_v3", sql=_V3_DDL),
)

#: Highest version this build knows how to apply.
LATEST_VERSION = max(m.version for m in MIGRATIONS)


def get_migration(version: int) -> Migration:
    for migration in MIGRATIONS:
        if migration.version == version:
            return migration
    raise MigrationError(
        f"no migration {version} in this build (known: "
        f"{', '.join(str(m.version) for m in MIGRATIONS)})"
    )


def pending(current: int | None) -> tuple[Migration, ...]:
    """Migrations that still need applying against a live version.

    ``current is None`` means the database was never migrated, so everything is
    pending.
    """
    floor = 0 if current is None else current
    return tuple(m for m in MIGRATIONS if m.version > floor)


# --- the checksum ledger (B1a, ORG-191) --------------------------------------


def ledger_exists(conn: DbConnection) -> bool:
    """Whether the ``schema_migrations`` ledger table exists.

    Total-function probe (``to_regclass``), never EAFP — this is called from
    inside open transactions, including the startup gate, and a raising probe
    aborts its caller's transaction (the v0.1.0 fresh-database bug).
    """
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('schema_migrations') AS oid")
        row = cur.fetchone()
    return row is not None and row["oid"] is not None


def _ledger_rows(conn: DbConnection, namespace: str) -> dict[int, dict[str, str]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT version, name, checksum FROM schema_migrations "
            "WHERE namespace = %s",
            (namespace,),
        )
        rows = cur.fetchall()
    return {
        int(r["version"]): {"name": r["name"], "checksum": r["checksum"]} for r in rows
    }


def verify_ledger(
    conn: DbConnection,
    *,
    namespace: str = CORE_NAMESPACE,
    migrations: tuple[Migration, ...] = MIGRATIONS,
    applied_through: int | None = None,
) -> None:
    """Assert the recorded migration history is consistent with this build.

    Read-only, safe in any transaction state. Raises
    :class:`MigrationChecksumMismatch` when:

    - a recorded migration's checksum differs from this build's definition of
      the same ``(namespace, version)`` — the two-shapes-one-number failure; or
    - a version above :data:`PRE_LEDGER_MAX_VERSION` was applied
      (``applied_through``) with no ledger row — unaccounted history that only a
      foreign, ledger-less build could have produced.

    Tolerated, deliberately: an absent ledger table or absent rows for
    pre-ledger-era versions (``<= PRE_LEDGER_MAX_VERSION``) while nothing newer
    was applied — that is what every healthy pre-B1a database looks like, and
    the next ``wrc migrate`` backfills it. Ledger rows for versions this build
    does not know are skipped: "newer than me" is the range gate's question,
    not the ledger's.

    ``applied_through`` is the highest version known to be applied in this
    namespace — ``schema_meta.version`` for the core chain. App chains pass
    ``None``: the ledger itself is their only record of application.
    """
    known = {m.version: m for m in migrations}

    if not ledger_exists(conn):
        if applied_through is not None and applied_through > PRE_LEDGER_MAX_VERSION:
            raise MigrationChecksumMismatch(
                f"schema is at v{applied_through} (namespace {namespace!r}) but the "
                "schema_migrations ledger does not exist. Versions beyond "
                f"v{PRE_LEDGER_MAX_VERSION} are only ever applied by ledger-aware "
                "builds, so this history is unaccounted — refusing to trust the "
                "version number alone."
            )
        return

    recorded = _ledger_rows(conn, namespace)

    for version, row in sorted(recorded.items()):
        expected = known.get(version)
        if expected is None:
            continue
        if row["checksum"] != expected.checksum:
            raise MigrationChecksumMismatch(
                f"migration v{version} (namespace {namespace!r}) was applied as "
                f"{row['name']!r} sha256={row['checksum'][:12]}…, but this build "
                f"defines it as {expected.name!r} sha256={expected.checksum[:12]}…. "
                "The database was migrated by a DIFFERENT definition of the same "
                "version; serving against it would silently misread the schema. "
                "Deploy the build whose migrations match this history."
            )

    if applied_through is not None:
        # Every applied version beyond the pre-ledger era needs a row — including
        # versions this build does not know, which is precisely the state a
        # ledger-less foreign build leaves behind.
        for version in range(PRE_LEDGER_MAX_VERSION + 1, applied_through + 1):
            if version not in recorded:
                raise MigrationChecksumMismatch(
                    f"schema is at v{applied_through} (namespace {namespace!r}) but "
                    f"the ledger has no row for v{version}. A version beyond "
                    f"v{PRE_LEDGER_MAX_VERSION} can only be applied by a "
                    "ledger-aware build, so this history is unaccounted."
                )


def apply_migrations(
    conn: DbConnection,
    *,
    target: int | None = None,
) -> int:
    """Apply every pending migration up to ``target`` under an advisory lock.

    Returns the resulting schema version. Idempotent: on an already-current
    database nothing is executed and the live version is returned unchanged.

    The advisory lock is transaction-scoped (``pg_advisory_xact_lock``) so it is
    released by COMMIT or ROLLBACK — a migrator killed mid-run cannot leave the
    lock held and wedge every future migrator.
    """
    from .schema import read_schema_version

    ceiling = LATEST_VERSION if target is None else target
    if ceiling > LATEST_VERSION:
        raise MigrationError(
            f"cannot migrate to v{ceiling}: this build only knows up to v{LATEST_VERSION}. "
            "Upgrade workflow-runtime-core before migrating."
        )

    with conn.cursor() as cur:
        # Serialise concurrent migrators. Whoever loses the race blocks here and
        # then observes the winner's committed version, so it applies nothing.
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (MIGRATION_ADVISORY_LOCK_ID,))

        live = read_schema_version(conn)
        if live is not None and live > LATEST_VERSION:
            raise MigrationError(
                f"live schema v{live} is NEWER than this build's v{LATEST_VERSION}. "
                "Refusing to touch it — downgrade migrations are never authored; "
                "deploy a newer workflow-runtime-core instead."
            )

        # Verify recorded history BEFORE creating anything: a divergent past must
        # stop the migrator, not gain a ledger that legitimises it. (On a fresh
        # or pre-ledger database this is the tolerated-absent path.)
        verify_ledger(conn, applied_through=live)

        cur.execute(_LEDGER_DDL)  # pyright: ignore[reportArgumentType, reportCallIssue]

        # Backfill the pre-ledger era: rows for already-applied versions that
        # legitimately predate the ledger. One-time amnesty, bounded by
        # PRE_LEDGER_MAX_VERSION — verify_ledger above already refused anything
        # newer with unaccounted history.
        for m in MIGRATIONS:
            if m.version <= (live or 0) and m.version <= PRE_LEDGER_MAX_VERSION:
                cur.execute(
                    """
                    INSERT INTO schema_migrations (namespace, version, name, checksum)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (namespace, version) DO NOTHING
                    """,
                    (CORE_NAMESPACE, m.version, m.name, m.checksum),
                )

        todo = [m for m in pending(live) if m.version <= ceiling]
        if not todo:
            return live if live is not None else 0

        for migration in todo:
            # No params tuple, deliberately: a migration body is MULTI-STATEMENT,
            # and psycopg only allows that through the simple-query protocol —
            # passing params switches to the extended protocol, which rejects it.
            # pyright resolves the bare-string call to psycopg's t-string
            # `Template` overload; the str form is correct at runtime.
            cur.execute(migration.sql)  # pyright: ignore[reportArgumentType, reportCallIssue]
            cur.execute(
                """
                INSERT INTO schema_migrations (namespace, version, name, checksum)
                VALUES (%s, %s, %s, %s)
                """,
                (CORE_NAMESPACE, migration.version, migration.name, migration.checksum),
            )

        applied = todo[-1].version
        cur.execute(
            """
            INSERT INTO schema_meta (version, singleton)
            VALUES (%s, TRUE)
            ON CONFLICT (singleton) DO UPDATE SET version = EXCLUDED.version,
                                                  applied_at = now()
            """,
            (applied,),
        )
    return applied


# --- app-owned chains ---------------------------------------------------------


def read_app_version(conn: DbConnection, namespace: str) -> int | None:
    """Highest applied version in an app namespace, or ``None`` if none.

    App chains have no ``schema_meta`` row — the ledger IS their record.
    """
    if not ledger_exists(conn):
        return None
    rows = _ledger_rows(conn, namespace)
    return max(rows) if rows else None


def apply_app_migrations(
    conn: DbConnection,
    namespace: str,
    migrations: tuple[Migration, ...],
) -> int:
    """Apply an application-owned additive chain under its own ledger namespace.

    The ownership rule this enforces (2026-08-06 fork resolution): the ``core``
    namespace belongs to this module's chain alone, so an application that owns
    tables of its own — the workflow facade's upload sessions are the motivating
    case — records them here instead of minting a competing ``schema_meta``
    version. Same advisory lock, same checksum verification, same fail-closed
    posture; ``schema_meta`` is never touched.

    App chains are ADDITIVE BY CONTRACT: they create and evolve the
    application's own objects and must never alter a core-owned table. That
    contract is documentation-and-review enforced — SQL cannot cheaply prove it —
    which is exactly why the chain is named in the ledger: a violation has an
    attributable author.
    """
    if namespace == CORE_NAMESPACE or not _APP_NAMESPACE_RE.match(namespace):
        raise MigrationError(
            f"invalid app namespace {namespace!r}: must match "
            f"{_APP_NAMESPACE_RE.pattern} and must not be {CORE_NAMESPACE!r}"
        )
    versions = [m.version for m in migrations]
    if not versions or versions != list(range(1, len(versions) + 1)):
        raise MigrationError(
            f"app chain {namespace!r} versions must be contiguous from 1, got "
            f"{versions} — a gap makes the pending calculation silently skip a step"
        )

    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (MIGRATION_ADVISORY_LOCK_ID,))

        verify_ledger(conn, namespace=namespace, migrations=migrations)

        cur.execute(_LEDGER_DDL)  # pyright: ignore[reportArgumentType, reportCallIssue]

        recorded = _ledger_rows(conn, namespace)
        floor = max(recorded) if recorded else 0
        todo = [m for m in migrations if m.version > floor]
        if not todo:
            return floor

        for migration in todo:
            cur.execute(migration.sql)  # pyright: ignore[reportArgumentType, reportCallIssue]
            cur.execute(
                """
                INSERT INTO schema_migrations (namespace, version, name, checksum)
                VALUES (%s, %s, %s, %s)
                """,
                (namespace, migration.version, migration.name, migration.checksum),
            )

    return todo[-1].version
