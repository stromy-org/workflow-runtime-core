"""Role separation, against a real PostgreSQL.

Three claims are only true if a database agrees with them, so none of this is
unit-testable:

1. ``wrc migrate`` run by an application principal FAILS — even when the ledger
   is already current, which is the case that used to exit 0 having done
   nothing;
2. the grant manifests, applied for real, leave every migration ledger
   read-only and every operational table writable;
3. the checkpointer's ``verify`` mode refuses an absent or stale store instead
   of migrating it.

The fixture builds the estate's shape in miniature — a NOLOGIN capability role
that a login inherits — rather than testing as a superuser, because a superuser
bypasses every ACL and would make every negative assertion here pass vacuously.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import psycopg
import pytest

from workflow_runtime_core import registry
from workflow_runtime_core.exceptions import MigrationRoleRequired
from workflow_runtime_core.grants import (
    CHECKPOINT_MANIFEST,
    CORE_MANIFEST,
    missing_privileges,
    reconcile,
)
from workflow_runtime_core.migrations import apply_migrations

pytestmark = pytest.mark.integration

#: Per-process suffix, for the same reason conftest's blank_dsn has one: role
#: names are CLUSTER-scoped, so a counter restarting each session would collide
#: with the previous run's leftovers on a long-lived server.
_RUN_ID = uuid.uuid4().hex[:8]

PASSWORD = "wrc-test-pw"  # noqa: S105 - a throwaway fixture credential


@dataclass(frozen=True)
class Separated:
    """One test's isolated owner/app world."""

    admin_dsn: str
    app_dsn: str
    owner: str
    app: str


def _dsn_as(dsn: str, user: str, password: str) -> str:
    """Rewrite a DSN's credentials, keeping host/port/database."""
    info = psycopg.conninfo.conninfo_to_dict(dsn)
    info["user"] = user
    info["password"] = password
    return psycopg.conninfo.make_conninfo(**info)


@pytest.fixture
def separated(blank_dsn: str, request: pytest.FixtureRequest) -> Separated:
    """A migrated database with owner/app roles, exactly as the estate has them.

    Role names are UNIQUE PER TEST because roles are cluster-scoped, not
    database-scoped: ``blank_dsn`` hands out a fresh database, which resets
    nothing about who exists on the server, so fixed names collide with the
    previous test's leftovers from the second test onward.
    """
    suffix = f"{_RUN_ID}_{abs(hash(request.node.nodeid)) % 10**8}"
    owner, app, login = f"wrc_owner_{suffix}", f"wrc_app_{suffix}", f"wrc_login_{suffix}"
    dbname = psycopg.conninfo.conninfo_to_dict(blank_dsn)["dbname"]

    with psycopg.connect(blank_dsn, autocommit=True) as conn:
        conn.execute(f"CREATE ROLE {owner} NOLOGIN NOINHERIT")  # noqa: S608 - generated identifier
        conn.execute(f"CREATE ROLE {app} NOLOGIN INHERIT")  # noqa: S608 - generated identifier
        conn.execute(f"CREATE ROLE {login} LOGIN PASSWORD '{PASSWORD}'")  # noqa: S608 - generated identifier
        conn.execute(f"GRANT {app} TO {login} WITH INHERIT TRUE, SET FALSE")  # noqa: S608
        conn.execute(f"GRANT {owner} TO CURRENT_USER WITH INHERIT FALSE, SET TRUE")  # noqa: S608
        # The NEW owner needs CREATE on the schema before anything can be given
        # to it: ALTER ... OWNER TO checks the schema privilege of the new
        # owner, not only of the caller.
        conn.execute(f"GRANT CREATE, USAGE ON SCHEMA public TO {owner}")  # noqa: S608
        conn.execute(f"GRANT CONNECT ON DATABASE {dbname} TO {app}")  # noqa: S608
        conn.execute(f"GRANT USAGE ON SCHEMA public TO {app}")  # noqa: S608

    with registry.connect(blank_dsn) as conn:
        apply_migrations(conn, owner_role=owner)
        reconcile(conn, CORE_MANIFEST, application_role=app)

    return Separated(
        admin_dsn=blank_dsn,
        app_dsn=_dsn_as(blank_dsn, login, PASSWORD),
        owner=owner,
        app=app,
    )


