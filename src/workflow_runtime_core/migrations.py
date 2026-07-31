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

Phase A ships migration ``0001`` only, which is byte/behaviour compatible with
the live Stromy schema v1 — applying it to an already-migrated v1 database is a
no-op. The additive ``0002`` (inbox / launch / outbox / receipts / leases) lands
in Phase B together with the ``schema_migrations`` checksum ledger.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .exceptions import MigrationError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .registry import DbConnection

# A fixed, transaction-scoped advisory lock id. Every migrator competes for this
# one key, so concurrent migrators serialise instead of racing the same DDL.
# The value is arbitrary but must never change: it IS the mutual-exclusion
# identity shared across processes and releases.
MIGRATION_ADVISORY_LOCK_ID = 0x57524301  # "WRC\x01"


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

        Phase B records this in ``schema_migrations`` and fails closed on a
        mismatch. Exposing it now means the value a future ledger compares
        against is computed by the same function that will verify it.
        """
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()


MIGRATIONS: tuple[Migration, ...] = (
    Migration(version=1, name="run_registry_v1", sql=_V1_DDL),
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
