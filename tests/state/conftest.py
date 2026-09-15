"""Shared fixtures for incident-state tests.

Two tiers, per D9:

- Unit tier: an in-memory SQLite engine. The JSON columns carry a sqlite
  variant (state/models.py) for exactly this, so the HTTP contract and
  dedup logic are testable without infrastructure.
- Integration tier: the compose Postgres (scripts/db-up.sh) with the real
  alembic migration applied by the `pg_engine` fixture - so every
  DB-backed test also proves the migration works.
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
    """Fresh in-memory SQLite database per test. StaticPool keeps one
    shared connection so FastAPI's TestClient threads see the same data."""
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

    Session-scoped: migrations run once per test run. Requires
    scripts/db-up.sh to have been run; marked tests are deselected by
    default so a clean machine never collects them (D9).
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