# --- 1. an application cannot migrate ---------------------------------------


def test_app_cannot_migrate_even_when_already_current(separated: Separated) -> None:
    """The regression this ordering exists to prevent.

    The database is fully migrated, so ``pending()`` is empty and the old code
    path would return the live version and exit 0 — reporting a successful
    migration performed by a principal that cannot write the ledger at all. The
    privilege proof runs BEFORE the ledger read precisely so this cannot happen.
    """
    with registry.connect(separated.app_dsn) as conn, pytest.raises(MigrationRoleRequired) as exc:
        apply_migrations(conn)
    assert "schema_migrations" in str(exc.value)


def test_app_cannot_assume_the_owner_role(separated: Separated) -> None:
    """Naming the owner role does not grant it."""
    with registry.connect(separated.app_dsn) as conn, pytest.raises(MigrationRoleRequired) as exc:
        apply_migrations(conn, owner_role=separated.owner)
    assert separated.owner in str(exc.value)


def test_owner_can_migrate(separated: Separated) -> None:
    """The positive half: elevation works and the operation is idempotent."""
    from workflow_runtime_core.migrations import LATEST_VERSION

    with registry.connect(separated.admin_dsn) as conn:
        assert apply_migrations(conn, owner_role=separated.owner) == LATEST_VERSION


def test_migrating_as_owner_transfers_ownership(separated: Separated) -> None:
    """Objects belong to the ROLE, not to the login that ran the migration.

    This is what decouples identity churn from ownership: a new operator, or a
    replaced service principal, changes nothing about who owns the tables.
    """
    with psycopg.connect(separated.admin_dsn) as conn:
        rows = conn.execute(
            """
            SELECT c.relname, pg_get_userbyid(c.relowner) AS owner
              FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = 'public' AND c.relkind = 'r'
            """
        ).fetchall()
    assert rows, "expected the core chain's tables to exist"
    assert all(owner == separated.owner for _name, owner in rows), dict(rows)


# --- 2. the grant manifest, for real ----------------------------------------


def test_manifest_leaves_no_gap(separated: Separated) -> None:
    with registry.connect(separated.admin_dsn) as conn:
        assert missing_privileges(conn, CORE_MANIFEST, application_role=separated.app) == []


def test_app_can_write_operational_rows(separated: Separated) -> None:
    run_id, workspace_id = uuid.uuid4(), uuid.uuid4()
    with registry.connect(separated.app_dsn) as conn:
        conn.execute(
            "INSERT INTO runs (run_id, workflow, thread_id, status, workspace_id) "
            "VALUES (%s, 'wf', 't', 'queued', %s)",
            (run_id, workspace_id),
        )
        # run_events is a BIGSERIAL insert, so this also exercises the SEQUENCE
        # grant — the privilege most easily forgotten, because the table grant
        # looks complete without it and only an insert reveals the gap.
        conn.execute("INSERT INTO run_events (run_id, kind) VALUES (%s, 'created')", (run_id,))


@pytest.mark.parametrize("table", CORE_MANIFEST.ledger_tables)
@pytest.mark.parametrize("statement", ["INSERT INTO {t} DEFAULT VALUES", "DELETE FROM {t}"])
def test_app_cannot_write_a_migration_ledger(separated: Separated, table: str, statement: str) -> None:
    """The security property, asserted on SQLSTATE rather than message text.

    Server messages are localised by lc_messages, so a text match silently stops
    working on a non-English server and turns a real regression green.
    """
    with (
        registry.connect(separated.app_dsn) as conn,
        pytest.raises(psycopg.errors.InsufficientPrivilege) as exc,
    ):
        conn.execute(statement.format(t=table))  # type: ignore[arg-type]
    assert exc.value.sqlstate == "42501"


