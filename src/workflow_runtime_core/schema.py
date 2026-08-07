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
#: Accepts [1, 3]: the shared production registry stays at its own version
#: during the expansion window while ephemeral/pilot databases run ahead.
#: Functions that need a v2 column raise a NAMED error on v1 instead of a
#: KeyError in a worker; the v3 messaging surfaces do the same on v1/v2.
#:
#: The range only ever widens at the top. A deployment stops at the version it
#: needs — the hosted plane at v2, a durable client service (ORG-PLAN-155
#: Phase C) at v3 — and a reader compiled for v3 serves all three, which is the
#: property that lets readers roll out ahead of any migration.
SUPPORTED_SCHEMA_MIN = 1
SUPPORTED_SCHEMA_MAX = 3

#: The version a fresh ``wrc migrate`` produces. Kept distinct from the MAX
#: above: they diverge during an expand/migrate/contract window, where a build
#: can READ a version it does not yet write by default.
SCHEMA_VERSION = 3


def read_schema_version(conn: DbConnection) -> int | None:
    """Live schema version, or ``None`` if the registry was never migrated.

    An absent ``schema_meta`` table is a legitimate answer ("never migrated"),
    not an error — it is exactly what a freshly provisioned database looks like,
    and the caller decides whether that is fatal.

    **Do not ask this question by catching an exception.** PostgreSQL aborts the
    *entire* surrounding transaction on any failed statement, so a plain
    ``SELECT ... EXCEPT UndefinedTable`` returns the right answer while leaving
    the connection in ``InFailedSqlTransaction`` — every later statement then
    fails with an error that names neither this function nor the real cause.
    ``apply_migrations`` calls this while holding its advisory lock inside an
    open transaction, so that idiom broke migrating a *fresh* database: the one
    case every new deployment hits and no existing one ever does.

    ``to_regclass`` is the structural fix. It resolves a name to an OID or NULL
    and **never raises**, so the probe is safe in any transaction state instead
    of being safe only while someone remembers to wrap it. The savepoint below
    is then belt-and-braces for the narrow race where the table is dropped
    between the two statements.

    The same reasoning applies to any "does this object exist?" question against
    PostgreSQL: prefer a total function (``to_regclass``, ``information_schema``,
    ``pg_catalog``) over EAFP. See ``tests/integration/test_migrate_fresh_database.py``.
    """
    import psycopg

    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('schema_meta') AS oid")
        probe = cur.fetchone()
    if probe is None or probe["oid"] is None:
        return None

    try:
        with conn.transaction(), conn.cursor() as cur:
            cur.execute("SELECT version FROM schema_meta LIMIT 1")
            row = cur.fetchone()
    except psycopg.errors.UndefinedTable:  # pragma: no cover - concurrent DROP
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

    Beyond the version-range check, this verifies the ``schema_migrations``
    checksum ledger (B1a, ORG-191): the range check reads a *number*, and a
    number cannot distinguish two different shapes that both claim it. A
    :class:`~workflow_runtime_core.exceptions.MigrationChecksumMismatch` here
    means the database was migrated by a different definition of some version
    than the one this build carries — fail the health check; do not serve.
    """
    from .migrations import verify_ledger

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
    verify_ledger(conn, applied_through=live)
    return live


#: Backwards-compatible alias for the extracted Stromy call site. Stromy's
#: ``registry.require_schema_version`` had exactly this signature and behaviour.
require_schema_version = require_compatible_schema
