# Changelog

## 0.15.0

ORG-PLAN-345 — a run is bounded by the wall-clock ceiling its attempt was started under.

- `execute()` bounds each execution by a deadline: an explicit `deadline_seconds`, else the attempt's declared spend ceiling (`record_spend_ceiling`), else the binding's optional `default_deadline_seconds(run)` (`DeadlineExecutionBinding`). On expiry the graph is cancelled **and awaited**, the run is recorded `failed` with `error_type: deadline_exceeded` (stage `deadline`, not retryable) and the lease is released by that same write. The deadline sits beneath the lease, so a lost lease still stops the run and records nothing. A paused run's next execution has its own bound.
- New `exceptions.DeadlineExceeded`.
- `registry.release_cancelled_attempt_lease(conn, run_id=, attempt_no=, owner=)` moves in from Stromy: the worker's owner- and attempt-scoped acknowledgement after a cancellation, which now emits `lease_released`.

## 0.14.0

ORG-PLAN-345 — a caller can now tell a cancellation that was *asked for* from one the worker has *acknowledged*.

- `RunRecord.public()` gains `cancellation`: `not_requested` | `requested` | `confirmed` | `unavailable`, derived by `RunRecord.cancellation_state()` from the worker-owned lease. `confirmed` means no worker holds the lease; `requested` means the lease is still live; a lapsed-but-unreleased lease (a dead worker) or a pre-v2 row reads `unavailable`, never `confirmed`. The lease itself is still never emitted.
- `release_lease` emits `lease_released` when the run's status is `cancelled` — the worker's acknowledgement.
- New `registry.public_events(conn, run_id)`: an allow-list of event kinds (`PUBLIC_EVENT_KINDS`), `{kind, created_at}` only, last 50. Every `detail` is dropped, so the `claimed` event's owner and dispatch id never leave the core.
- New `registry.record_spend_ceiling(conn, run_id, max_runtime_minutes)` and `RunRecord.spend_ceiling_minutes()`/`usage()`: an attempt declares its own wall-clock ceiling, stored beside (not inside) the pinned snapshot so a retry never inherits the parent's bound. `public()` gains `usage` — `{status: reserved, max_runtime_minutes}` or `{status: unavailable}`; never a zero or a cost figure (`provider_reported` is reserved vocabulary).
- No schema migration.
