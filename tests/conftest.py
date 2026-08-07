"""Shared pytest fixtures.

``admin_dsn``/``blank_dsn`` give integration tests a real PostgreSQL with a
brand-new empty database per test, so tests cannot order-couple through shared
schema state. Locally and in CI the engine comes from testcontainers; setting
``STROMY_PG_DSN`` substitutes an existing server that can CREATE DATABASE.
"""

from __future__ import annotations

import os
import uuid

import pytest

_DSN_ENV = "STROMY_PG_DSN"

#: Per-process suffix. A monotonic counter alone is NOT enough: it restarts at 1
#: every pytest session, so against a long-lived server (the documented
#: ``STROMY_PG_DSN`` path, and the fast way to run this suite locally) the second
#: run collides with the first run's leftover databases and every integration
#: test errors at fixture setup. It also makes two concurrent test processes
#: safe against each other.
_RUN_ID = uuid.uuid4().hex[:8]


@pytest.fixture(scope="session")
def admin_dsn():
    """A reachable PostgreSQL with permission to CREATE DATABASE."""
    provided = os.environ.get(_DSN_ENV, "").strip()
    if provided:
        yield provided
        return
    testcontainers = pytest.importorskip(
        "testcontainers.postgres",
        reason=f"neither {_DSN_ENV} nor testcontainers is available",
    )
    with testcontainers.PostgresContainer("postgres:16-alpine") as pg:
        yield pg.get_connection_url().replace("postgresql+psycopg2://", "postgresql://")


@pytest.fixture
def blank_dsn(admin_dsn: str, request: pytest.FixtureRequest):
    """A brand-new empty database per test, so tests cannot order-couple."""
    import psycopg

    name = f"blank_{_RUN_ID}_{abs(hash(request.node.nodeid)) % 10**10}"
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        # Belt-and-braces against a re-used server: the suffix should already be
        # unique, and a leftover database from a killed run must not fail the
        # next one.
        conn.execute(f'DROP DATABASE IF EXISTS "{name}"')  # noqa: S608 - generated name
        conn.execute(f'CREATE DATABASE "{name}"')  # noqa: S608 - generated name
    head, _, _tail = admin_dsn.rpartition("/")
    yield f"{head}/{name}"
