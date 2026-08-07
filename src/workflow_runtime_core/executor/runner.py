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

Lease ownership (schema v2)
---------------------------
``execute`` optionally takes a :class:`~workflow_runtime_core.binding.LeaseRenewer`.
With one, the run holds a single-writer lease that is renewed *alongside* the
graph rather than between its steps, and losing it stops the run without writing
a terminal status. Without one, a claim is exactly the single-flight
``queued -> running`` transition it always was, so the ``--run-id`` lane is
unchanged.

This loop is deliberately core-owned rather than per-consumer. Every durable
executor — the hosted plane and a client-owned service alike — needs the same
three guarantees (renew both halves together, never overwrite another owner's
outcome, never report ``completed`` before the outputs exist), and a second copy
of them is exactly how the ORG-PLAN-155/164 schema fork happened.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from typing import TYPE_CHECKING, Any, cast

from .. import registry, schema
from ..exceptions import LeaseLost, RegistryError
from ..models import TERMINAL_STATUSES, RunRecord, RunStatus, TerminalProjection
from .checkpointer import DURABILITY, acheckpointer, bind_checkpointer

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Awaitable

    from ..binding import ExecutionBinding, LeaseRenewer

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
    """Carries which stage failed, and the message prefix it records.

    Two readers of the same fact: the ``error`` text (whose prefix the extracted
    worker's tests assert on) and, on schema v2, ``error_json.stage`` — the field
    a client-facing failure surface uses to say *where* a run died without
    exposing internal frames. Keeping both with the stage, rather than inferring
    them from where an exception surfaced, means the whole run still executes
    inside ONE event loop, which a binding holding async resources requires.
    """

    def __init__(self, stage: str, prefix: str, cause: BaseException) -> None:
        super().__init__(f"{prefix}{cause}" if prefix else str(cause))
        self.stage = stage
        self.error_type = type(cause).__name__


def _declared_stage(exc: BaseException, default: str) -> str:
    """A binding's own stage label, or the core's default for that call.

    Read defensively: ``.stage`` may be anything on an arbitrary exception, and a
    non-string would end up in a JSON column that a client-facing surface reads.
    """
    declared = getattr(exc, "stage", None)
    return declared if isinstance(declared, str) and declared else default


async def _with_lease_renewal(
    invocation: Awaitable[Any], lease: LeaseRenewer, *, run_id: str
) -> Any:
    """Drive ``invocation`` while renewing ``lease``; cancel it if the lease drops.

    The renewal runs as a sibling task rather than between graph steps: a single
    node can legitimately take many minutes (deep research, rendering), and a
    lease that only advanced at step boundaries would expire mid-node and invite a
    second writer onto the same checkpoint thread.

    On loss the graph is cancelled **and awaited** before :class:`LeaseLost`
    propagates. Reporting first and stopping later would leave this process still
    writing checkpoints while the replacement runner is already writing its own.
    """
    task = asyncio.ensure_future(invocation)
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=lease.interval_seconds)
            if done:
                return task.result()
            if not await lease.renew():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                raise LeaseLost(
                    f"lease for run {run_id} was lost; another runner owns it now"
                )
    finally:
        if not task.done():  # pragma: no cover - defensive
            task.cancel()


