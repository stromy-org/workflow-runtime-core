"""Core run executor — claim one run, execute its graph, record the outcome.

One process, one run. The runner claims a queued run, executes its graph against
the durable checkpointer, and records the outcome:

* graph reaches END          -> ``completed`` (+ the binding's artifact projection)
* graph hits ``interrupt()`` -> ``paused``, and **the process exits**
* anything raises            -> ``failed`` (+ error)

The exit-at-interrupt is the point of the whole design: a paused run holds no
compute, so human-in-the-loop costs nothing while it waits. A resume is just a
new execution of this same entrypoint against the same ``thread_id``.

The run's configuration comes from the REGISTRY, never from argv/env beyond the
run id. That is load-bearing security, not tidiness: starting a hosted job grants
the caller access to every secret configured on it, so caller-controlled values
must never reach the job template. The run id is an opaque UUID minted by us.

Phase A parity note
-------------------
This is the extracted Stromy worker with its graph/input/context/projection
decisions moved behind :class:`~workflow_runtime_core.binding.ExecutionBinding`.
Lease ownership, stale-claim recovery and terminal-snapshot finalisation without
replay arrive with schema v2 in Phase B; until then a claim is exactly the
single-flight ``queued -> running`` transition it always was.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, cast

from .. import registry, schema
from ..exceptions import RegistryError
from ..models import TERMINAL_STATUSES, RunRecord, RunStatus, TerminalProjection
from .checkpointer import DURABILITY, acheckpointer, bind_checkpointer

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..binding import ExecutionBinding

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_CLAIM_LOST = 0  # not an error: another runner legitimately won the race
EXIT_FAILED = 1
EXIT_USAGE = 2


def extract_interrupt(state: Any) -> Any | None:
    """Return the interrupt payload if the graph paused, else ``None``.

    LangGraph surfaces interrupts under ``__interrupt__`` in the emitted state.
    Shape varies by version (a tuple of Interrupt objects, or plain dicts), so we
    normalise rather than assume.
    """
    if not isinstance(state, dict):
        return None
    emitted = cast("dict[str, Any]", state)
    raw: Any = emitted.get("__interrupt__")
    if not raw:
        return None
    items: list[Any] = list(cast("list[Any] | tuple[Any, ...]", raw)) if isinstance(raw, (list, tuple)) else [raw]
    payloads: list[Any] = []
    for item in items:
        value = getattr(item, "value", None)
        payloads.append(value if value is not None else item)
    if not payloads:
        return None
    return payloads[0] if len(payloads) == 1 else payloads


def run_once(
    run_id: str,
    binding: ExecutionBinding,
    *,
    dsn: str | None = None,
) -> int:
    """Claim and execute one run. Returns a process exit code."""
    with registry.connect(dsn) as conn:
        # Refuse to run against a schema we do not understand, rather than
        # writing rows a future/past reader will misread. Read-only: the runner
        # never migrates (ORG-PLAN-155 locked decision 5).
        schema.require_compatible_schema(conn)

        run = registry.get_run(conn, run_id)
        if run is None:
            logger.error("run %s not found in registry", run_id)
            return EXIT_FAILED
        if run.status in TERMINAL_STATUSES:
            logger.info("run %s is already %s; nothing to do", run_id, run.status)
            return EXIT_OK

        claimed = registry.claim_run(conn, run_id)
        if claimed is None:
            # The loser of a single-flight race exits cleanly and silently. It
            # must NOT execute — two runners on one thread_id would interleave
            # checkpoint writes.
            logger.info("run %s claim lost or not startable; exiting", run_id)
            return EXIT_CLAIM_LOST

    # Registry connection released before the (potentially hours-long) graph run:
    # job-per-run should hold exactly ONE connection — the checkpointer's — for
    # the duration, since concurrent connections are the shared-Postgres limit.
    return execute(claimed, binding, dsn=dsn)


class _StageError(Exception):
    """Carries the message prefix the failing stage records in the registry.

    The extracted worker distinguished "graph resolution failed: …" from a bare
    graph error, and consumer tests assert on that text. Keeping the prefix with
    the stage — rather than inferring it from where an exception surfaced — means
    the whole run still executes inside ONE event loop, which a binding holding
    async resources requires.
    """

    def __init__(self, prefix: str, cause: BaseException) -> None:
        super().__init__(f"{prefix}{cause}" if prefix else str(cause))


def execute(
    run: RunRecord,
    binding: ExecutionBinding,
    *,
    dsn: str | None = None,
) -> int:
    """Execute an already-claimed run to a terminal or paused state."""
    config = dict(run.config_json)
    config.pop(registry.RESUME_KEY, None)

    invoke_config = {"configurable": {"thread_id": run.thread_id, **config}}

    async def _drive() -> tuple[str, Any]:
        try:
            graph = await binding.resolve_graph(run.workflow)
        except Exception as exc:
            raise _StageError("graph resolution failed: ", exc) from exc

        payload = await binding.build_input(run)

        # Hosted graphs are async (every real workflow node is ``async def``), so
        # they run through LangGraph's ASYNC Pregel loop: ``ainvoke`` against an
        # async-capable saver. A sync ``.invoke()`` raises "No synchronous
        # function provided" on the first async node, and a sync ``PostgresSaver``
        # has no async methods for the async loop to call. ``ainvoke`` is a strict
        # superset — it also runs any sync-node graph.
        async with acheckpointer(dsn) as saver:
            compiled = bind_checkpointer(graph, saver)
            context = await binding.build_context(run)
            final_state = await compiled.ainvoke(  # type: ignore[attr-defined]
                payload,
                config=invoke_config,
                context=context,
                durability=DURABILITY,
            )

            interrupt_payload = extract_interrupt(final_state)
            if interrupt_payload is not None:
                return ("paused", interrupt_payload)

            # Read the durable snapshot back rather than projecting from the
            # emitted state. It is the same data for a completed run, but it is a
            # real ``StateSnapshot`` — the type the binding protocol promises, and
            # the object Phase B's "finalize a terminal checkpoint without
            # replaying the graph" recovery path hands to this same method.
            snapshot = await compiled.aget_state(invoke_config)  # type: ignore[attr-defined]
            try:
                projection = await binding.project_terminal(run, snapshot)
            except Exception as exc:
                raise _StageError("terminal projection failed: ", exc) from exc
            return ("terminal", projection)

    try:
        outcome, value = asyncio.run(_drive())
    except Exception as exc:  # noqa: BLE001 - any graph error is a run failure
        logger.exception("run %s failed", run.run_id)
        with registry.connect(dsn) as conn:
            registry.mark_failed(conn, run.run_id, str(exc))
        return EXIT_FAILED

    if outcome == "paused":
        with registry.connect(dsn) as conn:
            registry.mark_paused(conn, run.run_id, value)
        logger.info("run %s paused at interrupt; exiting (state is durable)", run.run_id)
        return EXIT_OK

    return _finalize(run, value, dsn=dsn)


def _finalize(run: RunRecord, projection: TerminalProjection, *, dsn: str | None) -> int:
    """Commit the binding's terminal projection to the registry.

    Phase B makes this transactional with the outbox rows the projection carries;
    in Phase A ``projection.outbox`` is always empty and finalisation is the same
    single ``mark_*`` the extracted worker performed.
    """
    if projection.outbox:  # pragma: no cover - Phase B surface, unreachable in v1
        raise RegistryError(
            "TerminalProjection.outbox requires schema v2; this build is v1-only. "
            "Upgrade workflow-runtime-core and migrate before emitting outbox rows."
        )

    with registry.connect(dsn) as conn:
        if projection.status is RunStatus.FAILED:
            registry.mark_failed(conn, run.run_id, projection.error or "run failed")
            logger.info("run %s failed via terminal projection", run.run_id)
            return EXIT_FAILED
        registry.mark_completed(conn, run.run_id, projection.artifacts)
    logger.info("run %s completed", run.run_id)
    return EXIT_OK
