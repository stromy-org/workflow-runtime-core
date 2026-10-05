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

Receipt adapters import `workflow_runtime_core.messaging.receipts`. Commit the
claim and `start_effect` marker before provider I/O, then commit the provider ID
before finalization. Every worker transition requires the live lease owner and
claim epoch. Unknown outcomes are never automatically recreated.

`claim_due(message_ids=(...))` restricts selection to a bounded set of domain
IDs while retaining the namespace/destination, due-time and status fences. An
empty tuple selects nothing; leaving it unset preserves the generic pull.
`review_uncertain` requires a verified service actor, current event version and
redacted inspection evidence. It moves an unknown effect to review even when a
create response lost its provider ID; it cannot retry or mark delivered.

Restricted receipt roles can use `ReceiptCapabilityConnection(native)` from
`workflow_runtime_core.messaging.receipt_capabilities`. It borrows the native
connection and preserves the caller's transaction: a proposal, its binding and
the WRC receipt can commit or roll back together. Existing receipt APIs keep
their arguments; the wrapper uses individually granted, fixed SQL functions.
It never falls back to raw table mutations or creates another receipt ledger.

The migration owner explicitly runs `wrc receipt-capabilities-setup --owner-role
<owner>`. This opt-in checksum-ledger chain adds functions only and leaves core
schema v5 unchanged. Every function fixes its search path and revokes PUBLIC
execution; setup grants nothing to application roles. Deployment manifests must
name each permitted function using `function_name(operation)` and revoke raw
receipt/audit table writes. The application also needs SELECT on core schema
metadata and receipt rows. A runtime principal cannot run setup, even when the
capability ledger is already current.

Grant `open` only to an authorized domain definer that atomically persists a
proposal. Grant worker and read-only recovery operations only to the connector.
Keep `resolve:*` behind the domain resolution service, which authenticates the
reviewer before calling WRC: a SQL argument is evidence of an actor, not an
authentication mechanism. No application role needs access to every capability.
Unknown operations, ungranted functions, wrong transition/status/actor kinds,
stale leases and optimistic versions all fail closed. Frozen migration bytes
are checked against the same fixed query definitions used by native callers;
changing an installed capability requires a new numbered migration.

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
