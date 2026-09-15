"""Shared pytest fixtures for agent-graph tests.

Unit tier (per D9): in-memory SQLite for state, Fixture* evidence
capabilities over the recorded fault fixtures, and a scripted FakeLLM
(tests/agent/fakes.py) - no live infrastructure, no real LLM calls.
Integration-marked tests layer on the compose Postgres (checkpoint
round-trip, run-guard index) or the real Groq client (the first live
provider round-trip).
"""

import functools
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine
from sqlalchemy.pool import StaticPool

from helix.config import get_settings
from integrations.fixtures import FixtureStore
from state import models
from state.db import session_scope

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "state" / "migrations" / "alembic.ini"


@pytest.fixture
def sqlite_engine() -> Iterator[Engine]:
    """Fresh in-memory SQLite per test (StaticPool shares one connection)."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    models.Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def sqlite_sessions(sqlite_engine: Engine):
    """A session_scope-equivalent bound to the test SQLite engine."""
    return functools.partial(session_scope, sqlite_engine)


@pytest.fixture
def store() -> FixtureStore:
    return FixtureStore()


@pytest.fixture(scope="session")
def pg_engine() -> Iterator[Engine]:
    """Compose Postgres with `alembic upgrade head` applied (D9 integration
    tier - deselected by default on a clean machine)."""
    from alembic import command
    from alembic.config import Config

    command.upgrade(Config(str(ALEMBIC_INI)), "head")
    engine = create_engine(get_settings().database_url)
    yield engine
    engine.dispose()


@pytest.fixture
def pg(pg_engine: Engine) -> Engine:
    """The migrated Postgres engine with all Helix state tables emptied.
    (Checkpointer tables are LangGraph's, not Base.metadata's, and persist
    - incident ids are unique per test so threads never collide.)"""
    with pg_engine.begin() as conn:
        for table in reversed(models.Base.metadata.sorted_tables):
            conn.execute(table.delete())
    return pg_engine


@pytest.fixture
def pg_sessions(pg: Engine):
    """A session_scope-equivalent bound to the live Postgres."""
    return functools.partial(session_scope, pg)
