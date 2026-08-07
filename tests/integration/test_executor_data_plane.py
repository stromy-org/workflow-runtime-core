"""``execute`` against a real registry and a real checkpoint store.

Three guarantees the data plane rests on, each asserted end-to-end rather than
by inspecting the code that implements it:

1. **A leased run stays single-writer.** The lease is renewed alongside the graph,
   and losing it stops the run *without* writing a terminal status — because the
   replacement runner already owns the outcome.
2. **``completed`` never precedes publication.** The binding publishes inside
   ``project_terminal``; the status flip and the publication stamp land together.
3. **A failure names its stage.** On v2 the durable payload says where the run
   died; on v1 — the shared registry during the expansion window — the recorded
   text is exactly what it always was.

The graph is a stand-in, not a LangGraph. What is under test is the runner's
lifecycle, and a real compiled graph would only add a slower way to reach the
same call sequence (``ainvoke`` then ``aget_state``).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from workflow_runtime_core import registry
from workflow_runtime_core.exceptions import CheckpointerError, LeaseLost, StageFailure
from workflow_runtime_core.executor.checkpointer import acheckpointer
from workflow_runtime_core.executor.runner import EXIT_CLAIM_LOST, EXIT_FAILED, EXIT_OK, execute
from workflow_runtime_core.migrations import apply_migrations
from workflow_runtime_core.models import RunRecord, RunStatus, TerminalProjection


class _FakeGraph:
    """Minimal stand-in for a compiled graph.

    ``bind_checkpointer`` shallow-copies whatever it is given, so the mutable
    ``observed`` dict is shared with the copy that actually runs — which is how a
    cancellation raised inside ``ainvoke`` is visible to the assertions.
    """

    context_schema = None

    def __init__(self, *, sleep: float = 0.0, raises: Exception | None = None) -> None:
        self.sleep = sleep
        self.raises = raises
        self.checkpointer: Any = None
        self.observed: dict[str, Any] = {"cancelled": False, "invoked": False}

    async def ainvoke(self, payload: Any, **kwargs: Any) -> dict[str, Any]:
        self.observed["invoked"] = True
        if self.raises is not None:
            raise self.raises
        if self.sleep:
            try:
                await asyncio.sleep(self.sleep)
            except asyncio.CancelledError:
                self.observed["cancelled"] = True
                raise
        return {"artifacts": {"report": "ok"}}

    async def aget_state(self, config: Any) -> Any:
        return type("_Snapshot", (), {"values": {"artifacts": {"report": "ok"}}, "next": ()})()


class _Binding:
    """An ``ExecutionBinding`` whose every stage can be made to fail."""

    def __init__(
        self,
        graph: _FakeGraph | None = None,
        *,
        fail_stage: str | None = None,
        artifacts_published: bool = False,
        status: RunStatus = RunStatus.COMPLETED,
    ) -> None:
        self.graph = graph or _FakeGraph()
        self.fail_stage = fail_stage
        self.artifacts_published = artifacts_published
        self.status = status

    def _maybe_fail(self, stage: str) -> None:
        if self.fail_stage == stage:
            raise RuntimeError(f"{stage} exploded")

    async def resolve_graph(self, workflow: str) -> Any:
        self._maybe_fail("resolve")
        return self.graph

    async def build_input(self, run: RunRecord) -> Any:
        self._maybe_fail("input")
        return {"input": "x"}

    async def build_context(self, run: RunRecord) -> Any:
        self._maybe_fail("context")
        return None

    async def project_terminal(self, run: RunRecord, snapshot: Any) -> TerminalProjection:
        self._maybe_fail("publish")
        return TerminalProjection(
            status=self.status,
            artifacts={"published": [{"artifact_id": "report_pdf"}]},
            error="projected failure" if self.status is RunStatus.FAILED else None,
            artifacts_published=self.artifacts_published,
        )


class _Renewer:
    """Grants ``grants`` renewals, then reports the lease lost."""

    def __init__(self, *, grants: int, interval_seconds: float = 0.01) -> None:
        self.interval_seconds = interval_seconds
        self._grants = grants
        self.calls = 0

    async def renew(self) -> bool:
        self.calls += 1
        return self.calls <= self._grants


def _claimed(dsn: str, *, target: int | None = None) -> RunRecord:
    with registry.connect(dsn) as conn:
        apply_migrations(conn, target=target)
    with registry.connect(dsn) as conn:
        run = registry.create_run(conn, workflow="demo", config={})
        claimed = registry.claim_run(conn, run.run_id)
    assert claimed is not None
    return claimed


def _row(dsn: str, run_id: str) -> RunRecord:
    with registry.connect(dsn) as conn:
        row = registry.get_run(conn, run_id)
    assert row is not None
    return row


# --- 1. a leased run stays single-writer --------------------------------------


@pytest.mark.integration
def test_a_leased_run_is_renewed_and_completes(blank_dsn: str) -> None:
    run = _claimed(blank_dsn)
    renewer = _Renewer(grants=100)
    binding = _Binding(_FakeGraph(sleep=0.08))

    assert execute(run, binding, dsn=blank_dsn, lease=renewer) == EXIT_OK
    assert renewer.calls >= 2
    assert _row(blank_dsn, run.run_id).status is RunStatus.COMPLETED


@pytest.mark.integration
def test_a_lost_lease_stops_the_graph_and_records_nothing(blank_dsn: str) -> None:
    """The run stays ``running``: whichever runner holds the lease now will write
    the outcome, and a terminal status written here would overwrite it."""
    run = _claimed(blank_dsn)
    graph = _FakeGraph(sleep=10)
    binding = _Binding(graph)

    assert execute(run, binding, dsn=blank_dsn, lease=_Renewer(grants=1)) == EXIT_CLAIM_LOST

    assert graph.observed["cancelled"] is True
    after = _row(blank_dsn, run.run_id)
    assert after.status is RunStatus.RUNNING
    assert after.error is None
    assert after.error_json is None


@pytest.mark.integration
def test_the_unleased_lane_is_unchanged(blank_dsn: str) -> None:
    """``--run-id`` runs pass no lease; the loop must not require one."""
    run = _claimed(blank_dsn)
    assert execute(run, _Binding(), dsn=blank_dsn) == EXIT_OK
    assert _row(blank_dsn, run.run_id).status is RunStatus.COMPLETED


# --- 2. completed never precedes publication ----------------------------------


@pytest.mark.integration
def test_publication_is_stamped_with_the_completion(blank_dsn: str) -> None:
    run = _claimed(blank_dsn)
    assert execute(run, _Binding(artifacts_published=True), dsn=blank_dsn) == EXIT_OK

    after = _row(blank_dsn, run.run_id)
    assert after.status is RunStatus.COMPLETED
    assert after.artifacts_published_at is not None
    assert after.artifacts_json is not None
    assert after.artifacts_json["published"] == [{"artifact_id": "report_pdf"}]
    # The lease is released by the same statement.
    assert after.lease_owner is None
    assert after.lease_expires_at is None


@pytest.mark.integration
def test_nothing_published_means_no_stamp(blank_dsn: str) -> None:
    """An operator-only workflow publishes nothing. Stamping anyway would tell the
    maintenance pass its outputs are in the container when none were ever sent."""
    run = _claimed(blank_dsn)
    assert execute(run, _Binding(artifacts_published=False), dsn=blank_dsn) == EXIT_OK
    assert _row(blank_dsn, run.run_id).artifacts_published_at is None


@pytest.mark.integration
def test_a_publication_failure_leaves_the_run_failed_not_completed(blank_dsn: str) -> None:
    """The ordering that matters: publication happens inside ``project_terminal``,
    so a failure there can never leave a ``completed`` run with missing outputs."""
    run = _claimed(blank_dsn)
    assert execute(run, _Binding(fail_stage="publish"), dsn=blank_dsn) == EXIT_FAILED

    after = _row(blank_dsn, run.run_id)
    assert after.status is RunStatus.FAILED
    assert after.artifacts_published_at is None
    assert after.error_json is not None
    assert after.error_json["stage"] == "artifacts"
    # Retryable: the graph output is on the durable workspace, so a retry
    # republishes rather than recomputing an expensive run.
    assert after.error_json["retryable"] is True


@pytest.mark.integration
def test_a_failed_projection_is_committed_as_the_bindings_decision(blank_dsn: str) -> None:
    run = _claimed(blank_dsn)
    binding = _Binding(status=RunStatus.FAILED)
    assert execute(run, binding, dsn=blank_dsn) == EXIT_FAILED

    after = _row(blank_dsn, run.run_id)
    assert after.status is RunStatus.FAILED
    assert after.error == "projected failure"
    # A binding-decided failure carries no runtime stage — it is an outcome, not
    # a crash — but it still releases the lease.
    assert after.lease_owner is None


# --- 3. a failure names its stage ---------------------------------------------


@pytest.mark.integration
@pytest.mark.parametrize(
    ("fail_stage", "expected_stage", "message_prefix"),
    [
        ("resolve", "graph", "graph resolution failed: "),
        ("input", "inputs", ""),
        ("context", "context", ""),
        ("publish", "artifacts", "terminal projection failed: "),
    ],
)
def test_each_stage_records_its_own_label(
    blank_dsn: str, fail_stage: str, expected_stage: str, message_prefix: str
) -> None:
    run = _claimed(blank_dsn)
    assert execute(run, _Binding(fail_stage=fail_stage), dsn=blank_dsn) == EXIT_FAILED

    after = _row(blank_dsn, run.run_id)
    assert after.status is RunStatus.FAILED
    assert after.error_json is not None
    assert after.error_json["stage"] == expected_stage
    assert after.error_json["error_type"] == "RuntimeError"
    assert after.error_json["correlation_id"]
    assert after.error_json["message"] == f"{message_prefix}{fail_stage} exploded"
    # ``error`` carries the same human text, so an operator reading the run
    # detail sees the failure without decoding JSON.
    assert after.error == f"{message_prefix}{fail_stage} exploded"
    assert after.lease_owner is None


@pytest.mark.integration
def test_a_binding_may_name_its_own_stage(blank_dsn: str) -> None:
    """A durable share that is not mounted and a file that fails its digest both
    surface while building inputs, and they are not the same incident. A binding
    that knows which one it is says so, and the core records that verbatim."""

    class _Staged(_Binding):
        async def build_input(self, run: RunRecord) -> Any:
            raise StageFailure("workspace", "run substrate /mnt/runs is not mounted")

    run = _claimed(blank_dsn)
    assert execute(run, _Staged(), dsn=blank_dsn) == EXIT_FAILED

    after = _row(blank_dsn, run.run_id)
    assert after.error_json is not None
    assert after.error_json["stage"] == "workspace"
    assert after.error_json["message"] == "run substrate /mnt/runs is not mounted"


@pytest.mark.integration
def test_a_bogus_stage_attribute_falls_back_to_the_core_label(blank_dsn: str) -> None:
    """``.stage`` is read off an arbitrary exception, so a non-string must not
    reach the JSON column a client-facing surface renders."""

    class _Weird(_Binding):
        async def build_input(self, run: RunRecord) -> Any:
            exc = RuntimeError("input exploded")
            exc.stage = 42  # type: ignore[attr-defined]
            raise exc

    run = _claimed(blank_dsn)
    assert execute(run, _Weird(), dsn=blank_dsn) == EXIT_FAILED

    after = _row(blank_dsn, run.run_id)
    assert after.error_json is not None
    assert after.error_json["stage"] == "inputs"


@pytest.mark.integration
def test_a_graph_error_is_recorded_bare(blank_dsn: str) -> None:
    """A graph that raises gets no prefix — the exception text IS the reason."""
    run = _claimed(blank_dsn)
    binding = _Binding(_FakeGraph(raises=ValueError("node blew up")))
    assert execute(run, binding, dsn=blank_dsn) == EXIT_FAILED

    after = _row(blank_dsn, run.run_id)
    assert after.error == "node blew up"
    assert after.error_json is not None
    assert after.error_json["stage"] == "graph"
    assert after.error_json["error_type"] == "ValueError"


@pytest.mark.integration
def test_on_v1_the_recorded_failure_is_unchanged(blank_dsn: str) -> None:
    """The shared registry stays v1 during the expansion window. This build must
    write it exactly as the pre-data-plane runner did — no v2-only column, and no
    named-error refusal at a point the old code succeeded."""
    run = _claimed(blank_dsn, target=1)
    binding = _Binding(_FakeGraph(raises=ValueError("node blew up")))
    assert execute(run, binding, dsn=blank_dsn) == EXIT_FAILED

    after = _row(blank_dsn, run.run_id)
    assert after.status is RunStatus.FAILED
    assert after.error == "node blew up"
    # v2 fields read as their defaults through the tolerant reader.
    assert after.error_json is None


@pytest.mark.integration
def test_on_v1_a_completion_still_lands(blank_dsn: str) -> None:
    run = _claimed(blank_dsn, target=1)
    assert execute(run, _Binding(), dsn=blank_dsn) == EXIT_OK
    assert _row(blank_dsn, run.run_id).status is RunStatus.COMPLETED


# --- 4. the checkpointer's error boundary -------------------------------------


@pytest.mark.integration
def test_a_body_failure_is_not_relabelled_as_a_store_failure(blank_dsn: str) -> None:
    """A generator context manager receives the body's exception at its yield, so
    a blanket handler around the ``async with`` used to rewrite every graph and
    stage error as "cannot open the checkpoint store". For ``LeaseLost`` that was
    a correctness bug, not just a bad message: relabelled as a failure, a lost
    lease made the runner write a terminal status over the real owner's outcome."""

    async def _body() -> None:
        async with acheckpointer(blank_dsn):
            raise LeaseLost("lease gone")

    with pytest.raises(LeaseLost, match="lease gone"):
        asyncio.run(_body())


@pytest.mark.integration
def test_a_genuine_open_failure_is_still_reported_as_one() -> None:
    """The flag must not disarm the wrapper for what it was written for."""

    async def _body() -> None:
        async with acheckpointer("postgresql://nobody@127.0.0.1:1/nothing"):
            pass  # pragma: no cover - the open never succeeds

    with pytest.raises(CheckpointerError, match="cannot open the checkpoint store"):
        asyncio.run(_body())
