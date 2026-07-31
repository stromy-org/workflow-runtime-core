"""Typed run-lifecycle models shared by every consumer.

These are part of the BASE install: the workflow facade depends on them without
acquiring LangGraph or aio-pika (ORG-PLAN-155 acceptance criterion 1).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class RunStatus(StrEnum):
    """Run lifecycle. Transitions are enforced in SQL, not by convention.

    queued -> running -> {paused, completed, failed}
    paused -> queued (resume)      queued|paused -> cancelled
    """

    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_STATUSES = frozenset({RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED})

#: The same set as plain strings — handy for SQL parameters and for callers that
#: compare against a raw ``row["status"]`` rather than a parsed enum.
TERMINAL_STATUS_VALUES = frozenset(s.value for s in TERMINAL_STATUSES)


@dataclass(frozen=True)
class RunRecord:
    """One hosted run. Mirrors a ``runs`` row.

    Schema v2 adds recovery columns (lease, attempt, execution reference). They
    are deliberately absent here in v1 and will arrive as defaulted optional
    fields so that a v1 reader keeps constructing this record unchanged.
    """

    run_id: str
    workflow: str
    thread_id: str
    status: RunStatus
    client_slug: str | None
    config_json: dict[str, Any]
    image_tag: str | None
    job_template_json: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime
    interrupt_payload: dict[str, Any] | None
    error: str | None
    artifacts_json: dict[str, Any] | None
    idempotency_key: str | None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> RunRecord:
        return cls(
            run_id=str(row["run_id"]),
            workflow=row["workflow"],
            thread_id=str(row["thread_id"]),
            status=RunStatus(row["status"]),
            client_slug=row["client_slug"],
            config_json=row["config_json"] or {},
            image_tag=row["image_tag"],
            job_template_json=row["job_template_json"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            interrupt_payload=row["interrupt_payload"],
            error=row["error"],
            artifacts_json=row["artifacts_json"],
            idempotency_key=row["idempotency_key"],
        )

    def public(self) -> dict[str, Any]:
        """The client-safe projection the workflow facade returns.

        Deliberately omits ``config_json``, ``job_template_json`` and
        ``image_tag``: the rendered job template is an internal secret-bearing
        surface and must never reach a caller.
        """
        return {
            "run_id": self.run_id,
            "workflow": self.workflow,
            "status": str(self.status),
            "client_slug": self.client_slug,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "interrupt_payload": self.interrupt_payload,
            "error": self.error,
            "artifacts": self.artifacts_json,
        }


#: Historical name used by the extracted Stromy runtime. Kept so consumer code
#: and tests that referenced ``registry.Run`` continue to type-check.
Run = RunRecord


@dataclass(frozen=True)
class TerminalProjection:
    """What an :class:`~workflow_runtime_core.binding.ExecutionBinding` returns
    once its graph reached a terminal snapshot.

    ``outbox`` is empty in Phase A — the transactional outbox lands with schema
    v2 — but the field exists now so a binding written today keeps its signature
    when the outbox is switched on.
    """

    status: RunStatus
    artifacts: dict[str, Any] | None = None
    error: str | None = None
    outbox: tuple[Any, ...] = ()


def utcnow() -> datetime:
    return datetime.now(UTC)