def execute(
    run: RunRecord,
    binding: ExecutionBinding,
    *,
    dsn: str | None = None,
    lease: LeaseRenewer | None = None,
) -> int:
    """Execute an already-claimed run to a terminal or paused state.

    Pass ``lease`` when the run arrived over a transport that must stay leased
    for the duration (a queue message). Losing the lease returns
    ``EXIT_CLAIM_LOST`` and records nothing: the replacement runner owns the
    outcome from that moment.
    """
    config = dict(run.config_json)
    config.pop(registry.RESUME_KEY, None)

    invoke_config = {"configurable": {"thread_id": run.thread_id, **config}}

    async def _drive() -> tuple[str, Any]:
        try:
            graph = await binding.resolve_graph(run.workflow)
        except Exception as exc:
            raise _StageError("graph", "graph resolution failed: ", exc) from exc

        # No message prefix: a binding that prepares inputs (materialising an
        # uploaded set into the run workspace, say) raises errors already phrased
        # for an operator, and re-wrapping them would bury that text mid-sentence.
        # The STAGE is what the core adds, so a client-facing surface can say
        # "this run died fetching its inputs" without reading the message at all
        # — unless the binding named a sharper one (see
        # :class:`~workflow_runtime_core.exceptions.StageFailure`), which wins.
        try:
            payload = await binding.build_input(run)
        except Exception as exc:
            raise _StageError(_declared_stage(exc, "inputs"), "", exc) from exc

        # Hosted graphs are async (every real workflow node is ``async def``), so
        # they run through LangGraph's ASYNC Pregel loop: ``ainvoke`` against an
        # async-capable saver. A sync ``.invoke()`` raises "No synchronous
        # function provided" on the first async node, and a sync ``PostgresSaver``
        # has no async methods for the async loop to call. ``ainvoke`` is a strict
        # superset — it also runs any sync-node graph.
        async with acheckpointer(dsn) as saver:
            compiled = bind_checkpointer(graph, saver)
            try:
                context = await binding.build_context(run)
            except Exception as exc:
                raise _StageError(_declared_stage(exc, "context"), "", exc) from exc
            invocation = cast(
                "Awaitable[Any]",
                compiled.ainvoke(  # type: ignore[attr-defined]
                    payload,
                    config=invoke_config,
                    context=context,
                    durability=DURABILITY,
                ),
            )
            final_state: Any = (
                await invocation
                if lease is None
                else await _with_lease_renewal(invocation, lease, run_id=run.run_id)
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
            # The binding publishes its declared exports inside this call, so a
            # publication failure lands here — BEFORE any terminal write. That
            # ordering is the point: a run marked ``completed`` whose exports
            # never reached durable storage tells the client to collect something
            # that does not exist, and no retry follows because the run is
            # terminal. Failing here instead leaves it retryable with its
            # workspace intact, so a retry republishes what the graph already
            # produced rather than recomputing it.
            try:
                projection = await binding.project_terminal(run, snapshot)
            except Exception as exc:
                raise _StageError(
                    _declared_stage(exc, "artifacts"),
                    "terminal projection failed: ",
                    exc,
                ) from exc
            return ("terminal", projection)

    try:
        outcome, value = asyncio.run(_drive())
    except LeaseLost as exc:
        # Stop immediately and WITHOUT a terminal status: the replacement runner
        # has already been told it may claim this run, so recording an outcome
        # here would overwrite a result this process never saw.
        logger.error("run %s: %s", run.run_id, exc)
        return EXIT_CLAIM_LOST
    except Exception as exc:  # noqa: BLE001 - any graph error is a run failure
        return _record_failure(run, exc, dsn=dsn)

    if outcome == "paused":
        with registry.connect(dsn) as conn:
            registry.mark_paused(conn, run.run_id, value)
        logger.info("run %s paused at interrupt; exiting (state is durable)", run.run_id)
        return EXIT_OK

    return _finalize(run, value, dsn=dsn)


def _record_failure(run: RunRecord, exc: BaseException, *, dsn: str | None) -> int:
    """Record a run failure — structured on v2, plain text on v1.

    The traceback goes to the server log keyed by a correlation id; the durable
    payload gets a stage, an exception type and a truncated message. A client
    payload is not the place for internal frames or filesystem paths, and an
    operator reading the log needs the id to find the frames that matter.

    On v1 this is byte-for-byte the transition the extracted worker wrote, so a
    registry still inside the expansion window sees no change.
    """
    stage = getattr(exc, "stage", "graph")
    error_type = getattr(exc, "error_type", type(exc).__name__)
    correlation_id = str(uuid.uuid4())
    logger.exception(
        "run %s failed at stage %s (correlation_id=%s)", run.run_id, stage, correlation_id
    )
    message = str(exc)
    with registry.connect(dsn) as conn:
        live = schema.read_schema_version(conn)
        if live is not None and live >= 2:
            registry.mark_failed_structured(
                conn,
                run.run_id,
                {
                    "stage": stage,
                    "error_type": error_type,
                    "message": message[:2000],
                    # Retryable by default: every stage the core can fail at
                    # leaves the durable workspace and the checkpoint intact, so
                    # a retry resumes rather than recomputing. A binding that
                    # knows better says so by returning a FAILED projection.
                    "retryable": True,
                    "correlation_id": correlation_id,
                },
            )
        else:
            registry.mark_failed(conn, run.run_id, message)
    return EXIT_FAILED


def _finalize(run: RunRecord, projection: TerminalProjection, *, dsn: str | None) -> int:
    """Commit the binding's terminal projection to the registry.

    One ``mark_*`` per outcome, and on v2 the completion carries the publication
    stamp in the same statement (see
    :attr:`~workflow_runtime_core.models.TerminalProjection.artifacts_published`).
    """
    if projection.outbox:  # pragma: no cover - lands with migration 0003
        raise RegistryError(
            "TerminalProjection.outbox requires the messaging tables (migration "
            "0003, ORG-PLAN-155 Phase B), which this build's chain stops short of. "
            "Upgrade workflow-runtime-core and migrate before emitting outbox rows."
        )

    with registry.connect(dsn) as conn:
        if projection.status is RunStatus.FAILED:
            registry.mark_failed(conn, run.run_id, projection.error or "run failed")
            logger.info("run %s failed via terminal projection", run.run_id)
            return EXIT_FAILED
        registry.mark_completed(
            conn,
            run.run_id,
            projection.artifacts,
            artifacts_published=projection.artifacts_published,
        )
    logger.info("run %s completed", run.run_id)
    return EXIT_OK
