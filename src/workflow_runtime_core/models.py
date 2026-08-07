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

    The v2 data-plane fields (workspace/attempt lineage, dispatch lease,
    progress) are defaulted optionals read through ``.get``: this same reader
    runs against a v1 database during the expansion window, where none of those
    columns exist. Column names follow the 2026-08-06 fork resolution —
    ``lease_expires_at``, ``attempt_no``/``retry_of`` — one shape, one number.
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
    # --- v2: workflow data plane (ORG-PLAN-164) ------------------------------
    workspace_id: str | None = None
    retry_of: str | None = None
    attempt_no: int = 1
    dispatch_id: str | None = None
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    heartbeat_at: datetime | None = None
    progress_json: dict[str, Any] | None = None
    error_json: dict[str, Any] | None = None
    delivery_count: int = 0
    artifacts_published_at: datetime | None = None
    input_set_id: str | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> RunRecord:
        def _uuid(name: str) -> str | None:
            value = row.get(name)
            return str(value) if value is not None else None

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
            workspace_id=_uuid("workspace_id"),
            retry_of=_uuid("retry_of"),
            attempt_no=row.get("attempt_no") or 1,
            dispatch_id=_uuid("dispatch_id"),
            lease_owner=row.get("lease_owner"),
            lease_expires_at=row.get("lease_expires_at"),
            heartbeat_at=row.get("heartbeat_at"),
            progress_json=row.get("progress_json"),
            error_json=row.get("error_json"),
            delivery_count=row.get("delivery_count") or 0,
            artifacts_published_at=row.get("artifacts_published_at"),
            input_set_id=_uuid("input_set_id"),
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

    ``outbox`` is still empty: the transactional outbox arrives with the
    messaging chain (ORG-PLAN-155 Phase B, migration ``0003``), not with the
    data plane's ``0002``. The field exists now so a binding written today keeps
    its signature when the outbox is switched on.
    """

    status: RunStatus
    artifacts: dict[str, Any] | None = None
    error: str | None = None
    outbox: tuple[Any, ...] = ()
    #: Whether the binding already published this run's declared exports to
    #: durable storage. Threaded into ``mark_completed`` so the status flip and
    #: the publication stamp land in ONE statement: a client that saw
    #: ``completed`` before the descriptors were written would poll a finished
    #: run and find nothing to download. Requires schema v2 — the registry
    #: refuses the stamp on v1 rather than dropping it silently, because a
    #: dropped stamp lies to the maintenance pass about what is in the container.
    artifacts_published: bool = False


def utcnow() -> datetime:
    return datetime.now(UTC)
