# Changelog

## 0.14.0

ORG-PLAN-345 — a caller can now tell a cancellation that was *asked for* from one the worker has *acknowledged*.

- `RunRecord.public()` gains `cancellation`: `not_requested` | `requested` | `confirmed` | `unavailable`, derived by `RunRecord.cancellation_state()` from the worker-owned lease. `confirmed` means no worker holds the lease; `requested` means the lease is still live; a lapsed-but-unreleased lease (a dead worker) or a pre-v2 row reads `unavailable`, never `confirmed`. The lease itself is still never emitted.
- `release_lease` emits `lease_released` when the run's status is `cancelled` — the worker's acknowledgement.
- New `registry.public_events(conn, run_id)`: an allow-list of event kinds (`PUBLIC_EVENT_KINDS`), `{kind, created_at}` only, last 50. Every `detail` is dropped, so the `claimed` event's owner and dispatch id never leave the core.
- No schema migration.