def test_missing_privileges_detects_a_widened_ledger(separated: Separated) -> None:
    """The detector must fail when the thing it detects is present.

    A check that only ever reports "all good" is indistinguishable from one that
    reports nothing, so the widening is applied for real and the check must see
    it. This is the mutation test for missing_privileges.
    """
    with psycopg.connect(separated.admin_dsn, autocommit=True) as conn:
        conn.execute(f"SET ROLE {separated.owner}")  # noqa: S608 - generated identifier
        conn.execute(f"GRANT INSERT ON schema_migrations TO {separated.app}")  # noqa: S608

    with registry.connect(separated.admin_dsn) as conn:
        findings = missing_privileges(conn, CORE_MANIFEST, application_role=separated.app)
    assert any("HOLDS INSERT on ledger" in f for f in findings), findings


def test_missing_privileges_detects_a_revoked_operational_grant(separated: Separated) -> None:
    """The other direction: a gap that would break the application."""
    with psycopg.connect(separated.admin_dsn, autocommit=True) as conn:
        conn.execute(f"SET ROLE {separated.owner}")  # noqa: S608 - generated identifier
        conn.execute(f"REVOKE UPDATE ON runs FROM {separated.app}")  # noqa: S608

    with registry.connect(separated.admin_dsn) as conn:
        findings = missing_privileges(conn, CORE_MANIFEST, application_role=separated.app)
    assert any("lacks UPDATE on public.runs" in f for f in findings), findings


def test_reconcile_is_idempotent_and_repairs(separated: Separated) -> None:
    """Re-running NARROWS a hand-widened ledger rather than leaving it widened."""
    with psycopg.connect(separated.admin_dsn, autocommit=True) as conn:
        conn.execute(f"SET ROLE {separated.owner}")  # noqa: S608 - generated identifier
        conn.execute(f"GRANT INSERT, UPDATE, DELETE ON schema_meta TO {separated.app}")  # noqa: S608

    with registry.connect(separated.admin_dsn) as conn:
        conn.execute(f"SET LOCAL ROLE {separated.owner}")  # noqa: S608 - generated identifier
        reconcile(conn, CORE_MANIFEST, application_role=separated.app)

    with registry.connect(separated.admin_dsn) as conn:
        assert missing_privileges(conn, CORE_MANIFEST, application_role=separated.app) == []


# --- 3. checkpoint setup vs verify ------------------------------------------


def test_verify_refuses_an_absent_checkpoint_store(separated: Separated) -> None:
    """A DML-only runtime is told what deployment step was skipped.

    Not "permission denied for table checkpoints" three frames into langgraph —
    which is what a runtime calling setup() without DDL privilege would report,
    and which names neither the cause nor the fix.
    """
    pytest.importorskip("langgraph.checkpoint.postgres")
    from workflow_runtime_core.exceptions import CheckpointStoreOutdated
    from workflow_runtime_core.executor.checkpointer import checkpointer

    with pytest.raises(CheckpointStoreOutdated) as exc:
        with checkpointer(separated.admin_dsn, setup="verify"):
            pass  # pragma: no cover - the context manager raises on entry
    assert "wrc checkpoint-setup" in str(exc.value)


def test_run_creates_the_store_and_verify_then_accepts_it(separated: Separated) -> None:
    pytest.importorskip("langgraph.checkpoint.postgres")
    from workflow_runtime_core.executor.checkpointer import checkpointer

    with checkpointer(separated.admin_dsn, setup="run") as saver:
        assert saver is not None
    # Same database, now migrated: verify must be satisfied.
    with checkpointer(separated.admin_dsn, setup="verify") as saver:
        assert saver is not None


