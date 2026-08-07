"""The terminal transaction — run state and its outbox rows land together.

The window this closes: a run finishes, the process records ``completed``, then
dies before queuing the reply. The run reads as done, the customer never hears
anything, and nothing anywhere records that a message was owed. Writing both in
one transaction makes that state unreachable.

The second property is what makes recovery safe: re-projecting a terminal
snapshot is a normal recovery path, so finalizing twice must produce ONE
message. That is why the outbox key is the caller's stable ``message_id`` and
not a generated id.
"""

from __future__ import annotations

from typing import Any

import pytest

from workflow_runtime_core import registry
from workflow_runtime_core.exceptions import SchemaVersionMismatch
from workflow_runtime_core.executor.runner import EXIT_FAILED, EXIT_OK, execute
from workflow_runtime_core.messaging import OutboxMessage, outbox
from workflow_runtime_core.migrations import apply_migrations
from workflow_runtime_core.models import RunRecord, RunStatus, TerminalProjection

NS = "gmf-pilot"


class _Graph:
    """Stand-in compiled graph.

    ``astream`` in ``updates`` mode — one chunk per finished node — because that
    is how the runner drives graphs, so node completions can become durable
    progress.
    """

    context_schema = None

    def __init__(self) -> None:
        self.checkpointer: Any = None

    async def astream(self, payload: Any, **kwargs: Any) -> Any:
        yield {"analyse": {"ok": True}}

    async def aget_state(self, config: Any) -> Any:
        return type("_Snapshot", (), {"values": {}, "next": ()})()


class _Binding:
    """Projects a fixed set of outbox messages with STABLE ids."""

    def __init__(
        self, *, status: RunStatus = RunStatus.COMPLETED, messages: int = 1
    ) -> None:
        self.graph = _Graph()
        self.status = status
        self.messages = messages

    async def resolve_graph(self, workflow: str) -> Any:
        return self.graph

    async def build_input(self, run: RunRecord) -> Any:
        return {"input": "x"}

    async def build_context(self, run: RunRecord) -> Any:
        return None

    async def project_terminal(
        self, run: RunRecord, snapshot: Any
    ) -> TerminalProjection:
        return TerminalProjection(
            status=self.status,
            artifacts={"report": "ok"},
            error="projected failure" if self.status is RunStatus.FAILED else None,
            outbox=tuple(
                OutboxMessage(
                    service_namespace=NS,
                    # Derived from the run, so a re-projection reproduces it
                    # byte-for-byte. A uuid here would duplicate every reply.
                    message_id=f"reply:{run.run_id}:{i}",
                    destination="whatsapp.reply",
                    payload={"text": "we are on it"},
                    run_id=run.run_id,
                )
                for i in range(self.messages)
            ),
        )


def _claimed(dsn: str, *, target: int | None = None) -> RunRecord:
    with registry.connect(dsn) as conn:
        apply_migrations(conn, target=target)
    with registry.connect(dsn) as conn:
        run = registry.create_run(conn, workflow="triage", config={})
        claimed = registry.claim_run(conn, run.run_id)
    assert claimed is not None
    return claimed


@pytest.mark.integration
def test_completion_and_its_outbox_message_land_together(blank_dsn: str) -> None:
    run = _claimed(blank_dsn)

    assert execute(run, _Binding(), dsn=blank_dsn) == EXIT_OK

    with registry.connect(blank_dsn) as conn:
        after = registry.get_run(conn, run.run_id)
        depth = outbox.pending_depth(conn, service_namespace=NS)
        claimed = outbox.claim_due(
            conn, service_namespace=NS, owner="egress", lease_seconds=60
        )

    assert after is not None and after.status is RunStatus.COMPLETED
    assert depth == 1
    assert claimed[0].message_id == f"reply:{run.run_id}:0"
    assert claimed[0].payload["text"] == "we are on it"


@pytest.mark.integration
def test_a_failed_run_still_queues_its_projected_messages(blank_dsn: str) -> None:
    """A failure often owes a reply too ("we could not process this"). The
    outbox is written on the failure path for the same reason as the success
    one."""
    run = _claimed(blank_dsn)

    assert execute(run, _Binding(status=RunStatus.FAILED), dsn=blank_dsn) == EXIT_FAILED

    with registry.connect(blank_dsn) as conn:
        after = registry.get_run(conn, run.run_id)
        assert outbox.pending_depth(conn, service_namespace=NS) == 1

    assert after is not None and after.status is RunStatus.FAILED


@pytest.mark.integration
def test_refinalizing_the_same_run_does_not_duplicate_the_reply(
    blank_dsn: str,
) -> None:
    """Recovery re-projects terminal snapshots. One customer message must not
    become two because a process was restarted at the wrong moment."""
    run = _claimed(blank_dsn)
    binding = _Binding()

    assert execute(run, binding, dsn=blank_dsn) == EXIT_OK
    # Second finalization of the very same run — what a crashed-and-restarted
    # runner does when it finds a completed checkpoint.
    assert execute(run, binding, dsn=blank_dsn) == EXIT_OK

    with registry.connect(blank_dsn) as conn:
        assert outbox.pending_depth(conn, service_namespace=NS) == 1


@pytest.mark.integration
def test_multiple_destinations_are_queued_independently(blank_dsn: str) -> None:
    run = _claimed(blank_dsn)
    assert execute(run, _Binding(messages=3), dsn=blank_dsn) == EXIT_OK
    with registry.connect(blank_dsn) as conn:
        assert outbox.pending_depth(conn, service_namespace=NS) == 3


@pytest.mark.integration
def test_outbox_on_a_pre_v3_registry_fails_by_name(blank_dsn: str) -> None:
    """A v2 database has no messaging tables. The failure must name migration
    0003, not surface as an UndefinedTable from inside a finalizer after the
    startup gate already went green."""
    run = _claimed(blank_dsn, target=2)

    with pytest.raises(SchemaVersionMismatch, match="v3"):
        execute(run, _Binding(), dsn=blank_dsn)

    with registry.connect(blank_dsn) as conn:
        after = registry.get_run(conn, run.run_id)
    # And the run was NOT marked complete on the way past.
    assert after is not None and after.status is RunStatus.RUNNING
