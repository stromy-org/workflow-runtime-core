"""Opt-in, fixed-operation database capabilities for restricted receipt roles.

Provisioning is explicit through ``wrc receipt-capabilities-setup``. Runtime code
only calls individually granted functions; it cannot install them or fall back
to direct table writes. The wrapper borrows its caller's transaction/connection.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from importlib.resources import files
from typing import Any, LiteralString, cast

from psycopg import Cursor
from psycopg.types.json import Jsonb

from ..migrations import Migration
from ..registry import DbConnection
from ._receipt_sql import OPERATIONS

CAPABILITY_NAMESPACE = "wrc-receipt-capabilities"
CAPABILITY_VERSION = 1
CAPABILITY_NAMES = ("open", "select_due", "select_stale", *OPERATIONS)


def migrations() -> tuple[Migration, ...]:
    """Frozen checksum-ledger chain; merely loading it never executes SQL."""
    return (
        Migration(
            1,
            "fixed_receipt_capabilities_v1",
            files("workflow_runtime_core.messaging")
            .joinpath("receipt_capabilities_v1.sql")
            .read_text(encoding="utf-8"),
        ),
    )


def function_name(operation: str) -> str:
    """Closed operation names, suitable for an explicit per-role grant manifest."""
    if operation not in CAPABILITY_NAMES:
        raise ValueError("unknown receipt capability")
    return f"wrc_receipt_{operation.replace(':', '_')}_v{CAPABILITY_VERSION}"


@dataclass(frozen=True)
class ReceiptCapabilityConnection:
    """Borrowed native connection; no close, implicit commit, or DDL capability."""

    native: DbConnection

    def cursor(self) -> Cursor[dict[str, Any]]:
        return self.native.cursor()


def execute_capability(cur: Cursor[dict[str, Any]], operation: str, parameters: tuple[Any, ...]) -> None:
    name = function_name(operation)
    values = [
        json.loads(value) if index == len(parameters) - 1 and operation not in {"select_due", "select_stale"} else value
        for index, value in enumerate(parameters)
    ]
    cur.execute(
        cast(LiteralString, f"SELECT * FROM public.{name}(%s::jsonb)"),  # noqa: S608 - closed names above
        (Jsonb(values),),
    )
