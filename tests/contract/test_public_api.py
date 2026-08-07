"""Public-API contract — guards what consumers can import.

The dependency-boundary test here is the mechanical half of ORG-PLAN-155
acceptance criterion 1 ("the workflow MCP base install contains neither LangGraph
nor RabbitMQ"). It asserts the property at the point it is easy to break —
someone adding a convenience re-export to ``__init__`` — rather than only in a
consumer's lockfile, where the regression surfaces as a mysterious image-size
jump months later.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

import workflow_runtime_core


@pytest.mark.contract
def test_all_is_a_list() -> None:
    assert isinstance(workflow_runtime_core.__all__, list)


@pytest.mark.contract
def test_all_symbols_are_exported() -> None:
    for symbol in workflow_runtime_core.__all__:
        assert hasattr(workflow_runtime_core, symbol), (
            f"__all__ lists {symbol!r} but it's not exported"
        )


@pytest.mark.contract
def test_lifecycle_surface_is_exported() -> None:
    """The symbols consumers actually bind against."""
    for symbol in (
        "RunRecord",
        "RunStatus",
        "TerminalProjection",
        "ExecutionBinding",
        "RegistryError",
        "SchemaVersionMismatch",
        "require_compatible_schema",
        "apply_migrations",
        # WS5: retry lineage and ordered retention are part of the shared surface
        # for the same reason the rest is — both consumers need them, and a second
        # implementation of "what a retry is" is how the schema forked.
        "RetryNotAllowed",
        "RetentionCandidate",
    ):
        assert symbol in workflow_runtime_core.__all__


@pytest.mark.contract
def test_base_import_does_not_pull_langgraph_or_aio_pika() -> None:
    """Importing the base package must not drag in the optional extras.

    Run in a subprocess: this pytest session may legitimately have LangGraph
    imported by an executor test, which would mask the regression.
    """
    code = (
        "import sys;"
        "import workflow_runtime_core;"
        "import workflow_runtime_core.registry;"
        "import workflow_runtime_core.migrations;"
        "import workflow_runtime_core.schema;"
        "import workflow_runtime_core.binding;"
        # messaging is BASE install: ingress, outbox and receipt logic are pure
        # DML, and only the broker transport needs aio-pika. That split is the
        # reason the workflow facade can consume this package without acquiring
        # a broker client, so it is asserted rather than assumed.
        "import workflow_runtime_core.messaging;"
        "import workflow_runtime_core.messaging.egress;"
        "leaked=[m for m in sys.modules if m.split('.')[0] in {'langgraph','aio_pika'}];"
        "print(','.join(sorted(leaked)))"
    )
    out = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
    )
    leaked = out.stdout.strip()
    assert leaked == "", f"base import leaked optional-extra modules: {leaked}"


@pytest.mark.contract
def test_binding_protocol_is_runtime_checkable() -> None:
    """Consumers duck-type their binding; the protocol must accept that."""

    class _Binding:
        async def resolve_graph(self, workflow: str) -> object: ...
        async def build_input(self, run: object) -> object: ...
        async def build_context(self, run: object) -> object: ...
        async def project_terminal(self, run: object, snapshot: object) -> object: ...

    assert isinstance(_Binding(), workflow_runtime_core.ExecutionBinding)


@pytest.mark.contract
def test_incomplete_binding_is_rejected() -> None:
    class _Partial:
        async def resolve_graph(self, workflow: str) -> object: ...

    assert not isinstance(_Partial(), workflow_runtime_core.ExecutionBinding)


@pytest.mark.contract
def test_lease_renewer_is_duck_typeable() -> None:
    """A consumer's renewer couples its transport to the registry lease; the core
    must accept it structurally rather than by inheritance."""

    class _Renewer:
        interval_seconds = 30.0

        async def renew(self) -> bool: ...

    assert isinstance(_Renewer(), workflow_runtime_core.LeaseRenewer)


@pytest.mark.contract
def test_lease_surface_is_exported() -> None:
    for symbol in ("LeaseRenewer", "LeaseLost"):
        assert symbol in workflow_runtime_core.__all__
