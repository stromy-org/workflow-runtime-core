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


class RetryNotAllowed(RegistryError):
    """This run cannot be the parent of a new attempt.

    Distinct from :class:`ActiveAttemptExists`, which is about the *workspace*
    already being busy. This one is about the run itself: only a ``failed`` run
    is retryable. A ``completed`` one already produced its outputs, a
    ``cancelled`` one was stopped deliberately, and a live one has not finished —
    retrying any of those would mint a second attempt against work that is either
    fine or still moving.

    A resume is the *other* operation, and the difference is not cosmetic: a
    resume continues the same LangGraph thread from its checkpoint, while a retry
    starts a new thread on the same workspace. Offering retry as a way to nudge a
    paused run would silently abandon its checkpoint.
    """


class StageFailure(WorkflowRuntimeCoreError, RuntimeError):
    """A binding-raised failure that names the pipeline stage it happened in.

    The core labels the stages it drives itself (``graph``, ``inputs``,
    ``context``, ``artifacts``), but a binding often knows a sharper answer: a
    durable share that is not mounted and an uploaded file that fails its digest
    both surface while building inputs, and they are not the same incident — one
    is infrastructure, one is data, and an operator triages them differently.
    Raise this to say which; the runner records the given ``stage`` verbatim.

    Any exception carrying a ``.stage`` attribute works — this class is just the
    obvious way to make one.
    """

    def __init__(self, stage: str, message: str) -> None:
        super().__init__(message)
        self.stage = stage


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
