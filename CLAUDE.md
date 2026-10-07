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

Modules: `models` · `registry` (all run DML, no DDL) · `auth` · `grants` · `migrations` · `schema` (reads the live version, never writes) · `binding` · `cli` (`wrc`), plus `executor/` behind the `executor` extra. The annotated map is in `runtime-invariants.md`.

## Always-on rules

- **Dependency direction is one-way**: the base package imports only `psycopg` + `click`, `executor/` may import LangGraph, and nothing imports a consumer. [`runtime-invariants.md`]
- **Ten load-bearing invariants govern `src/`** — read `runtime-invariants.md` before changing it. Never weakened, whatever the change:
  - application code never runs DDL; `wrc migrate` is the only schema writer;
  - `RunRecord.public()` never returns the secret-bearing `job_template_json`, `config_json` or `image_tag`;
  - role names are arguments validated by `auth.validate_identifier()`, never constants;
  - grants name every object: no `GRANT ... ON ALL TABLES`, no `ALTER DEFAULT PRIVILEGES`.
- **Database auth settings never default silently** (`WRC_PG_CREDENTIAL` unset is an error). [`database-auth.md`]
- **Loud failures, never fallbacks**: optional dependencies are guarded with `try/except` + `DependencyError`; ruff (line-length 120) and strict pyright gate every change. [`runtime-invariants.md`]

<!-- route-index:start -->
## Route index

Claude sessions load these procedures automatically. Every other agent opens the file named here **before** doing the matching work. The always-on rules above apply either way.

| Open | When you touch |
|---|---|
| `.claude/rules/runtime-invariants.md` | `src/**`, `tests/**`, `pyproject.toml`, a command matching `\bwrc\b` |
| `.claude/rules/database-auth.md` | `src/workflow_runtime_core/auth.py`, `src/workflow_runtime_core/grants.py`, `src/workflow_runtime_core/migrations.py`, `src/workflow_runtime_core/cli.py`, a command matching `\bwrc (migrate\|checkpoint-setup\|auth-probe)\b`, code containing `\bWRC_PG_[A-Z_]+\b`, code containing `\bWRC_CHECKPOINT_SETUP\b` |
<!-- route-index:end -->

## Skill Workflow

- **Commits**: `/conventional-commit`
- **Library maintenance**: `/python-library-maintain` (in-satellite — bump version, tag release, refresh AGENTS, sync optional extras)
- **New skills (rare for libs)**: `/skill-creator`
