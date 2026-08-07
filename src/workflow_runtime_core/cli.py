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


_namespace_option = click.option(
    "--namespace",
    required=True,
    help="Service namespace to operate on (immutable per service).",
)


@main.command()
@_dsn_option
def reconcile(dsn: str | None) -> None:
    """Recover work stranded by a crashed dispatcher, publisher or sender.

    Each of the three has a different correct recovery, which is why this is one
    command and not a loop over a generic 'stuck' state:

    * a stranded LAUNCH goes back to pending — it may never have started;
    * a stranded PUBLISH goes back to the retry schedule — a redelivery of the
      same stable message id is survivable;
    * a stranded SEND becomes ``uncertain`` — the provider may already have it,
      so retrying could double-send to a real person.
    """
    from .messaging import launches, outbox, receipts

    with registry.connect(dsn) as conn:
        relaunched = launches.reconcile_stale(conn)
        republished = outbox.reconcile_stale(conn)
        unresolved = receipts.reconcile_stale(conn)

    click.echo(f"launches returned to pending:   {len(relaunched)}")
    click.echo(f"outbox rows returned to retry:  {len(republished)}")
    click.echo(f"sends marked uncertain:         {len(unresolved)}")
    if unresolved:
        click.echo(
            "\nThose sends are NOT retried automatically — their provider outcome is "
            "unobservable. Review them with `wrc uncertain --namespace <ns>` and settle "
            "each against the provider's own record."
        )


@main.command()
@_dsn_option
@_namespace_option
@click.option("--limit", type=int, default=100, show_default=True)
def uncertain(dsn: str | None, namespace: str, limit: int) -> None:
    """List deliveries whose provider outcome could not be observed.

    Exits non-zero when the list is non-empty so a monitoring job can alert on
    it directly. A non-empty worklist is not an error in the system; it is work
    owed to a human, and the exit code says so.
    """
    from .messaging import receipts

    with registry.connect(dsn) as conn:
        rows = receipts.list_uncertain(conn, service_namespace=namespace, limit=limit)

    if not rows:
        click.echo("no uncertain deliveries")
        return
    click.echo(f"{len(rows)} uncertain delivery(ies) in {namespace!r}:")
    for r in rows:
        click.echo(
            f"  {r.updated_at:%Y-%m-%d %H:%M}  {r.destination}  {r.message_id}  "
            f"attempts={r.attempts}  {r.last_error or ''}"
        )
    raise SystemExit(1)


@main.command()
@_dsn_option
@_namespace_option
@click.option(
    "--older-than-days",
    type=int,
    default=30,
    show_default=True,
    help="Retention window. Per-client overrides may only SHORTEN this.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Report what would be deleted and exit without deleting anything.",
)
def purge(dsn: str | None, namespace: str, older_than_days: int, dry_run: bool) -> None:
    """Delete inbox/outbox payloads past the retention window.

    Deletion order is dependency order, and the predicates are deliberately
    narrow: inbox rows go only for runs that actually reached a terminal state
    (a paused run waits on a human far longer than any retention window and
    still needs its envelope to resume), and outbox rows go only once
    ``delivered``. Counts only — no message bodies are emitted, because this
    output goes to logs.
    """
    from .messaging import inbox, outbox

    with registry.connect(dsn) as conn:
        inbox_rows = inbox.purge_inbox(
            conn,
            service_namespace=namespace,
            older_than_days=older_than_days,
            dry_run=dry_run,
        )
        outbox_rows = outbox.purge_delivered(
            conn,
            service_namespace=namespace,
            older_than_days=older_than_days,
            dry_run=dry_run,
        )

    verb = "would delete" if dry_run else "deleted"
    click.echo(f"{verb} {inbox_rows} inbox row(s) and {outbox_rows} outbox row(s)")
    if dry_run:
        click.echo("dry run — nothing deleted")


@main.command("outbox-status")
@_dsn_option
@_namespace_option
def outbox_status(dsn: str | None, namespace: str) -> None:
    """Report undelivered depth and the age of the oldest owed message.

    Age, not just depth, is the number that catches a stuck lane: a steady depth
    of five is healthy throughput, while a depth of one that is four hours old
    is an outage.
    """
    from .messaging import outbox

    with registry.connect(dsn) as conn:
        depth = outbox.pending_depth(conn, service_namespace=namespace)
        age = outbox.oldest_pending_age_seconds(conn, service_namespace=namespace)

    click.echo(f"undelivered:  {depth}")
    click.echo(f"oldest age:   {'-' if age is None else f'{age:.0f}s'}")


if __name__ == "__main__":  # pragma: no cover
    main()
