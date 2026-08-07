"""Opening the checkpoint store concurrently on a FRESH database.

Job-per-run starts N runner processes in parallel by design, and each one opens
the checkpoint store as it boots. `setup()` is idempotent sequentially, so this
passes trivially against any database something has already initialised — which
is every database a test suite normally sees.

It is not idempotent concurrently. On a database with no checkpoint tables, two
simultaneous `setup()` calls both observe "absent", both issue the CREATE, and
the loser dies on a duplicate `pg_type` key. So the failure window is exactly a
brand-new installation's first burst of traffic: the one moment nobody is
watching a test suite, and the one database state the suite never has.

That is why this test insists on a genuinely empty database rather than reusing
a migrated one.
"""

from __future__ import annotations

import asyncio

import pytest

from workflow_runtime_core.executor.checkpointer import acheckpointer


async def _open_and_close(dsn: str) -> str:
    async with acheckpointer(dsn) as saver:
        assert saver is not None
    return "ok"


@pytest.mark.integration
async def test_concurrent_first_open_on_a_fresh_database(blank_dsn: str) -> None:
    """Eight runners boot at once against a database with no checkpoint tables.

    Without the advisory lock this fails with:
        duplicate key value violates unique constraint "pg_type_typname_nsp_index"
        DETAIL: Key (typname, typnamespace)=(checkpoint_migrations, 2200) ...
    """
    pytest.importorskip(
        "langgraph.checkpoint.postgres.aio",
        reason="requires the `executor` extra",
    )

    # Bounded, because the two rejected fixes for this failed by HANGING rather
    # than raising (an advisory lock starves LangGraph's CREATE INDEX
    # CONCURRENTLY, which waits on the very transactions queued behind it). An
    # unbounded gather would turn that regression into a wedged CI job with no
    # failure to read.
    async with asyncio.timeout(120):
        results = await asyncio.gather(
            *(_open_and_close(blank_dsn) for _ in range(8)),
            return_exceptions=True,
        )

    failures = [r for r in results if isinstance(r, BaseException)]
    assert not failures, f"{len(failures)}/8 concurrent opens failed: {failures[0]!r}"


@pytest.mark.integration
async def test_the_setup_lock_is_released_for_the_next_opener(blank_dsn: str) -> None:
    """A session-scoped lock that leaked would wedge every later runner.

    The second open must not block: if the first one failed to unlock, this
    hangs rather than fails, so it is bounded by a timeout to make the symptom a
    test failure instead of a stuck suite.
    """
    pytest.importorskip(
        "langgraph.checkpoint.postgres.aio",
        reason="requires the `executor` extra",
    )

    await _open_and_close(blank_dsn)
    async with asyncio.timeout(30):
        await _open_and_close(blank_dsn)
