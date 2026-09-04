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


class ExecutionMetadataConflict(RegistryError):
    """A run's server-derived execution snapshot was already pinned differently.

    The snapshot is written once, at run creation, and is then immutable for the
    life of the run and every attempt of it. This is raised when something tries
    to pin a *different* snapshot over an existing one — re-pinning an identical
    snapshot is a no-op, so an idempotent create path retries safely.

    Immutability is the whole point of the column. Requirements and entitlements
    are editable by design, so a runner that recomputed them per attempt would
    let an edit made between a failure and its retry move who pays for the second
    attempt — silently, and without the client ever approving it. Raising here
    means that attempt is refused rather than repriced.
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


class MigrationRoleRequired(MigrationError):
    """The connected principal could not assume the migration owner role.

    Raised BEFORE the ledger is read, and that ordering is the whole point. The
    application role usually finds the ledger already current, so a migration
    command that checked privileges lazily would take the "nothing to do" path
    and exit 0 — reporting success for an operation it was never able to
    perform. The next release then appears to have migrated when nothing did.

    Recovering means connecting as a principal that holds the migration
    capability, not weakening the role: an application that can migrate is the
    condition this separation exists to remove.
    """

    def __init__(self, role: str, detail: str) -> None:
        super().__init__(
            f"cannot assume the migration owner role {role!r}: {detail}. "
            f"Migrations run as an operator holding the migration capability, never "
            f"as the application role — connect with a principal that may SET ROLE "
            f"{role!r}."
        )
        self.role = role
        self.detail = detail


class CheckpointStoreOutdated(CheckpointerError):
    """The checkpoint store needs a migration this process may not apply.

    The runtime opens the checkpointer in ``verify`` mode, so a store that is
    absent or behind is a deployment step that has not happened — not something
    to fix in-process. Applying it here would mean the application issuing DDL,
    which is exactly the privilege this separation removes, and it would let one
    replica migrate a store its siblings are mid-read of.

    Raised before any work is claimed, so a run fails fast rather than pausing
    into a store that cannot record it.
    """

    def __init__(self, detail: str, *, command: str = "wrc checkpoint-setup") -> None:
        super().__init__(
            f"{detail} Run `{command}` as the migration operator before starting the "
            f"runtime; applications never migrate their own checkpoint store."
        )
        self.detail = detail
        self.command = command


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
