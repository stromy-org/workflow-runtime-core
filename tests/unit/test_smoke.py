"""Smoke test — module import, version, and the CLI's shape."""

from __future__ import annotations

import pytest
from click.testing import CliRunner

import workflow_runtime_core
from workflow_runtime_core.cli import main


@pytest.mark.unit
def test_module_imports() -> None:
    assert workflow_runtime_core.__name__ == "workflow_runtime_core"


@pytest.mark.unit
def test_module_has_version() -> None:
    assert hasattr(workflow_runtime_core, "__version__")
    assert isinstance(workflow_runtime_core.__version__, str)


@pytest.mark.unit
def test_cli_exposes_the_migrator_commands() -> None:
    result = CliRunner().invoke(main, ["--help"])
    assert result.exit_code == 0
    for command in ("migrate", "status", "list-migrations"):
        assert command in result.output


@pytest.mark.unit
def test_list_migrations_needs_no_database() -> None:
    """Inspection must work without a DSN — an operator checks what a build knows
    before pointing it at anything."""
    result = CliRunner().invoke(main, ["list-migrations"])
    assert result.exit_code == 0
    assert "run_registry_v1" in result.output
