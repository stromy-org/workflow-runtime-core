# Workflow Runtime Core

Client-neutral durable workflow lifecycle: versioned run registry, explicit migrations, execution bindings, leases and transactional outbox

## Install

```bash
uv sync                          # Core deps
uv sync --extra all              # All optional extras
uv sync --extra dev              # Dev tools
```


## CLI

```bash
uv run wrc --help
```


## Public API

```python
from workflow_runtime_core import ...   # populate __all__ in src/workflow_runtime_core/__init__.py
```

## Tests

```bash
uv run pytest tests/unit
uv run pytest tests/contract
```

## Releases

This library is consumed by downstream repos via `[tool.uv.sources]` git+URL pins. To cut a release:

1. Bump `[project].version` in `pyproject.toml` on `main` — a normal reviewed PR. Relock (`uv lock`) in the same commit.
2. Actions -> **Release** -> *Run workflow* (or `gh workflow run release.yml`).

**You never type a tag.** The workflow derives it from `[project].version`, runs
every gate — default branch, main-ancestry, lint, types, tests, lockfile, build —
and creates the tag and the GitHub Release only if they all pass. Dispatching
without having bumped the version is refused before anything is built.

Consumer pins are not your job: stromy-org's `internal-lib-pins.yml` reconciles
them daily from every consumer's own `[tool.uv.sources]`.

See `stromy-org/infra-docs/ai/internal-libs.md` for the full release pattern.

## Agent instructions

See `AGENTS.md` (canonical, cross-vendor). `CLAUDE.md` and `.github/copilot-instructions.md` are regenerated from it by `scripts/render-agent-md.py`.
