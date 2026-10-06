# AGENTS.md

Self-contained instructions for Codex and other AI agents working on workflow-runtime-core.

> **AGENTS.md is the canonical instruction file** for this repo (cross-vendor standard).
> `CLAUDE.md` and `.github/copilot-instructions.md` are generated from this file by
> `scripts/render-agent-md.py`. Gemini CLI reads this file directly via
> `context.fileName: ["AGENTS.md"]` in `.gemini/settings.json`. **Do not hand-edit
> the generated files.**

<!-- org-core:start v1 GENERATED from global-skills/global-instructions/org-core.md by stromy-org scripts/render-org-core.py; edit the source, not this copy -->
## Org core — how work lands in this org

This repository belongs to the stromy-org fleet. These rules apply here whichever agent you are and wherever this clone lives. The machine rules (git safety, worktrees, commits, `.env`) come from your user-level instructions; this block does not repeat them.

- **The control plane is `stromy-org/stromy-org`.** Its `catalog.json` is the inventory of every repo. **Work that spans more than one repo starts from a stromy-org root checkout, not from here.** That includes a parent submodule pointer, a shared template or skill, and a fleet-wide check.
- **Land through a branch and a PR** (`gh pr create`, or `scripts/sync.sh land` from the stromy-org root). A pushed branch without a PR is parked, not landed. **Every PR you mention carries its full URL** (`https://github.com/<owner>/<repo>/pull/<n>`), never only `#<n>`.
- **Parent submodule pointers are bot-owned.** When this repo is a stromy-org submodule, push its branch and stop. Never hand-edit or stage the parent's gitlink.
- **Merging a `deploy_on_main` repo deploys it.** `catalog.json` marks those repos. A merge needs an explicit deploy decision: the reviewed route (`premerge_review.py`, then `merge-reviewed.sh`, both run from the stromy-org root) or an operator yes for that specific PR. It is never a wrap-up side effect. Merge over a red check only when the failure is proven to already exist on `main`.
- **Every commit goes through the `conventional-commit` skill**: Conventional Commits with gitmoji.
- **Backlog and plans:** track work with the `backlog` skill against this repo's own backlog, and plan with the `plan-*` skills. Never hand-edit a rendered `BACKLOG.md` or `DONE.md` where a renderer owns it.
- **Generated files are never hand-edited.** `CLAUDE.md` and `.github/copilot-instructions.md` are rendered from `AGENTS.md` (`scripts/render-agent-md.py`). This block is rendered from the org-core source in `global-skills`, and `scripts/check-org-core.py --check` verifies it. To change it, edit the source in `global-skills` and re-render; never edit the copy here.
- **Client confidentiality:** never name a client, its end clients, projects or people in anything another client can read.
- **Contract:** `infra-docs/ai/instruction-distribution.md` defines which instructions reach which agent, and the budgets.
<!-- org-core:end -->

## Project Overview

Client-neutral durable workflow lifecycle: versioned run registry, explicit migrations, execution bindings, leases and transactional outbox

## Commands

```bash
uv sync
uv sync --extra all              # All optional extras
uv run pytest -v
uv run ruff check src/
uv run pyright src/workflow_runtime_core/
uv run wrc --help
```


## Architecture

One workflow lifecycle, shared by three consumers — the Stromy runtime, the public
workflow facade (`stromy-workflows-mcp`), and client executors. Before this package
each of them carried its own copy of the same registry DML; the whole point is that
they now carry none.

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

### Database authentication (0.8.0, opt-in)

`WRC_PG_AUTH` defaults to `password`, so a consumer that upgrades and changes
nothing keeps the connection behaviour it had, and the base install stays free of
Azure dependencies. `entra` needs the `azure-postgres` extra and acquires a token
per connection.

| Setting | Values | Default | Notes |
|---|---|---|---|
| `WRC_PG_AUTH` | `password`, `entra` | `password` | selects the connection class |
| `WRC_PG_CREDENTIAL` | `managed-identity`, `azure-cli` | **none** | unset is an error, never a guess |
| `WRC_PG_OWNER_ROLE` / `--owner-role` | SQL identifier | unset | `SET ROLE` before migrating |
| `WRC_PG_APPLICATION_ROLE` / `--application-role` | SQL identifier | unset | chain grants reconciled for it |
| `WRC_CHECKPOINT_SETUP` | `run`, `verify` | `run` | `verify` refuses to migrate |

`WRC_PG_CREDENTIAL` has no default deliberately: `DefaultAzureCredential` succeeds
with whichever source answers first, which makes the database identity a property
of the ambient environment rather than of the deployment.

**Dependency direction is one-way and load-bearing.** The base package imports only
`psycopg` + `click`; `executor/` may import LangGraph; nothing imports a consumer.
A contract test spawns a subprocess and asserts that importing the base package
leaks neither `langgraph` nor `aio_pika` — that is what lets the facade depend on
this package without acquiring a graph engine.

### Invariants — do not weaken these

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
- All commits via `/conventional-commit` skill (machine-wide global skill install)

## Testing

```
tests/
  conftest.py
  unit/        # fast, isolated
  contract/    # API stability
  integration/ # end-to-end, may need env vars
```

Markers: `@pytest.mark.unit`, `@pytest.mark.contract`, `@pytest.mark.integration`.


## Agent-md rendering

`AGENTS.md` is the only authored agent-instruction file. Regenerate the rest:

```bash
python3 scripts/render-agent-md.py            # CLAUDE.md + .github/copilot-instructions.md
python3 scripts/render-agent-md.py --check    # exit 1 if stale
```

**Never hand-edit** `CLAUDE.md` or `.github/copilot-instructions.md` — they carry a "GENERATED FILE" banner; edits are wiped on next render.

## Commit Standards

- Conventional Commits with gitmoji
- Every commit via the `conventional-commit` skill (machine-wide)
- Co-Authored-By trailer on AI-assisted commits

## Skill Workflow

- **Commits**: `/conventional-commit`
- **Library maintenance**: `/python-library-maintain` (in-satellite — bump version, tag release, refresh AGENTS, sync optional extras)
- **New skills (rare for libs)**: `/skill-creator`
