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


class CheckpointerError(WorkflowRuntimeCoreError, RuntimeError):
    """Checkpointer could not be constructed or verified."""


class MigrationError(WorkflowRuntimeCoreError, RuntimeError):
    """A migration could not be applied, or the recorded history is inconsistent."""


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
