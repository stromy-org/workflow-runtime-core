<!--
  GENERATED FILE — DO NOT EDIT.
  Source of truth: AGENTS.md (cross-vendor standard).
  Override file:   .agent-overrides/claude.md (optional, appended below)
  Regenerate with: scripts/render-agent-md.py
-->

# AGENTS.md

Self-contained instructions for Codex and other AI agents working on workflow-runtime-core.

> **AGENTS.md is the canonical instruction file** for this repo (cross-vendor standard).
> `CLAUDE.md` and `.github/copilot-instructions.md` are generated from this file by
> `scripts/render-agent-md.py`. Gemini CLI reads this file directly via
> `context.fileName: ["AGENTS.md"]` in `.gemini/settings.json`. **Do not hand-edit
> the generated files.**

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
  migrations.py   numbered migrations + advisory-locked applier    (base)
  schema.py       read/require the live version — never writes     (base)
  binding.py      the ExecutionBinding protocol                    (base)
  cli.py          `wrc migrate | status | list-migrations`         (base)
  executor/       checkpointer + runner                        (executor extra)
```

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
