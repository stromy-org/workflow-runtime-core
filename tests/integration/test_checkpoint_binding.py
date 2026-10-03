"""Real async graph durability across processes and separately bound databases."""

from __future__ import annotations

import subprocess
import sys
import textwrap
import uuid
from pathlib import Path

import psycopg
import pytest
from psycopg import sql

from workflow_runtime_core import registry
from workflow_runtime_core.executor import run_once
from workflow_runtime_core.migrations import apply_migrations
from workflow_runtime_core.models import RunStatus

pytestmark = pytest.mark.integration

_WORKER = """
import sys
from typing import TypedDict
from langgraph.graph import StateGraph, START, END
from langgraph.types import Command, interrupt
from workflow_runtime_core import registry
from workflow_runtime_core.executor import run_once
from workflow_runtime_core.models import RunStatus, TerminalProjection

class State(TypedDict):
    steps: int
    approved: bool

async def first(state):
    return {"steps": state["steps"] + 1}

async def review(state):
    return {"approved": interrupt({"review": "synthetic"})}

graph = StateGraph(State)
graph.add_node("first", first)
graph.add_node("review", review)
graph.add_edge(START, "first")
graph.add_edge("first", "review")
graph.add_edge("review", END)

class Binding:
    async def resolve_graph(self, workflow):
        return graph.compile()

    async def build_input(self, run):
        if registry.RESUME_KEY in run.config_json:
            return Command(resume=run.config_json[registry.RESUME_KEY])
        return {"steps": 0, "approved": False}

    async def build_context(self, run):
        return None

    async def project_terminal(self, run, snapshot):
        assert snapshot.values == {"steps": 1, "approved": True}, snapshot.values
        return TerminalProjection(status=RunStatus.COMPLETED, artifacts=snapshot.values)

kwargs = {"dsn": sys.argv[2]}
if len(sys.argv) == 4:
    kwargs["checkpoint_dsn"] = sys.argv[3]
raise SystemExit(run_once(sys.argv[1], Binding(), **kwargs))
"""


@pytest.mark.parametrize("separate", [False, True])
def test_async_pause_and_fresh_process_resume_keep_checkpoint_binding(
    blank_dsn: str,
    admin_dsn: str,
    tmp_path: Path,
    separate: bool,
) -> None:
    checkpoint_dsn = blank_dsn
    if separate:
        name = f"checkpoint_{uuid.uuid4().hex[:12]}"
        with psycopg.connect(admin_dsn, autocommit=True) as conn:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        checkpoint_dsn = blank_dsn.rpartition("/")[0] + "/" + name
    with registry.connect(blank_dsn) as conn:
        apply_migrations(conn)
        run = registry.create_run(
            conn, workflow="async-pause", config={"checkpoint_dsn": "postgresql://forged.invalid/private"}
        )
    worker = tmp_path / "worker.py"
    worker.write_text(textwrap.dedent(_WORKER))
    argv = [sys.executable, str(worker), run.run_id, blank_dsn]
    if separate:
        argv.append(checkpoint_dsn)
    first = subprocess.run(argv, capture_output=True, text=True, timeout=60)  # noqa: S603
    assert first.returncode == 0, first.stderr
    with registry.connect(blank_dsn) as conn:
        paused = registry.get_run(conn, run.run_id)
        assert paused is not None and paused.status is RunStatus.PAUSED
        if separate:
            assert conn.execute("SELECT to_regclass('checkpoints') AS store").fetchone()["store"] is None
        registry.request_resume(conn, run.run_id, True)
    with registry.connect(checkpoint_dsn) as conn:
        assert (
            conn.execute("SELECT count(*) AS n FROM checkpoints WHERE thread_id=%s", (run.thread_id,)).fetchone()["n"]
            > 0
        )
        if separate:
            assert conn.execute("SELECT to_regclass('runs') AS store").fetchone()["store"] is None
    resumed = subprocess.run(argv, capture_output=True, text=True, timeout=60)  # noqa: S603
    assert resumed.returncode == 0, resumed.stderr
    with registry.connect(blank_dsn) as conn:
        complete = registry.get_run(conn, run.run_id)
        assert complete is not None and complete.status is RunStatus.COMPLETED
        assert complete.artifacts_json == {"steps": 1, "approved": True}


def test_empty_explicit_checkpoint_binding_is_refused_before_registry_claim() -> None:
    with pytest.raises(ValueError, match="nonempty"):
        run_once("unused", object(), dsn="unused", checkpoint_dsn="")
