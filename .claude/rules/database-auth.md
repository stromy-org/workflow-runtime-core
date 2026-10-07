---
paths:
  - "src/workflow_runtime_core/auth.py"
  - "src/workflow_runtime_core/grants.py"
  - "src/workflow_runtime_core/migrations.py"
  - "src/workflow_runtime_core/cli.py"
---
# Database authentication

> Routed rule: loaded when a session touches the auth, grants, migrations or CLI modules, or writes a `WRC_PG_*` / `WRC_CHECKPOINT_SETUP` setting (paths, commands and content selectors in `.claude/instruction-routes.json`).

## Database authentication (0.8.0, opt-in)

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
