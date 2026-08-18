"""The optional per-run execution scope (ORG-PLAN-206 C5).

The scope exists so a binding can bind process state — for the BYOK plane, one
client's provider credentials — around exactly one run. Three properties, and
each of them is a security property rather than a convenience:

1. **Entered before ``resolve_graph``.** A graph is compiled against whatever
   the process says at that moment, so a binding that bound credentials after
   resolution has already let a module-level provider client capture the wrong
   ones.
2. **Exited on the way out, whatever the way out is.** The interesting case is
   the exception. A scope that only unwinds on success leaves one client's key
   in the environment for the rest of the process, where the next
   operator-funded code path reads it happily.
3. **A refusal at entry fails the run before any graph work**, with the stage
   the binding named. That is what lets a client-facing surface say "this run
   died acquiring its credentials" without saying anything about which ones.

A binding with no ``execution_scope`` gets a no-op and is not otherwise touched
— asserted here too, because "optional" is a claim about every existing
consumer, and those consumers do not test this file.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from typing import Any

import pytest

from workflow_runtime_core import registry
from workflow_runtime_core.exceptions import StageFailure
from workflow_runtime_core.executor.runner import EXIT_FAILED, EXIT_OK, execute
from workflow_runtime_core.migrations import apply_migrations
from workflow_runtime_core.models import RunRecord, RunStatus, TerminalProjection


class _Graph:
    context_schema = None

    async def astream(self, payload: Any, **kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        yield {"analyse": {"ok": True}}

    async def aget_state(self, config: Any) -> Any:
        return type("_Snapshot", (), {"values": {}, "next": ()})()


class _PlainBinding:
    """No ``execution_scope`` — every consumer that predates this feature."""

    def __init__(self, *, graph: Any | None = None) -> None:
        self.graph = graph or _Graph()
        self.trace: list[str] = []

    async def resolve_graph(self, workflow: str) -> Any:
        self.trace.append("resolve")
        return self.graph

    async def build_input(self, run: RunRecord) -> Any:
        return {"input": "x"}

    async def build_context(self, run: RunRecord) -> Any:
        return None

    async def project_terminal(self, run: RunRecord, snapshot: Any) -> TerminalProjection:
        self.trace.append("project")
        return TerminalProjection(status=RunStatus.COMPLETED, artifacts={})


class _ScopedBinding(_PlainBinding):
    """Records the exact order of scope entry, exit and every stage between."""

    def __init__(
        self,
        *,
        enter_raises: BaseException | None = None,
        graph_raises: BaseException | None = None,
    ) -> None:
        super().__init__(graph=_RaisingGraph(graph_raises) if graph_raises else None)
        self.enter_raises = enter_raises

    @contextlib.asynccontextmanager
    async def execution_scope(self, run: RunRecord) -> AsyncIterator[None]:
        if self.enter_raises is not None:
            raise self.enter_raises
        self.trace.append("enter")
        try:
            yield
        finally:
            self.trace.append("exit")


class _RaisingGraph(_Graph):
    def __init__(self, exc: BaseException) -> None:
        self.exc = exc

    async def astream(self, payload: Any, **kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        raise self.exc
        yield {}  # pragma: no cover - unreachable, keeps this an async generator


def _claimed(dsn: str) -> RunRecord:
    with registry.connect(dsn) as conn:
        apply_migrations(conn)
    with registry.connect(dsn) as conn:
        run = registry.create_run(conn, workflow="demo", config={})
        claimed = registry.claim_run(conn, run.run_id)
    assert claimed is not None
    return claimed


def _failure(dsn: str, run_id: str) -> dict[str, Any]:
    with registry.connect(dsn) as conn:
        run = registry.get_run(conn, run_id)
    assert run is not None
    assert run.error_json is not None
    return run.error_json


# --- 1. ordering --------------------------------------------------------------


@pytest.mark.integration
def test_the_scope_opens_before_the_graph_is_resolved(blank_dsn: str) -> None:
    run = _claimed(blank_dsn)
    binding = _ScopedBinding()
    assert execute(run, binding, dsn=blank_dsn) == EXIT_OK
    assert binding.trace == ["enter", "resolve", "project", "exit"]


# --- 2. unwinding -------------------------------------------------------------


@pytest.mark.integration
def test_the_scope_closes_when_the_graph_raises(blank_dsn: str) -> None:
    """The case that matters. A key bound for this run must not outlive it just
    because the run died."""
    run = _claimed(blank_dsn)
    binding = _ScopedBinding(graph_raises=RuntimeError("node exploded"))
    assert execute(run, binding, dsn=blank_dsn) == EXIT_FAILED
    assert binding.trace == ["enter", "resolve", "exit"]
    assert _failure(blank_dsn, run.run_id)["stage"] == "graph"


# --- 3. refusal at entry ------------------------------------------------------


@pytest.mark.integration
def test_a_refused_scope_fails_the_run_before_any_graph_work(blank_dsn: str) -> None:
    run = _claimed(blank_dsn)
    binding = _ScopedBinding(
        enter_raises=StageFailure("credentials", "client key for openai-api is not registered")
    )
    assert execute(run, binding, dsn=blank_dsn) == EXIT_FAILED

    # Nothing ran: not the scope body, not resolution, not the graph.
    assert binding.trace == []

    failure = _failure(blank_dsn, run.run_id)
    assert failure["stage"] == "credentials"
    assert failure["error_type"] == "StageFailure"
    assert "openai-api" in failure["message"]


@pytest.mark.integration
def test_an_unlabelled_refusal_still_gets_a_stage(blank_dsn: str) -> None:
    """A binding that raises a plain exception is not left labelled ``graph``,
    which would send an operator to read a graph that never ran."""
    run = _claimed(blank_dsn)
    binding = _ScopedBinding(enter_raises=RuntimeError("vault unreachable"))
    assert execute(run, binding, dsn=blank_dsn) == EXIT_FAILED
    assert _failure(blank_dsn, run.run_id)["stage"] == "execution_scope"


# --- 4. the bindings that predate all of this ---------------------------------


@pytest.mark.integration
def test_a_binding_without_a_scope_is_unaffected(blank_dsn: str) -> None:
    run = _claimed(blank_dsn)
    binding = _PlainBinding()
    assert execute(run, binding, dsn=blank_dsn) == EXIT_OK
    assert binding.trace == ["resolve", "project"]
    with registry.connect(blank_dsn) as conn:
        completed = registry.get_run(conn, run.run_id)
    assert completed is not None
    assert completed.status is RunStatus.COMPLETED
