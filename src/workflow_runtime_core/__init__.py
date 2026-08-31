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

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _metadata_version

from .auth import (
    AuthConfigurationError,
    AuthMode,
    CheckpointSetupMode,
    CredentialSource,
    describe_session,
    resolve_application_role,
    resolve_auth_mode,
    resolve_checkpoint_setup,
    resolve_owner_role,
    validate_identifier,
)
from .binding import ExecutionBinding, LeaseRenewer, ScopedExecutionBinding
from .exceptions import (
    ActiveAttemptExists,
    CheckpointerError,
    CheckpointStoreOutdated,
    DependencyError,
    ExecutionMetadataConflict,
    LeaseLost,
    MigrationChecksumMismatch,
    MigrationError,
    MigrationRoleRequired,
    RegistryError,
    RetryNotAllowed,
    SchemaVersionMismatch,
    StageFailure,
    WorkflowRuntimeCoreError,
)
from .grants import (
    CHECKPOINT_MANIFEST,
    CORE_MANIFEST,
    GrantManifest,
    missing_privileges,
)
from .migrations import (
    CORE_NAMESPACE,
    LATEST_VERSION,
    MIGRATIONS,
    Migration,
    apply_app_migrations,
    apply_migrations,
    assert_may_migrate,
    pending,
    read_app_version,
    verify_ledger,
)
from .models import (
    PUBLIC_EXECUTION_METADATA_KEYS,
    TERMINAL_STATUS_VALUES,
    TERMINAL_STATUSES,
    RetentionCandidate,
    Run,
    RunRecord,
    RunStatus,
    TerminalProjection,
    public_execution_metadata,
    utcnow,
)
from .schema import (
    SCHEMA_VERSION,
    SUPPORTED_SCHEMA_MAX,
    SUPPORTED_SCHEMA_MIN,
    read_schema_version,
    require_compatible_schema,
)

# Read from the installed distribution rather than restated here. The hardcoded
# copy had drifted to 0.4.0 while the package shipped 0.6.0 — a version string
# that lies is worse than none, because it is exactly what someone reads when
# working out which build a production replica is running.
try:
    __version__ = _metadata_version("workflow-runtime-core")
except PackageNotFoundError:  # pragma: no cover - running from a source tree
    __version__ = "0.0.0.dev0"

__all__: list[str] = [
    "CHECKPOINT_MANIFEST",
    "CORE_MANIFEST",
    "CORE_NAMESPACE",
    "LATEST_VERSION",
    "PUBLIC_EXECUTION_METADATA_KEYS",
    "MIGRATIONS",
    "SCHEMA_VERSION",
    "SUPPORTED_SCHEMA_MAX",
    "SUPPORTED_SCHEMA_MIN",
    "TERMINAL_STATUSES",
    "TERMINAL_STATUS_VALUES",
    "AuthConfigurationError",
    "AuthMode",
    "CheckpointSetupMode",
    "CheckpointStoreOutdated",
    "CheckpointerError",
    "CredentialSource",
    "DependencyError",
    "ExecutionBinding",
    "GrantManifest",
    "ExecutionMetadataConflict",
    "LeaseLost",
    "LeaseRenewer",
    "Migration",
    "MigrationChecksumMismatch",
    "MigrationError",
    "MigrationRoleRequired",
    "RegistryError",
    "RetentionCandidate",
    "RetryNotAllowed",
    "Run",
    "RunRecord",
    "RunStatus",
    "SchemaVersionMismatch",
    "ScopedExecutionBinding",
    "StageFailure",
    "TerminalProjection",
    "WorkflowRuntimeCoreError",
    "ActiveAttemptExists",
    "apply_app_migrations",
    "apply_migrations",
    "assert_may_migrate",
    "describe_session",
    "missing_privileges",
    "pending",
    "public_execution_metadata",
    "read_app_version",
    "read_schema_version",
    "require_compatible_schema",
    "resolve_application_role",
    "resolve_auth_mode",
    "resolve_checkpoint_setup",
    "resolve_owner_role",
    "utcnow",
    "validate_identifier",
    "verify_ledger",
]
