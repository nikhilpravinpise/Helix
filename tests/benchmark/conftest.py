"""Benchmark test fixtures: the same unit-tier seam as tests/agent -
in-memory SQLite, fixture-backed capabilities, scripted FakeLLM."""

import functools
import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine
from sqlalchemy.pool import StaticPool

# tests/agent/fakes.py is the shared test-double module; pytest only puts
# THIS directory on sys.path, so expose the sibling directory explicitly.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))

from fakes import make_deps  # noqa: E402  (path shim above must run first)

from integrations.fixtures import FixtureStore  # noqa: E402
from state import models  # noqa: E402
from state.db import session_scope  # noqa: E402


@pytest.fixture(autouse=True)
def _restore_environ() -> Iterator[None]:
    """run_benchmark pins its isolation (HELIX_CONNECTIONS_ENABLED=false,
    the benchmark registry, ...) into os.environ on purpose - it is the
    CLI's whole process. In-process calls here must not carry that into
    every later test."""
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)


@pytest.fixture
def sqlite_engine() -> Iterator[Engine]:
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
    return functools.partial(session_scope, sqlite_engine)


@pytest.fixture
def store() -> FixtureStore:
    return FixtureStore()


@pytest.fixture
def deps_factory():
    """make_deps passthrough so arm tests read uniformly."""
    return make_deps
