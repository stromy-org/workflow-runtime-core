---
paths:
  - "src/**"
  - "tests/**"
  - "pyproject.toml"
---
# Runtime invariants and the public API

> Routed rule: loaded when a session touches source, tests or `pyproject.toml` (paths in the frontmatter, Bash/Grep via `.claude/hooks/context-router.sh`). The security-critical invariants are also always on in `AGENTS.md`. If you are changing code here and have NOT seen this file, read it first: these invariants are load-bearing for three consumers.

## Module map

```
src/workflow_runtime_core/
  models.py       RunStatus · RunRecord · TerminalProjection      (base)
  registry.py     connection + ALL run DML, no DDL                (base)
  auth.py         auth mode, credential source, role validation    (base)
  grants.py       per-chain privilege manifests                    (base)
  migrations.py   numbered migrations + advisory-locked applier    (base)
  schema.py       read/require the live version — never writes     (base)
  binding.py      the ExecutionBinding protocol                    (base)
  cli.py          `wrc migrate | status | list-migrations |        (base)
                   checkpoint-setup | auth-probe`
  executor/       checkpointer + runner                        (executor extra)
```

## Dependency direction

**Dependency direction is one-way and load-bearing.** The base package imports only
`psycopg` + `click`; `executor/` may import LangGraph; nothing imports a consumer.
A contract test spawns a subprocess and asserts that importing the base package
leaks neither `langgraph` nor `aio_pika` — that is what lets the facade depend on
this package without acquiring a graph engine.

## Invariants — do not weaken these

1. **This package never runs DDL from application code.** `wrc migrate` is the only
   writer of schema; applications call `require_compatible_schema()` and nothing
   else. An app that migrates itself can move the shared schema out from under a
   consumer still compiled against the previous version.
2. **The compatibility gate is a RANGE, not an equality.** Readers must be
   deployable *ahead* of a migration; that is the whole expand/migrate/contract
   sequence. Widening the ceiling and applying the migration are separate releases.
3. **`claim_run` uses `FOR UPDATE` without `SKIP LOCKED`.** The loser must observe
   the winner's committed status and exit cleanly, not skip the row and conclude it
   vanished.
4. **The idempotency index is PARTIAL** (`WHERE idempotency_key IS NOT NULL`), and
   the racing INSERT sits in a savepoint so the loser's re-fetch still has a usable
   transaction.
5. **`RunRecord.public()` withholds `job_template_json`, `config_json` and
   `image_tag`.** The rendered job template is secret-bearing and must never reach a
   caller.
6. **Bind the checkpointer by attribute copy, never `with_config(checkpointer=...)`** —
   the latter is silently accepted and yields a graph with NO durability.
7. **A migration command proves its privilege BEFORE reading the ledger.** The
   application role almost always finds the ledger already current, so a lazy check
   takes the "nothing to do" path and exits 0 — reporting a migration nobody could
   have performed. `assert_may_migrate()` runs first and raises
   `MigrationRoleRequired`.
8. **Role names are ARGUMENTS, never constants.** This package is consumed by an
   estate whose roles it must not encode and by generated services that have none.
   `auth.validate_identifier()` refuses anything needing quoting rather than
   escaping it — these strings reach `SET ROLE` and `GRANT`, which take no
   parameters.
9. **No `GRANT ... ON ALL TABLES`, and no `ALTER DEFAULT PRIVILEGES`.** `ON ALL
   TABLES` is a snapshot that reads like a rule and sweeps the migration ledgers in
   with it; default privileges are a rule that is not retroactive and applies per
   creating role. `grants.py` names every object, so a new table is *inaccessible*
   until its chain classifies it operational or ledger — a loud failure, which is
   the one to want.
10. **`checkpoint-setup` runs under autocommit with a session-level `SET ROLE`.**
    LangGraph's `setup()` issues `CREATE INDEX CONCURRENTLY`, which PostgreSQL
    refuses inside a transaction block, so `SET LOCAL ROLE` would apply to a
    transaction ending with the statement itself. Elevation must precede `setup()`
    or the tables end up owned by the operator's login.

## Public API

```python
from workflow_runtime_core import (
    RunRecord, RunStatus, TerminalProjection,   # models
    ExecutionBinding,                            # the seam consumers implement
    require_compatible_schema, apply_migrations, # schema lifecycle
    RegistryError, SchemaVersionMismatch,        # loud failures, never fallbacks
)
from workflow_runtime_core import registry       # DML: create_run, claim_run, mark_*
from workflow_runtime_core.executor import run_once   # needs the `executor` extra
```

A consumer supplies an `ExecutionBinding` (`resolve_graph` / `build_input` /
`build_context` / `project_terminal`) and the core owns everything else. A bare
graph-resolver callable is deliberately *not* enough — it cannot express a resume
`Command`, a runtime context, or a terminal artifact projection.

Two OPTIONAL protocols extend it, both probed by attribute so a binding that
predates them needs no edit: `ScopedExecutionBinding` (`execution_scope`, per-run
process state) and `ConfiguredExecutionBinding` (`build_invoke_config`, top-level
runnable-config keys).

**`build_context` is NOT where callbacks go** — it feeds LangGraph's *runtime
context*, which discards LangChain callbacks silently. Tracing handlers,
`metadata` and `tags` go on `build_invoke_config`, which the runner merges into
the config it passes to `astream`. Getting this wrong costs nothing at runtime
and produces no telemetry, which is exactly how it survived four months
(ORG-291). `configurable` is reserved — it carries `thread_id`.

## Development Patterns

- ruff: line-length 120, rules `ASYNC, B, PERF, S, E, F, W, I`
- pyright: strict mode
- Optional dependencies guarded with `try/except` + `DependencyError` (see `exceptions.py`)

## Testing

```
tests/
  conftest.py
  unit/        # fast, isolated
  contract/    # API stability
  integration/ # end-to-end, may need env vars
```

Markers: `@pytest.mark.unit`, `@pytest.mark.contract`, `@pytest.mark.integration`.
