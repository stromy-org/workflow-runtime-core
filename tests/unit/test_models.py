"""Model + projection unit tests (no database required)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from workflow_runtime_core.models import (
    TERMINAL_STATUS_VALUES,
    TERMINAL_STATUSES,
    Run,
    RunRecord,
    RunStatus,
    TerminalProjection,
)

_NOW = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


def _row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "run_id": "11111111-1111-1111-1111-111111111111",
        "workflow": "demo",
        "thread_id": "11111111-1111-1111-1111-111111111111",
        "status": "queued",
        "client_slug": "acme",
        "config_json": {"a": 1},
        "image_tag": "sha-abc",
        "job_template_json": {"env": [{"name": "SECRET", "secretRef": "s"}]},
        "created_at": _NOW,
        "updated_at": _NOW,
        "interrupt_payload": None,
        "error": None,
        "artifacts_json": None,
        "idempotency_key": None,
    }
    row.update(overrides)
    return row


@pytest.mark.unit
def test_run_alias_points_at_run_record() -> None:
    """The extracted Stromy code referred to ``registry.Run``."""
    assert Run is RunRecord


@pytest.mark.unit
def test_from_row_parses_status_into_the_enum() -> None:
    run = RunRecord.from_row(_row(status="running"))
    assert run.status is RunStatus.RUNNING


@pytest.mark.unit
def test_from_row_defaults_a_null_config_to_empty_dict() -> None:
    """``config_json`` is NOT NULL in DDL, but a NULL from an older row must not
    become ``None`` and crash every consumer that does ``dict(run.config_json)``."""
    assert RunRecord.from_row(_row(config_json=None)).config_json == {}


@pytest.mark.unit
def test_terminal_statuses_are_exactly_the_unresumable_ones() -> None:
    assert TERMINAL_STATUSES == {
        RunStatus.COMPLETED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
    }
    assert RunStatus.PAUSED not in TERMINAL_STATUSES, "a paused run is resumable"
    assert TERMINAL_STATUS_VALUES == {"completed", "failed", "cancelled"}


@pytest.mark.unit
def test_public_projection_withholds_the_job_template_and_config() -> None:
    """The rendered job template is secret-bearing; it must never reach a caller."""
    public = RunRecord.from_row(_row()).public()
    assert "job_template_json" not in public
    assert "config_json" not in public
    assert "image_tag" not in public
    assert public["run_id"] == "11111111-1111-1111-1111-111111111111"
    assert public["status"] == "queued"


@pytest.mark.unit
def test_public_projection_serialises_timestamps() -> None:
    public = RunRecord.from_row(_row()).public()
    assert public["created_at"] == _NOW.isoformat()


@pytest.mark.unit
def test_terminal_projection_defaults_to_an_empty_outbox() -> None:
    """Phase A emits no outbox rows; the field exists so a Phase B binding keeps
    the same signature."""
    projection = TerminalProjection(status=RunStatus.COMPLETED)
    assert projection.outbox == ()
    assert projection.artifacts is None
