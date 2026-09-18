"""The optional runnable-config contribution (ORG-291).

The core owns the runnable config it hands to ``astream``, and for three releases
it built that config as ``{"configurable": {...}}`` and nothing else. LangChain
carries callbacks at the config's TOP level, so there was no channel a binding
could put a callback handler on — and the one that looked like it would,
``build_context``, feeds LangGraph's *runtime context*, a different parameter that
discards LangChain callbacks without complaint.

That failed silently, which is why it lived four months: on ``gmf-support-agent``
a run completed, the email was delivered, and Langfuse received zero traces.
Nothing raised, because nothing was wrong from LangGraph's point of view — it had
been handed some state it did not recognise and ignored it.

So the properties asserted here are about the *seam*, not about tracing:

1. **What the binding returns reaches ``astream``'s config, top-level.** This is
   the negative control the incident asked for: drop the merge and this test
   fails, because it reads the config the graph was actually invoked with rather
   than trusting that the binding was called.
2. **It does NOT reach ``aget_state``.** That call is a checkpoint read; handing
   it a callback handler publishes an observation for a node that never ran.
3. **``configurable`` cannot be overwritten**, and the refusal names the key. It
   carries ``thread_id``, so a binding that replaced it would detach the run from
   its own checkpoint thread and the damage would surface far from the cause.
4. **A binding without the method is untouched** — "optional" is a claim about
   every consumer that predates this, and those consumers do not test this file.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from typing import Any

import pytest

from workflow_runtime_core import registry
from workflow_runtime_core.executor.runner import EXIT_FAILED, EXIT_OK, execute
from workflow_runtime_core.migrations import apply_migrations
from workflow_runtime_core.models import RunRecord, RunStatus, TerminalProjection


class _RecordingGraph:
    """Records the config each call was given, which is the whole assertion.

    Records into LISTS rather than attributes on purpose. ``bind_checkpointer``
    hands the runner a ``copy.copy`` of this graph, so an attribute the driven
    copy sets is invisible here — a shared list survives the shallow copy because
    both instances hold the same reference. Written as attributes first, and
    every assertion read ``None``.
    """

    context_schema = None

    def __init__(self) -> None:
        self.stream_configs: list[Any] = []
        self.state_configs: list[Any] = []

    @property
    def stream_config(self) -> Any:
        return self.stream_configs[-1] if self.stream_configs else None

    @property
    def state_config(self) -> Any:
        return self.state_configs[-1] if self.state_configs else None

    async def astream(
        self, payload: Any, **kwargs: Any
    ) -> AsyncIterator[dict[str, Any]]:
        self.stream_configs.append(kwargs.get("config"))
        yield {"analyse": {"ok": True}}

    async def aget_state(self, config: Any) -> Any:
        self.state_configs.append(config)
        return type("_Snapshot", (), {"values": {}, "next": ()})()


class _PlainBinding:
    """No ``build_invoke_config`` — every consumer that predates ORG-291."""

    def __init__(self) -> None:
        self.graph = _RecordingGraph()

    async def resolve_graph(self, workflow: str) -> Any:
        return self.graph

    async def build_input(self, run: RunRecord) -> Any:
        return {"input": "x"}

    async def build_context(self, run: RunRecord) -> Any:
        return None

    async def project_terminal(
        self, run: RunRecord, snapshot: Any
    ) -> TerminalProjection:
        return TerminalProjection(status=RunStatus.COMPLETED, artifacts={})


_HANDLER = object()
_TRACING_CONTRIBUTION: Mapping[str, Any] = {
    "callbacks": [_HANDLER],
    "metadata": {"langfuse_session_id": "conv-1"},
}


class _ConfiguredBinding(_PlainBinding):
    """Contributes what a tracing binding contributes: a callback + metadata."""

    def __init__(self, extra: Mapping[str, Any] | None = None) -> None:
        super().__init__()
        self._extra = _TRACING_CONTRIBUTION if extra is None else extra
        self.calls: list[str] = []

    async def build_invoke_config(self, run: RunRecord) -> Mapping[str, Any]:
        self.calls.append(run.run_id)
        return self._extra


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


# --- 1. the contribution lands where the callback manager looks ---------------


@pytest.mark.integration
def test_the_contribution_reaches_astream_top_level(blank_dsn: str) -> None:
    """The negative control: read the config the graph was invoked WITH.

    Asserting that ``build_invoke_config`` was called would have passed against
    the bug — it was the merge that was missing, not the call.
    """
    run = _claimed(blank_dsn)
    binding = _ConfiguredBinding()
    assert execute(run, binding, dsn=blank_dsn) == EXIT_OK

    config = binding.graph.stream_config
    assert binding.calls == [run.run_id]
    assert config["callbacks"] == [_HANDLER]
    assert config["metadata"] == {"langfuse_session_id": "conv-1"}
    # The core's own key survives the merge untouched.
    assert config["configurable"]["thread_id"] == run.thread_id


@pytest.mark.integration
def test_the_contribution_does_not_reach_a_checkpoint_read(blank_dsn: str) -> None:
    """``aget_state`` is a read. A handler there traces a node that never ran."""
    run = _claimed(blank_dsn)
    binding = _ConfiguredBinding()
    assert execute(run, binding, dsn=blank_dsn) == EXIT_OK

    assert binding.graph.state_config is not None
    assert "callbacks" not in binding.graph.state_config


@pytest.mark.integration
@pytest.mark.parametrize(
    "extra", [{"callbacks": [_HANDLER]}, {"tags": ["channel:whatsapp"]}]
)
def test_arbitrary_top_level_keys_are_threaded(
    blank_dsn: str, extra: Mapping[str, Any]
) -> None:
    """The seam is the runnable config in general, not a callbacks special case."""
    run = _claimed(blank_dsn)
    binding = _ConfiguredBinding(extra=extra)
    assert execute(run, binding, dsn=blank_dsn) == EXIT_OK

    for key, value in extra.items():
        assert binding.graph.stream_config[key] == value


# --- 2. the reserved key ------------------------------------------------------


@pytest.mark.integration
def test_overwriting_configurable_is_refused_by_name(blank_dsn: str) -> None:
    """It carries ``thread_id``; replacing it detaches the run from its thread."""
    run = _claimed(blank_dsn)
    binding = _ConfiguredBinding(extra={"configurable": {"thread_id": "someone-else"}})
    assert execute(run, binding, dsn=blank_dsn) == EXIT_FAILED

    failure = _failure(blank_dsn, run.run_id)
    assert failure["stage"] == "invoke_config"
    assert "configurable" in failure["message"]
    # Refused BEFORE the graph ran, not after it corrupted the thread.
    assert binding.graph.stream_config is None


# --- 3. the bindings that predate all of this ---------------------------------


@pytest.mark.integration
def test_a_binding_without_the_method_is_unaffected(blank_dsn: str) -> None:
    run = _claimed(blank_dsn)
    binding = _PlainBinding()
    assert execute(run, binding, dsn=blank_dsn) == EXIT_OK

    config = binding.graph.stream_config
    assert config["configurable"]["thread_id"] == run.thread_id
    assert set(config) == {"configurable"}


@pytest.mark.integration
def test_an_empty_contribution_is_a_real_no_op(blank_dsn: str) -> None:
    run = _claimed(blank_dsn)
    binding = _ConfiguredBinding(extra={})
    assert execute(run, binding, dsn=blank_dsn) == EXIT_OK
    assert set(binding.graph.stream_config) == {"configurable"}
