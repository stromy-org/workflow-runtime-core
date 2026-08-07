"""Client-neutral durable workflow lifecycle.

One lifecycle, three consumers. This package owns the versioned run registry,
explicit migrations, the schema-compatibility gate, the ``ExecutionBinding``
seam and the runner; consumers own their graphs, contracts and channels.

Import boundaries
-----------------
Everything exported here is in the BASE install and needs only ``psycopg``.
Graph execution lives in :mod:`workflow_runtime_core.executor` and requires the
``executor`` extra; broker adapters land in Phase B behind ``rabbitmq``. That
split is what lets the public workflow facade depend on this package without
acquiring LangGraph or aio-pika.
"""

from __future__ import annotations

from .binding import ExecutionBinding, LeaseRenewer
from .exceptions import (
    ActiveAttemptExists,
    CheckpointerError,
    DependencyError,
    LeaseLost,
    MigrationChecksumMismatch,
    MigrationError,
    RegistryError,
    RetryNotAllowed,
    SchemaVersionMismatch,
    StageFailure,
    WorkflowRuntimeCoreError,
)
from .migrations import (
    CORE_NAMESPACE,
    LATEST_VERSION,
    MIGRATIONS,
    Migration,
    apply_app_migrations,
    apply_migrations,
    pending,
    read_app_version,
    verify_ledger,
)
from .models import (
    TERMINAL_STATUS_VALUES,
    TERMINAL_STATUSES,
    RetentionCandidate,
    Run,
    RunRecord,
    RunStatus,
    TerminalProjection,
    utcnow,
)
from .schema import (
    SCHEMA_VERSION,
    SUPPORTED_SCHEMA_MAX,
    SUPPORTED_SCHEMA_MIN,
    read_schema_version,
    require_compatible_schema,
)

__version__ = "0.4.0"

__all__: list[str] = [
    "CORE_NAMESPACE",
    "LATEST_VERSION",
    "MIGRATIONS",
    "SCHEMA_VERSION",
    "SUPPORTED_SCHEMA_MAX",
    "SUPPORTED_SCHEMA_MIN",
    "TERMINAL_STATUSES",
    "TERMINAL_STATUS_VALUES",
    "CheckpointerError",
    "DependencyError",
    "ExecutionBinding",
    "LeaseLost",
    "LeaseRenewer",
    "Migration",
    "MigrationChecksumMismatch",
    "MigrationError",
    "RegistryError",
    "RetentionCandidate",
    "RetryNotAllowed",
    "Run",
    "RunRecord",
    "RunStatus",
    "SchemaVersionMismatch",
    "StageFailure",
    "TerminalProjection",
    "WorkflowRuntimeCoreError",
    "ActiveAttemptExists",
    "apply_app_migrations",
    "apply_migrations",
    "pending",
    "read_app_version",
    "read_schema_version",
    "require_compatible_schema",
    "utcnow",
    "verify_ledger",
]
