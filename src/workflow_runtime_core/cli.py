"""``wrc`` — the migrator and inspection CLI.

This is the ONLY thing that issues DDL against a run registry. Applications call
:func:`workflow_runtime_core.require_compatible_schema` and nothing else, so a
schema upgrade is always a deliberate operator act with its own identity
(ORG-PLAN-155 locked decision 5) rather than a side effect of a process booting.

Run it with the migration role, not the application role: the application role
holds DML only, and that separation is what stops an application from silently
upgrading a schema other consumers are still reading.
"""

from __future__ import annotations

import json as _json

import click

from . import registry, schema
from .exceptions import MigrationChecksumMismatch
from .migrations import (
    LATEST_VERSION,
    MIGRATIONS,
    apply_migrations,
    ledger_exists,
    pending,
    verify_ledger,
)

_dsn_option = click.option(
    "--dsn",
    default=None,
    help="Postgres DSN (defaults to $STROMY_PG_DSN).",
)


@click.group()
@click.version_option()
def main() -> None:
    """Inspect and migrate a workflow run registry."""


@main.command()
@_dsn_option
@click.option(
    "--target",
    type=int,
    default=None,
    help=f"Migrate up to this version instead of the latest (v{LATEST_VERSION}).",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Report what would be applied and exit without touching the database.",
)
def migrate(dsn: str | None, target: int | None, dry_run: bool) -> None:
    """Apply pending migrations under a serialising advisory lock."""
    with registry.connect(dsn) as conn:
        live = schema.read_schema_version(conn)
        todo = [m for m in pending(live) if target is None or m.version <= target]

        if dry_run:
            click.echo(f"live:    {'unmigrated' if live is None else f'v{live}'}")
            click.echo(f"target:  v{LATEST_VERSION if target is None else target}")
            if todo:
                click.echo("pending:")
                for m in todo:
                    click.echo(f"  v{m.version}  {m.name}  sha256={m.checksum[:12]}")
            else:
                click.echo("pending: none — already current")
            click.echo("dry run — nothing applied")
            return

        applied = apply_migrations(conn, target=target)

    if todo:
        click.echo(f"migrated to v{applied} ({len(todo)} migration(s) applied)")
    else:
        click.echo(f"already at v{applied} — nothing to do")


@main.command()
@_dsn_option
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable output.")
def status(dsn: str | None, as_json: bool) -> None:
    """Report the live schema version and this build's supported range.

    Exits non-zero when the live schema is not servable, so a readiness probe can
    key off the exit code without parsing anything.
    """
    with registry.connect(dsn) as conn:
        live = schema.read_schema_version(conn)
        todo = pending(live)

        # Report ledger health without serving through it: a mismatch must be
        # visible to an operator running `wrc status`, not only to a crashing app.
        if not ledger_exists(conn):
            ledger = "absent (pre-ledger; next migrate backfills it)"
            ledger_ok = live is None or live <= 1
        else:
            try:
                verify_ledger(conn, applied_through=live)
                ledger = "ok"
                ledger_ok = True
            except MigrationChecksumMismatch as exc:
                ledger = f"MISMATCH — {exc}"
                ledger_ok = False

    compatible = ledger_ok and live is not None and (
        schema.SUPPORTED_SCHEMA_MIN <= live <= schema.SUPPORTED_SCHEMA_MAX
    )
    if as_json:
        click.echo(
            _json.dumps(
                {
                    "live_version": live,
                    "latest_known_version": LATEST_VERSION,
                    "supported_range": [
                        schema.SUPPORTED_SCHEMA_MIN,
                        schema.SUPPORTED_SCHEMA_MAX,
                    ],
                    "compatible": compatible,
                    "ledger": ledger,
                    "pending": [{"version": m.version, "name": m.name} for m in todo],
                },
                indent=2,
            )
        )
    else:
        click.echo(f"live schema:      {'unmigrated' if live is None else f'v{live}'}")
        click.echo(
            f"supported range:  [v{schema.SUPPORTED_SCHEMA_MIN}, "
            f"v{schema.SUPPORTED_SCHEMA_MAX}]"
        )
        click.echo(f"compatible:       {'yes' if compatible else 'NO'}")
        click.echo(f"ledger:           {ledger}")
        click.echo(
            "pending:          "
            + (", ".join(f"v{m.version} {m.name}" for m in todo) if todo else "none")
        )
    if not compatible:
        raise SystemExit(1)


@main.command("list-migrations")
def list_migrations() -> None:
    """List every migration this build knows, with its checksum."""
    for m in MIGRATIONS:
        click.echo(f"v{m.version}  {m.name}  sha256={m.checksum}")


if __name__ == "__main__":  # pragma: no cover
    main()
