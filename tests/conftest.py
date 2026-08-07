"""Shared pytest fixtures.

``admin_dsn``/``blank_dsn`` give integration tests a real PostgreSQL with a
brand-new empty database per test, so tests cannot order-couple through shared
schema state. Locally and in CI the engine comes from testcontainers; setting
``STROMY_PG_DSN`` substitutes an existing server that can CREATE DATABASE.
"""

from __future__ import annotations

import itertools
import os

import pytest

_DSN_ENV = "STROMY_PG_DSN"
_counter = itertools.count(1)


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
def blank_dsn(admin_dsn: str):
    """A brand-new empty database per test, so tests cannot order-couple."""
    import psycopg

    name = f"blank_test_{next(_counter)}"
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    head, _, _tail = admin_dsn.rpartition("/")
    yield f"{head}/{name}"
