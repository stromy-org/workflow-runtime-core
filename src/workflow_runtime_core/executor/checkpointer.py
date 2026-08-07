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

import os
import sys
from collections.abc import AsyncGenerator, Generator
from contextlib import asynccontextmanager, contextmanager
from typing import TYPE_CHECKING

from ..exceptions import CheckpointerError, RegistryError
from ..registry import dsn_from_env

if TYPE_CHECKING:  # pragma: no cover - typing only
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


def _missing_postgres_saver() -> CheckpointerError:
    return CheckpointerError(
        "langgraph-checkpoint-postgres is not installed; the hosted runtime "
        "cannot run without a durable checkpointer. Install with: "
        "uv pip install 'workflow-runtime-core[executor]'"
    )


@contextmanager
def checkpointer(dsn: str | None = None) -> Generator[PostgresSaver]:
    """Yield a set-up ``PostgresSaver`` bound to one shared connection.

    Job-per-run means one process, one run — so one connection is the right
    shape, and it keeps the per-run connection cost at exactly 1 (the constraint
    that decides when the shared Postgres needs resizing).

    The ``opened`` flag is load-bearing, not defensive (see
    :func:`acheckpointer` for the failure it prevents).
    """
    enforce_strict_msgpack()

    try:
        from langgraph.checkpoint.postgres import PostgresSaver
    except ImportError as exc:  # pragma: no cover - dependency wiring
        raise _missing_postgres_saver() from exc

    resolved = dsn or dsn_from_env()
    opened = False
    try:
        with PostgresSaver.from_conn_string(resolved) as saver:
            # Idempotent; creates the checkpoint tables on first use.
            saver.setup()
            opened = True
            yield saver
    except (CheckpointerError, RegistryError):
        raise
    except Exception as exc:  # noqa: BLE001 - surface any wiring failure loudly
        if opened:
            raise
        raise CheckpointerError(f"cannot open the checkpoint store: {exc}") from exc


@asynccontextmanager
async def acheckpointer(dsn: str | None = None) -> AsyncGenerator[AsyncPostgresSaver]:
    """Yield a set-up ``AsyncPostgresSaver`` bound to one shared connection.

    The runtime path (see module docstring): hosted graphs are async, so the
    runner runs them via ``ainvoke``, whose async Pregel loop requires a saver
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
    """
    enforce_strict_msgpack()

    try:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    except ImportError as exc:  # pragma: no cover - dependency wiring
        raise _missing_postgres_saver() from exc

    resolved = dsn or dsn_from_env()
    opened = False
    try:
        async with AsyncPostgresSaver.from_conn_string(resolved) as saver:
            # Idempotent; creates the checkpoint tables on first use.
            await saver.setup()
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
