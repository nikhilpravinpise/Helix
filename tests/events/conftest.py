"""Fixtures for the event-layer tests.

Mirrors `tests/state/conftest.py`'s two tiers per D9, because the E4
ingest path writes the same state tables. Defined per test package rather
than hoisted to a shared root conftest, which is the arrangement
`tests/state` and `tests/agent` already use.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from helix.config import get_settings
from state import models

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "state" / "migrations" / "alembic.ini"


@pytest.fixture
def sqlite_engine() -> Iterator[Engine]:
    """Fresh in-memory SQLite database per test."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    models.Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def db_session(sqlite_engine: Engine) -> Iterator[Session]:
    """A session on the test SQLite database."""
    with Session(sqlite_engine, expire_on_commit=False) as session:
        yield session


@pytest.fixture(scope="session")
def pg_engine() -> Iterator[Engine]:
    """Engine on the compose Postgres with `alembic upgrade head` applied.

    Session-scoped so migrations run once per test run; the tests using it
    are integration-marked and deselected by default (D9).
    """
    from alembic import command
    from alembic.config import Config

    command.upgrade(Config(str(ALEMBIC_INI)), "head")
    engine = create_engine(get_settings().database_url)
    yield engine
    engine.dispose()


@pytest.fixture
def pg(pg_engine: Engine) -> Engine:
    """The migrated Postgres engine with all state tables emptied."""
    with pg_engine.begin() as conn:
        for table in reversed(models.Base.metadata.sorted_tables):
            conn.execute(table.delete())
    return pg_engine
