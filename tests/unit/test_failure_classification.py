"""The two defensive readers the runner uses on values it did not produce.

Both read attributes off exceptions raised by a *binding* — arbitrary consumer
code — and both write what they find into JSON columns a client-facing surface
reads back. So neither may pass a value through unexamined, and both need an
answer for "the attribute is missing or nonsense" that is safe rather than merely
defined.

No database and no graph: these are pure functions, and the end-to-end proof that
a binding's verdict survives into ``error_json`` lives in
``tests/integration/test_executor_data_plane.py``.
"""

from __future__ import annotations

import pytest

from workflow_runtime_core.executor.runner import (
    _declared_retryable,
    _nodes_completed_so_far,
    _StageError,
)
from workflow_runtime_core.models import RunRecord, RunStatus


@pytest.mark.unit
def test_an_exception_says_nothing_and_is_treated_as_retryable() -> None:
    """The default is the safe direction: a needless retry costs compute, while
    refusing to retry a transient failure strands a recoverable run."""
    assert _declared_retryable(RuntimeError("boom")) is True


@pytest.mark.unit
def test_a_binding_declaring_false_is_believed() -> None:
    class _Deterministic(RuntimeError):
        retryable = False

    assert _declared_retryable(_Deterministic()) is False


@pytest.mark.unit
@pytest.mark.parametrize("value", ["no", 0, None, object()])
def test_a_non_bool_verdict_is_not_a_verdict(value: object) -> None:
    """Truthiness is the wrong test here — ``retryable = 0`` would silently mean
    "never retry this" while ``retryable = "no"`` would mean the opposite. Only a
    real bool counts; anything else means the binding did not answer."""
    exc = RuntimeError("boom")
    exc.retryable = value  # type: ignore[attr-defined]
    assert _declared_retryable(exc) is True


@pytest.mark.unit
def test_the_stage_wrapper_carries_the_causes_verdict() -> None:
    """The wrapper is what reaches the recorder, so a verdict left behind on the
    cause is a verdict dropped exactly where it was needed."""

    class _Deterministic(RuntimeError):
        retryable = False

    wrapped = _StageError("artifacts", "terminal projection failed: ", _Deterministic("no export"))
    assert wrapped.retryable is False
    assert wrapped.stage == "artifacts"
    assert wrapped.error_type == "_Deterministic"
    assert _declared_retryable(wrapped) is False


def _run(progress: object) -> RunRecord:
    """A row-shaped record, built the way the registry builds one.

    ``from_row`` rather than the constructor because that is the only path a real
    ``RunRecord`` takes, and it is what tolerates the partial rows these cases are
    about.
    """
    row: dict[str, object] = dict.fromkeys(
        (
            "client_slug",
            "image_tag",
            "job_template_json",
            "created_at",
            "updated_at",
            "interrupt_payload",
            "error",
            "artifacts_json",
            "idempotency_key",
        )
    )
    row.update(
        run_id="r1",
        workflow="demo",
        status=RunStatus.QUEUED.value,
        thread_id="t1",
        config_json={},
        progress_json=progress,
    )
    return RunRecord.from_row(row)


@pytest.mark.unit
def test_a_fresh_run_has_no_prior_count() -> None:
    assert _nodes_completed_so_far(_run(None)) == 0


@pytest.mark.unit
def test_a_run_shaped_object_without_the_field_is_tolerated() -> None:
    """Consumers drive ``execute`` with their own run-shaped objects.

    Requiring a new attribute would break them on what is meant to be a safe
    upgrade — which is exactly what happened to Stromy's own test double before
    this read became defensive. A run that cannot say how far it got has not got
    anywhere, as far as the counter is concerned.
    """

    class _MinimalRun:
        run_id = "r1"

    assert _nodes_completed_so_far(_MinimalRun()) == 0  # type: ignore[arg-type]


@pytest.mark.unit
def test_a_resumed_run_continues_from_the_row() -> None:
    assert _nodes_completed_so_far(_run({"node": "review", "nodes_completed": 12})) == 12


@pytest.mark.unit
@pytest.mark.parametrize(
    "progress",
    [
        {},
        {"nodes_completed": None},
        {"nodes_completed": "12"},
        {"nodes_completed": True},  # a bool is an int in Python; it is not a count
        {"nodes_completed": -3},
        "not a mapping",
    ],
)
def test_an_unusable_prior_count_reads_as_zero(progress: object) -> None:
    """``progress_json`` is a JSON column an older writer or a hand-edited row may
    have shaped differently. Anything unusable means "no prior count", which is
    exactly the fresh-run answer — never a crash on the resume path."""
    assert _nodes_completed_so_far(_run(progress)) == 0
