"""Schema-version inspection and the read-only compatibility gate.

This module is what APPLICATIONS call. It never issues DDL — see
:mod:`workflow_runtime_core.migrations` for why that separation is load-bearing.

The supported range is a range, not a number, on purpose. Rolling out schema v2
means: first deploy readers that accept ``[1, 2]``, then migrate, then deploy
writers. A consumer that only ever accepted its own exact version could never be
deployed ahead of the migration, which is the ordering the expand/migrate/
contract sequence requires.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .exceptions import SchemaVersionMismatch

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .registry import DbConnection

#: Inclusive range of live schema versions this build can read and write.
#: Phase A is v1-only; Phase B widens the ceiling to 2.
SUPPORTED_SCHEMA_MIN = 1
SUPPORTED_SCHEMA_MAX = 1

#: The version a fresh ``wrc migrate`` produces. Kept distinct from the MAX
#: above: they diverge during an expand/migrate/contract window, where a build
#: can READ a version it does not yet write by default.
SCHEMA_VERSION = 1


def read_schema_version(conn: DbConnection) -> int | None:
    """Live schema version, or ``None`` if the registry was never migrated.

    An absent ``schema_meta`` table is a legitimate answer ("never migrated"),
    not an error — it is exactly what a freshly provisioned database looks like,
    and the caller decides whether that is fatal.

    The probe runs inside a SAVEPOINT because that "legitimate answer" arrives as
    a Postgres error: selecting from a missing table aborts the *entire*
    surrounding transaction, so merely catching ``UndefinedTable`` would leave
    the connection in ``InFailedSqlTransaction`` and every later statement would
    fail. ``apply_migrations`` calls this while holding its advisory lock in an
    open transaction, so a bare catch here breaks migrating a fresh database —
    the single most common case.
    """
    import psycopg

    try:
        with conn.transaction(), conn.cursor() as cur:
            cur.execute("SELECT version FROM schema_meta LIMIT 1")
            row = cur.fetchone()
    except psycopg.errors.UndefinedTable:
        # Savepoint rolled back; the outer transaction is still usable.
        return None
    if not row:
        return None
    return int(row["version"])


def require_compatible_schema(
    conn: DbConnection,
    *,
    minimum: int = SUPPORTED_SCHEMA_MIN,
    maximum: int = SUPPORTED_SCHEMA_MAX,
) -> int:
    """Assert the live schema is servable, or raise loudly.

    This is the ONLY schema call an application makes at startup. It reads; it
    never creates. A process that finds an unservable schema must fail its health
    check rather than serve, because "deploys trail registry migrations" is only
    a rule if something enforces it.
    """
    live = read_schema_version(conn)
    if live is None:
        raise SchemaVersionMismatch(
            "run registry has no schema_meta row — it was never migrated. "
            "Run `wrc migrate` with the migration identity before starting an "
            "application against it. Applications never migrate themselves."
        )
    if not (minimum <= live <= maximum):
        raise SchemaVersionMismatch(
            f"run registry schema v{live} is outside the supported range "
            f"[v{minimum}, v{maximum}]. Migrations must land BEFORE the readers "
            "that depend on them; deploy order is migrate-then-deploy."
        )
    return live


#: Backwards-compatible alias for the extracted Stromy call site. Stromy's
#: ``registry.require_schema_version`` had exactly this signature and behaviour.
require_schema_version = require_compatible_schema
