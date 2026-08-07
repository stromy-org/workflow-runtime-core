"""The progress recorder's arithmetic and its refusal to be fatal.

No database: what is under test is the rate limit, the counter, and — most
importantly — that a registry which cannot store progress degrades quietly. That
last one is the reason this is telemetry rather than lifecycle: a v1 registry has
no ``progress_json`` column, and a multi-hour run must not die because nobody could
be told which node it was on.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from workflow_runtime_core.exceptions import SchemaVersionMismatch
from workflow_runtime_core.executor.progress import ProgressRecorder, node_names


class _Clock:
    """An advanceable stand-in for the recorder's monotonic clock.

    The rate limit is proved by moving time, not by waiting through it — a test
    that sleeps 15 real seconds is a test nobody runs. Patched at the recorder's
    own ``_monotonic`` alias rather than at ``time.monotonic``, because the event
    loop reads the latter and freezing it stops ``asyncio.run``'s timers.
    """

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class _Recorder(ProgressRecorder):
    """Captures writes instead of performing them."""

    def __init__(self, *, interval: float = 15.0, raises: Exception | None = None) -> None:
        super().__init__("run-1", min_interval_seconds=interval)
        self.writes: list[dict[str, Any]] = []
        self.raises = raises

    def _write_blocking(self, payload: dict[str, Any]) -> None:
        if self.raises is not None:
            raise self.raises
        self.writes.append(payload)


@pytest.fixture(autouse=True)
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    import workflow_runtime_core.executor.progress as mod

    fake = _Clock()
    monkeypatch.setattr(mod, "_monotonic", fake)
    return fake


def _observe(recorder: ProgressRecorder, chunk: dict[str, Any]) -> None:
    asyncio.run(recorder.observe(chunk))


# --- node extraction ----------------------------------------------------------


def test_langgraph_internal_channels_are_not_nodes() -> None:
    """``__interrupt__`` arrives as its own chunk; counting it as a completed node
    would report a pause as progress."""
    assert node_names({"analyse": {}, "__interrupt__": ()}) == ["analyse"]
    assert node_names({"__interrupt__": ()}) == []


def test_a_parallel_superstep_counts_every_node_it_finished() -> None:
    assert node_names({"fetch_a": {}, "fetch_b": {}}) == ["fetch_a", "fetch_b"]


# --- the rate limit -----------------------------------------------------------


def test_the_first_event_writes_immediately() -> None:
    """A client watching a run should see it move as soon as it moves, not after
    one full interval of apparent silence."""
    rec = _Recorder()
    _observe(rec, {"first": {}})
    assert len(rec.writes) == 1
    assert rec.writes[0]["node"] == "first"
    assert rec.writes[0]["nodes_completed"] == 1


def test_a_burst_inside_the_window_writes_once() -> None:
    rec = _Recorder()
    for i in range(50):
        _observe(rec, {f"node_{i}": {}})
    assert len(rec.writes) == 1


def test_suppressed_events_are_carried_not_lost(clock: _Clock) -> None:
    """The counter keeps moving while writes are suppressed, so the next admitted
    write is a true total rather than a resumed count."""
    rec = _Recorder()
    for i in range(10):
        _observe(rec, {f"node_{i}": {}})
    clock.t += 20
    _observe(rec, {"later": {}})

    assert len(rec.writes) == 2
    assert rec.writes[-1]["nodes_completed"] == 11
    assert rec.writes[-1]["node"] == "later"


def test_flush_forces_out_a_suppressed_tail() -> None:
    """Without this, a run whose last node landed inside a suppressed window would
    end reporting stale progress forever — the row is never written again."""
    rec = _Recorder()
    _observe(rec, {"a": {}})
    _observe(rec, {"b": {}})  # suppressed
    assert len(rec.writes) == 1

    asyncio.run(rec.flush())
    assert len(rec.writes) == 2
    assert rec.writes[-1] == {
        "node": "b",
        "nodes_completed": 2,
        "at": rec.writes[-1]["at"],
    }


def test_flush_with_nothing_new_is_a_no_op() -> None:
    rec = _Recorder()
    _observe(rec, {"a": {}})
    asyncio.run(rec.flush())
    asyncio.run(rec.flush())
    assert len(rec.writes) == 1


def test_an_empty_chunk_moves_nothing() -> None:
    rec = _Recorder()
    _observe(rec, {"__interrupt__": ()})
    assert rec.writes == []
    assert rec.nodes_completed == 0


# --- best-effort --------------------------------------------------------------


def test_a_registry_that_cannot_store_progress_disables_the_recorder(clock: _Clock) -> None:
    """The v1 case. One refusal, then silence — not one log line per node."""
    rec = _Recorder(raises=SchemaVersionMismatch("progress_json does not exist"))
    _observe(rec, {"a": {}})
    clock.t += 100
    _observe(rec, {"b": {}})

    assert rec.writes == []
    assert rec.nodes_completed == 2  # the run itself is unaffected


def test_an_unexpected_write_failure_is_also_survivable() -> None:
    rec = _Recorder(raises=RuntimeError("connection reset"))
    _observe(rec, {"a": {}})
    asyncio.run(rec.flush())
    assert rec.writes == []


def test_the_payload_carries_node_names_only() -> None:
    """``progress_json`` is read back by ``RunRecord.public()`` — a client-facing
    surface. The state delta a node returned is the client's own content and must
    not be echoed into it."""
    rec = _Recorder()
    _observe(rec, {"analyse": {"secret_finding": "acquisition target"}})
    assert set(rec.writes[0]) == {"node", "nodes_completed", "at"}
    assert "acquisition target" not in str(rec.writes[0])
