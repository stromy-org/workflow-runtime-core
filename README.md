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

1. Bump `[project].version` in `pyproject.toml` on `main`.
2. `git tag vX.Y.Z && git push --tags`
3. CI builds + publishes a GitHub Release; `notify-parent.yml` fires a `submodule-bumped` event into stromy-org.

See `stromy-org/infra-docs/ai/internal-libs.md` for the full release pattern.

## Agent instructions

See `AGENTS.md` (canonical, cross-vendor). `CLAUDE.md` and `.github/copilot-instructions.md` are regenerated from it by `scripts/render-agent-md.py`.
