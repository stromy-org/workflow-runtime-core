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

# Role names are ARGUMENTS, never constants. This package is consumed by an
# estate whose roles it must not encode and by generated services that have
# none, so both options default to None (meaning "this deployment does not
# separate ownership") and fall back to environment variables rather than to a
# value. auth.validate_identifier refuses anything that is not a plain SQL
# identifier, because these strings land in SET ROLE and GRANT, which take no
# parameters.
_owner_role_option = click.option(
    "--owner-role",
    default=None,
    help=(
        "Role to SET ROLE before migrating, so objects are owned by it rather than by "
        "the login (defaults to $WRC_PG_OWNER_ROLE). Elevation happens BEFORE the "
        "ledger is read."
    ),
)

_application_role_option = click.option(
    "--application-role",
    default=None,
    help=(
        "Role to reconcile this chain's grants for after migrating (defaults to "
        "$WRC_PG_APPLICATION_ROLE). Operational tables get DML, migration ledgers "
        "get SELECT only."
    ),
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
@_owner_role_option
@_application_role_option
def migrate(
    dsn: str | None,
    target: int | None,
    dry_run: bool,
    owner_role: str | None,
    application_role: str | None,
) -> None:
    """Apply pending migrations under a serialising advisory lock.

    With --owner-role, the session elevates before reading anything, so this
    fails with MigrationRoleRequired when run by a principal that only holds DML
    — even against an already-current ledger, where the lazy check would have
    reported success for a migration nobody could have performed.

    With --application-role, the core chain's grant manifest is reconciled after
    migrating: operational tables get DML, the two migration ledgers get SELECT
    only. Doing it in the same command is what makes it hard to add a table and
    forget to grant on it.
    """
    from .auth import resolve_application_role, resolve_owner_role
    from .grants import CORE_MANIFEST, reconcile

    owner = resolve_owner_role(owner_role)
    application = resolve_application_role(application_role)

    with registry.connect(dsn) as conn:
        live = schema.read_schema_version(conn)
        todo = [m for m in pending(live) if target is None or m.version <= target]

        if dry_run:
            click.echo(f"live:    {'unmigrated' if live is None else f'v{live}'}")
            click.echo(f"target:  v{LATEST_VERSION if target is None else target}")
            click.echo(f"owner:   {owner or '(none — migrating as the connected login)'}")
            click.echo(f"app:     {application or '(none — no grants reconciled)'}")
            if todo:
                click.echo("pending:")
                for m in todo:
                    click.echo(f"  v{m.version}  {m.name}  sha256={m.checksum[:12]}")
            else:
                click.echo("pending: none — already current")
            click.echo("dry run — nothing applied")
            return

        applied = apply_migrations(conn, target=target, owner_role=owner)

        granted: list[str] = []
        if application:
            # Inside the same transaction and the same elevation as the
            # migration: the grants must be made by the owner, and a separate
            # connection would have lost the SET LOCAL ROLE.
            granted = reconcile(conn, CORE_MANIFEST, application_role=application)

    if todo:
        click.echo(f"migrated to v{applied} ({len(todo)} migration(s) applied)")
    else:
        click.echo(f"already at v{applied} — nothing to do")
    if granted:
        click.echo(f"reconciled {len(granted)} grant(s) for {application}")


def _checkpoint_head(conn: object) -> int | str | None:
    """Applied version of the LangGraph checkpoint chain, without importing it.

    Read as a plain catalog query rather than through langgraph, so `wrc status`
    works on a base install: the facade depends on this package precisely
    because it does NOT want LangGraph, and a status command that needed the
    executor extra to answer would be useless to it.
    """
    with conn.cursor() as cur:  # type: ignore[attr-defined]
        cur.execute("SELECT to_regclass('public.checkpoint_migrations') AS r")
        row = cur.fetchone()
        present = row is not None and (row.get("r") if isinstance(row, dict) else row[0]) is not None
        if not present:
            return "absent (run `wrc checkpoint-setup`)"
        cur.execute("SELECT max(v) AS v FROM checkpoint_migrations")
        row = cur.fetchone()
        value = (row.get("v") if isinstance(row, dict) else row[0]) if row else None
    return int(value) if value is not None else None


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
        # The checkpoint store is a THIRD migration chain sharing this database,
        # and until now nothing reported it. An operator asking "is this
        # deployment migrated?" needs one answer covering every chain, or the
        # one that is behind is the one nobody looked at.
        checkpoint = _checkpoint_head(conn)

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
                    "checkpoint": checkpoint,
                    "pending": [{"version": m.version, "name": m.name} for m in todo],
                },
                indent=2,
            )
        )
    else:
        click.echo(f"live schema:      {'unmigrated' if live is None else f'v{live}'}")
        click.echo(f"checkpoint store: {checkpoint}")
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


