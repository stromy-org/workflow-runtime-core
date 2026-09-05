"""Durable checkpointer for hosted runs.

Why this exists at all: a workflow that calls ``interrupt()`` for human review
must survive its own process exiting, because job-per-run means there IS no
process left holding memory. Cross-process resume keyed by ``thread_id`` is the
entire justification for a durable store — nothing else here needs one.

Non-pausing runs use the identical path and simply never pause. That is
deliberate: HITL and non-HITL runs must not require different infrastructure.

Async ``AsyncPostgresSaver`` is the runtime path
------------------------------------------------
Real workflow nodes are ``async def``, so LangGraph runs them through its ASYNC
Pregel loop, which calls the checkpointer's *async* methods (``aget_tuple`` /
``aput`` / ``aput_writes``). The sync ``PostgresSaver`` does NOT implement those
— they inherit ``BaseCheckpointSaver``'s defaults, which ``raise
NotImplementedError`` — so a sync saver bound to an async graph fails the moment
the loop reads the first checkpoint. The runtime therefore uses
:func:`acheckpointer`.

The historical worry about ``AsyncPostgresSaver`` was its instance-level
``threading.Lock()`` serialising concurrent asyncio tasks against the pool
(langgraph#7259) — a THROUGHPUT defect on a concurrent multi-run server, never a
correctness one. Job-per-run has no such concurrency (one graph, one process,
one run), so that lock is uncontended and free here. The sync :func:`checkpointer`
is retained only as a utility for a hypothetical sync-node graph.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import AsyncGenerator, Generator
from contextlib import asynccontextmanager, contextmanager
from typing import TYPE_CHECKING, Any, cast

from ..exceptions import CheckpointerError, CheckpointStoreOutdated, RegistryError
from ..registry import dsn_from_env

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from langgraph.checkpoint.postgres import PostgresSaver
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

# Checkpoint payloads are deserialised from the store. Without strict msgpack, a
# crafted ext-type payload is an RCE vector (GHSA-g48c-2wqr-h844). We assert
# rather than merely default it, so a misconfigured runner fails at boot instead
# of running unprotected.
_STRICT_MSGPACK_ENV = "LANGGRAPH_STRICT_MSGPACK"

# LangGraph writes checkpoints at each step. "sync" durability means the write is
# committed BEFORE the node's result is acted on — so a process killed mid-step
# can never lose a step the graph believed it had taken.
DURABILITY = "sync"

# ``setup()`` is documented as idempotent, and it is — SEQUENTIALLY. Run two of
# them concurrently against a database with no checkpoint tables and both see
# "not created", both issue the CREATE, and the loser dies on `duplicate key
# value violates unique constraint "pg_type_typname_nsp_index"`.
#
# Job-per-run starts N runners in parallel by design, so a NEW install's first
# burst of traffic hits this — and only a new install, which is why it survives
# every test against a database something already migrated. Observed 2026-08-07
# on the GMF pilot's first end-to-end run: two messages in, one run completed,
# one died here.
#
# **Why not an advisory lock**, which is how this module's sibling (the registry
# migrator) serialises its DDL: LangGraph's setup issues `CREATE INDEX
# CONCURRENTLY`, and CIC waits for every concurrent transaction to finish before
# it completes. A runner queued on the advisory lock is itself inside a
# transaction, so the holder waits for the waiters and the waiters wait for the
# holder — a standstill Postgres does not even report as a deadlock, because
# CIC's wait is not in the lock graph it checks. Measured, twice: locking on the
# saver's own connection produced `deadlock detected`, and locking on a
# dedicated connection hung indefinitely with one session in CIC and seven in
# `Lock: advisory`.
#
# So the race is TOLERATED rather than prevented. The losing side's error is
# always "the thing I was creating already exists", and re-running setup against
# that state is a no-op, so a bounded retry converges — and nothing ever blocks,
# which is what keeps CIC able to finish.
SETUP_MAX_ATTEMPTS = 6

#: Base backoff between setup retries. Small: the winner only has to finish its
#: DDL, not do real work.
SETUP_RETRY_BASE_SECONDS = 0.25


def enforce_strict_msgpack() -> None:
    """Set + verify strict msgpack. Call before any checkpoint deserialisation.

    Idempotent. Raises if something explicitly disabled it — that is a
    misconfiguration we must not paper over, because the failure mode is silent
    (it only bites on a malicious payload).
    """
    current = os.environ.get(_STRICT_MSGPACK_ENV)
    if current is None:
        os.environ[_STRICT_MSGPACK_ENV] = "true"
    elif current.strip().lower() not in {"true", "1", "yes"}:
        raise CheckpointerError(
            f"{_STRICT_MSGPACK_ENV}={current!r} disables strict msgpack decoding. "
            "The checkpoint store is an untrusted deserialisation boundary "
            "(GHSA-g48c-2wqr-h844); refusing to start."
        )
    _force_strict_flag_on_loaded_module()


def _force_strict_flag_on_loaded_module() -> None:
    """Force strict msgpack ON in langgraph's already-imported serde module.

    langgraph reads ``LANGGRAPH_STRICT_MSGPACK`` **once at module-import time**
    and freezes it in ``langgraph.checkpoint.serde._msgpack.STRICT_MSGPACK_ENABLED``.
    The runner resolves (imports) the graph BEFORE the checkpointer opens, so by
    the time :func:`enforce_strict_msgpack` sets the env var that module is
    already imported with the frozen pre-enforcement value — setting the env var
    is then a silent no-op and deserialisation runs UNSAFE while the code
    believes it enforced the guard (GHSA-g48c-2wqr-h844). Force the frozen flag
    on the loaded module so enforcement holds regardless of import order. If the
    module is not yet imported, the env var set above freezes it True on first
    import. Guarded so a langgraph internals change degrades to the env-var path
    rather than crashing.
    """
    mod = sys.modules.get("langgraph.checkpoint.serde._msgpack")
    if mod is not None and getattr(mod, "STRICT_MSGPACK_ENABLED", None) is not True:
        setattr(mod, "STRICT_MSGPACK_ENABLED", True)  # noqa: B010 - module attr, not a known field


def _is_concurrent_setup_race(exc: BaseException) -> bool:
    """Is this "someone else created it first", rather than a real failure?

    Matched on SQLSTATE, not message text: the messages are localised by the
    server's lc_messages, so a text match silently stops working on a
    non-English database and turns a survivable race back into a dead run.

    * 23505 unique_violation — the duplicate ``pg_type`` row, the observed one.
    * 42P07 duplicate_table / 42710 duplicate_object — the same race caught at a
      different point in the DDL.
    * 40P01 deadlock_detected — two setups interleaving catalog work.
    """
    import psycopg

    if not isinstance(exc, psycopg.Error):
        return False
    return getattr(exc, "sqlstate", None) in {"23505", "42P07", "42710", "40P01"}


def _setup_retry_delay(attempt: int) -> float:
    """Backoff with jitter, so N racing runners do not retry in lockstep."""
    import random

    return SETUP_RETRY_BASE_SECONDS * (2 ** (attempt - 1)) * (0.5 + random.random())  # noqa: S311 - jitter, not cryptography


def _setup_tolerating_races(saver: PostgresSaver) -> None:
    """Run ``setup()``, retrying only the concurrent-first-use race."""
    import time

    for attempt in range(1, SETUP_MAX_ATTEMPTS + 1):
        try:
            saver.setup()
        except Exception as exc:
            if attempt == SETUP_MAX_ATTEMPTS or not _is_concurrent_setup_race(exc):
                raise
            time.sleep(_setup_retry_delay(attempt))
        else:
            return


async def _asetup_tolerating_races(saver: AsyncPostgresSaver) -> None:
    """Async twin of :func:`_setup_tolerating_races`."""
    for attempt in range(1, SETUP_MAX_ATTEMPTS + 1):
        try:
            await saver.setup()
        except Exception as exc:
            if attempt == SETUP_MAX_ATTEMPTS or not _is_concurrent_setup_race(exc):
                raise
            await asyncio.sleep(_setup_retry_delay(attempt))
        else:
            return


def _missing_postgres_saver() -> CheckpointerError:
    return CheckpointerError(
        "langgraph-checkpoint-postgres is not installed; the hosted runtime "
        "cannot run without a durable checkpointer. Install with: "
        "uv pip install 'workflow-runtime-core[executor]'"
    )


# ── setup() is a MIGRATION, and a DML-only runtime must not call it ──────────
#
# ``saver.setup()`` runs schema SQL. It is written to be idempotent, and it is —
# but idempotent is not the same as harmless: it still issues DDL, so a runtime
# holding only DML dies at startup with a privilege error rather than the
# actionable "your deployment skipped a step" this raises instead. Worse, if the
# runtime DID hold the privilege, one replica could migrate the checkpoint store
# while its siblings are mid-read of it.
#
# So the store's schema becomes a deployment step (`wrc checkpoint-setup`, run by
# the migration operator) and the runtime only VERIFIES. `run` remains the
# default, because every consumer before 0.8.0 connects as the object owner and
# nothing about their deployment changed.

#: The tables LangGraph's saver creates, and the ledger it records its own
#: schema version in. Read-only here; the authoritative list for GRANT purposes
#: is :data:`workflow_runtime_core.grants.CHECKPOINT_MANIFEST`.
_CHECKPOINT_LEDGER = "checkpoint_migrations"


def expected_checkpoint_version(saver: object) -> int | None:
    """Highest ``checkpoint_migrations.v`` a fully-migrated store reaches.

    Read off the saver's own ``MIGRATIONS`` list rather than pinned here: that
    list IS the schema definition, so a hardcoded expectation would need editing
    on every langgraph upgrade and would be silently wrong until someone did.
    The recorded version is the last INDEX, hence ``len - 1``.
    """
    migrations = getattr(saver, "MIGRATIONS", None)
    return len(migrations) - 1 if migrations else None


def _interpret_checkpoint_state(exists: bool, live: int | None, expected: int | None) -> None:
    """Raise :class:`CheckpointStoreOutdated` unless the store is current.

    Split out from the two I/O paths so the sync and async verifiers cannot
    drift into disagreeing about what "current" means.
    """
    if not exists:
        raise CheckpointStoreOutdated(
            f"the checkpoint store has never been created (no {_CHECKPOINT_LEDGER} table)."
        )
    if expected is not None and (live is None or live < expected):
        raise CheckpointStoreOutdated(
            f"the checkpoint store is at v{live} but this langgraph build expects v{expected}."
        )


def _first_value(row: object) -> object:
    """First column of a row from either a tuple or a dict row factory."""
    if row is None:
        return None
    if isinstance(row, dict):
        return next(iter(cast("dict[str, object]", row).values()), None)
    return cast("Sequence[object]", row)[0]


def _verify_checkpoint_store(saver: object) -> None:
    """Assert the checkpoint store exists and is current. Never migrates."""
    conn: Any = cast("Any", saver).conn
    cur: Any
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s)", (f"public.{_CHECKPOINT_LEDGER}",))
        exists = _first_value(cur.fetchone()) is not None
        live: int | None = None
        if exists:
            cur.execute(f"SELECT max(v) FROM {_CHECKPOINT_LEDGER}")  # noqa: S608 - module constant
            value = _first_value(cur.fetchone())
            live = int(value) if isinstance(value, int) else None
    _interpret_checkpoint_state(exists, live, expected_checkpoint_version(saver))


async def _averify_checkpoint_store(saver: object) -> None:
    """Async twin of :func:`_verify_checkpoint_store`."""
    conn: Any = cast("Any", saver).conn
    cur: Any
    async with conn.cursor() as cur:
        await cur.execute("SELECT to_regclass(%s)", (f"public.{_CHECKPOINT_LEDGER}",))
        exists = _first_value(await cur.fetchone()) is not None
        live: int | None = None
        if exists:
            await cur.execute(f"SELECT max(v) FROM {_CHECKPOINT_LEDGER}")  # noqa: S608 - module constant
            value = _first_value(await cur.fetchone())
            live = int(value) if isinstance(value, int) else None
    _interpret_checkpoint_state(exists, live, expected_checkpoint_version(saver))


# ── Opening the saver's connection ──────────────────────────────────────────
#
# LangGraph's own ``from_conn_string`` hardcodes ``psycopg.Connection`` /
# ``AsyncConnection``, so under Entra auth it would open a password connection
# with no password and fail. These wrappers reproduce its connection options
# EXACTLY — autocommit, prepare_threshold=0, dict_row, all three load-bearing to
# the saver — and vary only the password, which under Entra is a freshly
# acquired access token.
#
# The options are copied deliberately rather than referenced, because langgraph
# does not export them; the integration test that opens a real saver through
# this path is what would catch an upstream change to any of the three.
_SAVER_CONNECT_KWARGS = {"autocommit": True, "prepare_threshold": 0}


@contextmanager
def _saver_session(saver_cls: object, dsn: str) -> Generator[PostgresSaver]:
    """Open a sync saver on a connection built for the configured auth mode."""
    from psycopg.rows import dict_row

    from ..auth import connection_class, connection_kwargs, resolve_auth_mode

    mode = resolve_auth_mode()
    cls = connection_class(mode, is_async=False)
    with cls.connect(
        dsn, row_factory=dict_row, **_SAVER_CONNECT_KWARGS, **connection_kwargs(mode, dsn)
    ) as conn:
        yield saver_cls(conn)  # type: ignore[operator]


@asynccontextmanager
async def _asaver_session(saver_cls: object, dsn: str) -> AsyncGenerator[AsyncPostgresSaver]:
    """Open an async saver on a connection built for the configured auth mode."""
    from psycopg.rows import dict_row

    from ..auth import connection_class, connection_kwargs_async, resolve_auth_mode

    mode = resolve_auth_mode()
    cls = connection_class(mode, is_async=True)
    async with await cls.connect(
        dsn, row_factory=dict_row, **_SAVER_CONNECT_KWARGS, **await connection_kwargs_async(mode, dsn)
    ) as conn:
        yield saver_cls(conn=conn)  # type: ignore[operator]


@contextmanager
def checkpointer(dsn: str | None = None, *, setup: str | None = None) -> Generator[PostgresSaver]:
    """Yield a ``PostgresSaver`` bound to one shared connection.

    Job-per-run means one process, one run — so one connection is the right
    shape, and it keeps the per-run connection cost at exactly 1 (the constraint
    that decides when the shared Postgres needs resizing).

    ``setup`` resolves explicit argument -> ``WRC_CHECKPOINT_SETUP`` -> ``run``.
    ``run`` calls the saver's ``setup()`` (schema SQL, tolerating the
    first-use race below); ``verify`` only asserts the store is present and
    current, and raises :class:`CheckpointStoreOutdated` if it is not. The
    default keeps every pre-0.8.0 caller byte-identical; a DML-only runtime opts
    into ``verify``.

    The ``opened`` flag is load-bearing, not defensive (see
    :func:`acheckpointer` for the failure it prevents).
    """
    from ..auth import CheckpointSetupMode, resolve_checkpoint_setup

    enforce_strict_msgpack()
    mode = resolve_checkpoint_setup(setup)

    try:
        from langgraph.checkpoint.postgres import PostgresSaver
    except ImportError as exc:  # pragma: no cover - dependency wiring
        raise _missing_postgres_saver() from exc

    resolved = dsn or dsn_from_env()
    opened = False
    try:
        with _saver_session(PostgresSaver, resolved) as saver:
            if mode is CheckpointSetupMode.RUN:
                _setup_tolerating_races(saver)
            else:
                _verify_checkpoint_store(saver)
            opened = True
            yield saver
    except (CheckpointerError, RegistryError):
        raise
    except Exception as exc:  # noqa: BLE001 - surface any wiring failure loudly
        if opened:
            raise
        raise CheckpointerError(f"cannot open the checkpoint store: {exc}") from exc


@asynccontextmanager
async def acheckpointer(
    dsn: str | None = None, *, setup: str | None = None
) -> AsyncGenerator[AsyncPostgresSaver]:
    """Yield an ``AsyncPostgresSaver`` bound to one shared connection.

    The runtime path (see module docstring): hosted graphs are async, so the
    runner drives them via ``astream``, whose async Pregel loop requires a saver
    with async ``aget_tuple`` / ``aput`` methods. One connection per run keeps the
    per-run connection cost at exactly 1.

    **Why the ``opened`` flag.** A generator-based context manager receives the
    caller's body exception *at the yield*, so a blanket ``except Exception``
    around the ``async with`` catches failures that have nothing to do with
    opening anything. Without the flag, every graph error and every stage error
    inside this block was relabelled "cannot open the checkpoint store: …" —
    losing the exception's type along with the truth. That mattered most for
    :class:`~workflow_runtime_core.exceptions.LeaseLost`: rewritten as a
    ``CheckpointerError``, a lost lease looked like a run failure, and the runner
    would have written a terminal status over the outcome of whichever runner
    actually held the lease. Only failures raised *before* the first successful
    yield are store-open failures.

    ``setup`` behaves exactly as in :func:`checkpointer`. This is the path the
    hosted runtime takes, so it is the one that runs ``verify``.
    """
    from ..auth import CheckpointSetupMode, resolve_checkpoint_setup

    enforce_strict_msgpack()
    mode = resolve_checkpoint_setup(setup)

    try:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    except ImportError as exc:  # pragma: no cover - dependency wiring
        raise _missing_postgres_saver() from exc

    resolved = dsn or dsn_from_env()
    opened = False
    try:
        async with _asaver_session(AsyncPostgresSaver, resolved) as saver:
            if mode is CheckpointSetupMode.RUN:
                await _asetup_tolerating_races(saver)
            else:
                await _averify_checkpoint_store(saver)
            opened = True
            yield saver
    except (CheckpointerError, RegistryError):
        raise
    except Exception as exc:  # noqa: BLE001 - surface any wiring failure loudly
        if opened:
            raise
        raise CheckpointerError(f"cannot open the checkpoint store: {exc}") from exc


def bind_checkpointer(graph: object, saver: object) -> object:
    """Return a copy of ``graph`` bound to ``saver``.

    NOT ``graph.with_config(checkpointer=saver)``. That call is accepted, returns
    a CompiledStateGraph, and leaves ``.checkpointer`` as None — it stuffs the
    kwarg into RunnableConfig, which the Pregel loop never reads for the saver.
    The result is a graph that runs happily with NO durability: non-pausing runs
    still pass, and only a resume reveals the state was never written. Verified
    against langgraph 1.x; a consumer-side test pins the behaviour so an upgrade
    cannot silently reintroduce it.

    Graphs exported from ``langgraph.json`` are already compiled and carry no
    checkpointer (the platform is expected to attach one), so we copy rather than
    mutate the module-level singleton — a job-per-run process still shouldn't
    leave a global mutated for anything else importing it.
    """
    import copy

    bound = copy.copy(graph)
    bound.checkpointer = saver  # type: ignore[attr-defined]
    if getattr(bound, "checkpointer", None) is not saver:  # pragma: no cover
        raise CheckpointerError(
            "could not attach the checkpointer to the compiled graph; refusing to "
            "run without durability (a run with no checkpoint cannot be resumed)"
        )
    return bound
