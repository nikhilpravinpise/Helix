"""Shared fixtures for graph tests: reuse the tests/state SQLite pattern
so ingestion is exercised against real ORM rows, not hand-built dicts."""

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine
from sqlalchemy.pool import StaticPool

from state import models


@pytest.fixture
def sqlite_engine() -> Iterator[Engine]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    models.Base.metadata.create_all(engine)
    yield engine
    engine.dispose()
