---
name: python-library-maintain
description: "Maintain Workflow Runtime Core — bump version, cut a release, refresh AGENTS.md, sync optional extras, run copier update. Use whenever the user wants to maintain or update this library — even if they don't say 'library' explicitly. Triggers on phrases like 'bump version', 'tag a release', 'refresh AGENTS', 'pull template improvements', 'add an extra', 'sync the template'."
---

# python-library-maintain — Workflow Runtime Core

In-satellite maintainer skill for the **workflow-runtime-core** library. This skill is shipped by the `python-library-template` Copier template — the same content lives in every Python-library satellite and in `stromy-org/.claude/skills/python-library-maintain/` (org mirror, propagated by `scripts/sync-maintainer-skills.sh`).

## When to use

- Bumping the library version + cutting a release tag.
- Refreshing AGENTS.md / re-rendering CLAUDE.md after edits.
- Adding or removing an optional-dependency extra.
- Pulling template improvements via `copier update --skip-answered`.

## When NOT to use

- Creating a new library from scratch → org-level `/repo-scaffold python-library`.
- General Python coding questions → use repo-specific or global skills.

## Repo layout (recap)

```
workflow-runtime-core/
├── AGENTS.md                       # Source of truth — CLAUDE.md + copilot are generated
├── pyproject.toml                  # hatchling, ruff, pyright, pytest
├── src/workflow_runtime_core/
│   ├── __init__.py                 # __all__ is the public API contract
│   ├── exceptions.py               # base + DependencyError for optional extras
│   └── cli.py                       # Click CLI entry point
├── tests/
│   ├── unit/                       # fast, isolated
│   ├── contract/                   # public-API stability
│   └── integration/                # may need env vars
├── .github/workflows/
│   ├── ci.yml                      # uses stromy-org reusable workflow
│   ├── release.yml                 # tag → GH Release + consumer dispatch
│   └── notify-parent.yml           # push-to-main → stromy-org submodule bump
└── scripts/
    └── render-agent-md.py          # AGENTS.md → CLAUDE.md / copilot-instructions.md
```

## Workflows

### Bump version + release

1. Edit `[project].version` in `pyproject.toml` on `main`.
2. Update `CHANGELOG.md` if present.
3. Commit via `/conventional-commit`.
4. `git tag vX.Y.Z && git push --tags`.
5. CI runs `release.yml`, builds sdist+wheel, publishes a GitHub Release, and fires a `repository_dispatch` into each `consumer_repos_to_notify` to open a pin-bump PR — authenticated by the org `stromy-ci` GitHub App token (org secrets `CI_APP_ID`/`CI_APP_PRIVATE_KEY`).
6. `notify-parent.yml` fires a `submodule-bumped` event into stromy-org; the daily cron opens a pointer-bump PR if the dispatch was missed.

### Refresh AGENTS / re-render

1. Edit `AGENTS.md`.
2. Run `python3 scripts/render-agent-md.py`.
3. Commit AGENTS.md + the regenerated `CLAUDE.md` + `.github/copilot-instructions.md` in the **same logical unit**.

### Add or remove an optional extra

1. Edit `[project.optional-dependencies]` in `pyproject.toml`.
2. If adding code that uses the extra, guard imports with `try/except ImportError` and raise `DependencyError(extra="<name>", package="<pkg>")` so consumers get a clear install hint.
3. Add a `requires_<extra>` pytest marker in `pyproject.toml` `[tool.pytest.ini_options].markers` and mark relevant tests.
4. Run `uv sync --extra <name> && uv run pytest -m requires_<extra>`.

### Pull template improvements

1. From the lib root: `uvx copier update --trust --skip-answered`.
2. Review the diff carefully — answers stay; template improvements (CI workflow updates, AGENTS.md.jinja edits, ruff/pyright defaults) propagate.
3. Resolve conflicts by hand; do NOT regress public API.
4. Run the full quality stack before committing: `uv sync && uv run pytest && uv run ruff check src/ tests/ && uv run pyright src/workflow_runtime_core/`.

## Critical rules

- Every git commit goes through `/conventional-commit` (inherited skill).
- AGENTS.md is self-contained — no `@file` imports, no `.claude/rules/` references.
- Generated outputs (`CLAUDE.md`, `.github/copilot-instructions.md`) are committed but never hand-edited.
- Do not commit `.env` files. `.env.example` only.
- Public API (symbols in `__init__.py.__all__`) is a contract — drift requires a SemVer-major bump and consumer-side updates.
- Never weaken `[tool.pyright].typeCheckingMode = "strict"` to make errors go away; fix the types or add a per-symbol `# pyright: ignore[...]` with a justification.

## Reference

- Internal-libs release pattern: `stromy-org/infra-docs/ai/internal-libs.md`
- Template source-of-truth: `stromy-org/scaffolds/python-library-template/template/skills/python-library-maintain/SKILL.md.jinja`
- Plan: `stromy-org/PLAN_python_library_template.md`
