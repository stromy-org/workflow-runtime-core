"""Exceptions raised by workflow_runtime_core.

Every failure here is LOUD. There is no local-file fallback and no silent
degrade: a runtime that cannot reach its registry must not pretend to have run,
and a consumer compiled against a schema it does not understand must not serve.
"""

from __future__ import annotations


class WorkflowRuntimeCoreError(Exception):
    """Base exception for workflow_runtime_core."""


class RegistryError(WorkflowRuntimeCoreError, RuntimeError):
    """Registry could not be reached or used. Never swallowed into a fallback.

    Also subclasses ``RuntimeError`` so consumers that historically caught
    ``RuntimeError`` around the extracted Stromy registry keep working unchanged.
    """


class SchemaVersionMismatch(RegistryError):
    """The live schema is outside the caller's supported range.

    Raised loudly on purpose: a facade serving against the wrong schema is how
    silent data corruption starts.
    """


class ActiveAttemptExists(RegistryError):
    """A workspace already carries a queued/running/paused attempt.

    Enforced by a partial unique index, not by convention: two live attempts on
    one mutable folder interleave writes into each other's stage outputs, and no
    application-level care survives a crash at the wrong moment.
    """


class LeaseLost(WorkflowRuntimeCoreError, RuntimeError):
    """This runner's single-writer lease expired or was taken over.

    Deliberately NOT a :class:`RegistryError`: nothing went wrong with the
    registry, and nothing about this run has failed. Another runner has already
    been told it may claim the run, so the *outcome* now belongs to that process.
    A runner that sees this must stop and record nothing — writing a terminal
    status here would overwrite a result this process cannot see.
    """


class CheckpointerError(WorkflowRuntimeCoreError, RuntimeError):
    """Checkpointer could not be constructed or verified."""


class MigrationError(WorkflowRuntimeCoreError, RuntimeError):
    """A migration could not be applied, or the recorded history is inconsistent."""


class MigrationChecksumMismatch(MigrationError):
    """The ledger's recorded history contradicts this build's migrations.

    This is the fail-closed signal the ``schema_migrations`` ledger exists to
    produce (ORG-191): a database whose applied migration N has a different
    checksum than this build's migration N was migrated by a *different*
    definition of N. Serving against it means silently misreading a shape the
    version number cannot distinguish — the exact failure mode of the
    ORG-PLAN-155/164 schema-v2 fork. Never caught and continued past; the only
    recoveries are deploying the build whose history matches, or an explicit,
    human-reviewed ledger repair.
    """


class DependencyError(WorkflowRuntimeCoreError, ImportError):
    """An optional dependency is missing.

    Raised when an optional-extra feature is used but the required dependency
    isn't installed. The message tells the caller exactly which extra to install.
    """

    def __init__(self, extra: str, package: str) -> None:
        super().__init__(
            f"Missing optional dependency {package!r}. Install with: "
            f"uv pip install 'workflow-runtime-core[{extra}]'"
        )
        self.extra = extra
        self.package = package