def test_default_setup_mode_still_runs(separated: Separated) -> None:
    """The compatibility floor, proven against a database rather than asserted.

    Every pre-0.8.0 consumer relies on the runtime creating the checkpoint store
    on first use; this passes no ``setup`` argument at all.
    """
    pytest.importorskip("langgraph.checkpoint.postgres")
    from workflow_runtime_core.executor.checkpointer import checkpointer

    with checkpointer(separated.admin_dsn) as saver:
        assert saver is not None
    with psycopg.connect(separated.admin_dsn) as conn:
        row = conn.execute("SELECT to_regclass('public.checkpoint_migrations')").fetchone()
    assert row is not None and row[0] is not None


def test_checkpoint_setup_command_owns_and_grants(separated: Separated) -> None:
    """The shipped ``wrc checkpoint-setup``, not a re-implementation of it.

    Exercising the command matters here because the ORDER inside it is the
    whole contract: it must elevate BEFORE calling ``setup()``, so the tables
    langgraph creates are owned by the owner role. Calling ``checkpointer(...,
    setup="run")`` instead — the obvious-looking shortcut — leaves them owned by
    whichever login ran it, and the grants that follow then fail with
    "permission denied for table checkpoints". That is not a test artefact: it
    is precisely the ownership-follows-identity coupling this plan removes, and
    it is easier to reproduce by accident than to reason about.
    """
    pytest.importorskip("langgraph.checkpoint.postgres")
    from click.testing import CliRunner

    from workflow_runtime_core.cli import main

    result = CliRunner().invoke(
        main,
        [
            "checkpoint-setup",
            "--dsn",
            separated.admin_dsn,
            "--owner-role",
            separated.owner,
            "--application-role",
            separated.app,
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output

    with psycopg.connect(separated.admin_dsn) as conn:
        rows = conn.execute(
            """
            SELECT c.relname, pg_get_userbyid(c.relowner)
              FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = 'public' AND c.relname LIKE 'checkpoint%'
            """
        ).fetchall()
    assert rows, "expected the checkpoint store to exist"
    assert all(owner == separated.owner for _name, owner in rows), dict(rows)

    with registry.connect(separated.admin_dsn) as conn:
        assert missing_privileges(conn, CHECKPOINT_MANIFEST, application_role=separated.app) == []

    with (
        registry.connect(separated.app_dsn) as conn,
        pytest.raises(psycopg.errors.InsufficientPrivilege) as exc,
    ):
        conn.execute("INSERT INTO checkpoint_migrations (v) VALUES (999)")
    assert exc.value.sqlstate == "42501"


def test_auth_probe_reports_the_app_principal_and_denies_ddl(separated: Separated) -> None:
    """The command a canary runs inside a live consumer after rotation.

    Its exit code is a verdict, not a status: a runtime principal that can
    CREATE in the schema can shadow a table the rest of the estate reads, so the
    probe exits non-zero on that finding. Here it must exit 0 — the application
    role holds DML and nothing more.
    """
    import json

    from click.testing import CliRunner

    from workflow_runtime_core.cli import main

    result = CliRunner().invoke(main, ["auth-probe", "--dsn", separated.app_dsn, "--json"])
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["ddl_denied"] is True
    assert report["can_create_in_schema"] is False
    assert separated.app in report["memberships"]
    assert report["auth_mode"] == "password"
    # Nothing credential-shaped may reach a job log.
    assert PASSWORD not in result.output


def test_auth_probe_exits_non_zero_for_a_principal_that_can_issue_ddl(separated: Separated) -> None:
    """The mutation half: the probe must FAIL for an over-privileged principal.

    Without this, a probe that always exited 0 would look identical to one that
    works, and every canary would report success.
    """
    from click.testing import CliRunner

    from workflow_runtime_core.cli import main

    result = CliRunner().invoke(main, ["auth-probe", "--dsn", separated.admin_dsn, "--json"])
    assert result.exit_code == 1, result.output