@main.command("checkpoint-setup")
@_dsn_option
@_owner_role_option
@_application_role_option
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable output.")
def checkpoint_setup(
    dsn: str | None,
    owner_role: str | None,
    application_role: str | None,
    as_json: bool,
) -> None:
    """Create or upgrade the LangGraph checkpoint store, then grant on it.

    LangGraph's ``setup()`` is a MIGRATION: it runs schema SQL. Leaving it to the
    runtime means either an application holding DDL privilege — the thing this
    separation removes — or one replica migrating a store its siblings are
    mid-read of. So it becomes a deployment step, run by the migration operator,
    and the runtime is set to WRC_CHECKPOINT_SETUP=verify.

    The checkpoint chain owns its own grant manifest for the same reason the core
    chain does: only the chain that creates a table can say whether it holds
    operational rows or migration history. checkpoint_migrations is history, so
    the application reads it and never writes it.
    """
    from .auth import resolve_application_role, resolve_owner_role, set_role
    from .executor.checkpointer import expected_checkpoint_version
    from .grants import CHECKPOINT_MANIFEST, reconcile

    owner = resolve_owner_role(owner_role)
    application = resolve_application_role(application_role)

    try:
        from langgraph.checkpoint.postgres import PostgresSaver
    except ImportError:  # pragma: no cover - dependency wiring
        raise click.ClickException(
            "langgraph-checkpoint-postgres is not installed. Install with: "
            "uv pip install 'workflow-runtime-core[executor]'"
        ) from None

    # autocommit, because LangGraph's setup() issues CREATE INDEX CONCURRENTLY,
    # which PostgreSQL refuses inside a transaction block. That in turn is why
    # the elevation below is a SESSION-level SET ROLE: SET LOCAL would apply to
    # a transaction that ends with the statement itself.
    with registry.connect(dsn, autocommit=True) as conn:
        if owner:
            # Before setup(), so the tables it creates are owned by the owner
            # role rather than by whichever login the operator authenticated as.
            # An operator-owned checkpoint table is exactly the
            # ownership-follows-identity coupling this plan exists to break.
            set_role(conn, owner, local=False)

        try:
            saver = PostgresSaver(conn)  # type: ignore[arg-type]
            saver.setup()
            reached = expected_checkpoint_version(saver)

            granted: list[str] = []
            if application:
                granted = reconcile(conn, CHECKPOINT_MANIFEST, application_role=application)
        finally:
            if owner:
                with conn.cursor() as cur:
                    cur.execute("RESET ROLE")

    if as_json:
        click.echo(
            _json.dumps(
                {
                    "checkpoint_version": reached,
                    "owner_role": owner,
                    "application_role": application,
                    "grants_applied": len(granted),
                },
                indent=2,
            )
        )
        return
    click.echo(f"checkpoint store set up (v{reached})")
    click.echo(f"owner:  {owner or '(none — owned by the connected login)'}")
    if granted:
        click.echo(f"reconciled {len(granted)} grant(s) for {application}")
    else:
        click.echo("grants:  none reconciled (no --application-role)")


@main.command("auth-probe")
@_dsn_option
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable output.")
def auth_probe(dsn: str | None, as_json: bool) -> None:
    """Report who this process authenticates as, and what it may do.

    The command a canary runs inside a live consumer to answer the only question
    that matters after rotating it: is this workload still connecting as the
    local administrator, or as its own Entra principal with the application
    capability and nothing more?

    Read-only, and it never prints a token, a password or a DSN — it is meant to
    be safe to run in production and to leave its output in a job log.

    Exits non-zero when the principal can issue DDL, because for a runtime
    consumer that is a finding, not a status: an application that can CREATE in
    the schema can shadow a table the rest of the estate reads. An operator
    session legitimately trips this, which is why the reason is printed rather
    than merely signalled by the exit code.
    """
    from .auth import describe_session, resolve_auth_mode

    mode = resolve_auth_mode()
    with registry.connect(dsn) as conn:
        info = describe_session(conn)

    memberships = list(info.get("memberships") or [])
    can_ddl = bool(info.get("schema_create"))

    if as_json:
        click.echo(
            _json.dumps(
                {
                    "auth_mode": mode.value,
                    "current_user": info["current_user"],
                    "session_user": info["session_user"],
                    "database": info["database"],
                    "memberships": memberships,
                    "can_connect": info["can_connect"],
                    "schema_usage": info["schema_usage"],
                    "can_create_in_schema": can_ddl,
                    "ddl_denied": not can_ddl,
                },
                indent=2,
            )
        )
    else:
        click.echo(f"auth mode:     {mode.value}")
        click.echo(f"principal:     {info['current_user']}")
        click.echo(f"session user:  {info['session_user']}")
        click.echo(f"database:      {info['database']}")
        click.echo(f"memberships:   {', '.join(memberships) if memberships else '(none)'}")
        click.echo(f"can connect:   {'yes' if info['can_connect'] else 'NO'}")
        click.echo(f"schema usage:  {'yes' if info['schema_usage'] else 'NO'}")
        click.echo(f"DDL denied:    {'yes' if not can_ddl else 'NO — this principal can CREATE'}")

    if can_ddl:
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
