"""UI route-test fixtures (subphase 1.12).

Unit tier: FastAPI TestClient with BOTH session dependencies (the API's
and the UI's own - helix/ui.py deliberately keeps a separate dependency
so it never imports helix.api) overridden to the same in-memory SQLite
engine. Workflow runs are driven through the real runner via
tests/agent/fakes.py make_deps (fixture capabilities + scripted FakeLLM).

Integration tier: the compose Postgres via pg_engine/pg (same pattern as
tests/state) - the unmodified app serving real pages.
"""

import functools
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine
from sqlalchemy.pool import StaticPool

# tests/agent/fakes.py is the shared test-double module; pytest only puts
# THIS directory on sys.path, so expose the sibling directory explicitly.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))

from helix import api, ui  # noqa: E402
from helix.config import get_settings  # noqa: E402
from integrations.fixtures import FixtureStore  # noqa: E402
from state import models  # noqa: E402
from state.db import session_scope  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "state" / "migrations" / "alembic.ini"


@pytest.fixture
def sqlite_engine() -> Iterator[Engine]:
    """Fresh in-memory SQLite per test (StaticPool shares one connection
    so TestClient threads see the same data)."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    models.Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture(autouse=True)
def no_provider_network(monkeypatch):
    """The Connections page lists Slack channels and Jira projects for a
    connected card. Without a test's own fake, those calls fail fast here
    instead of reaching the real services."""
    import httpx

    from integrations.slack import oauth as slack_oauth
    from integrations.work import jira_oauth

    def offline(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline in tests")

    def client() -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(offline), timeout=5)

    monkeypatch.setattr(slack_oauth, "_http", client)
    monkeypatch.setattr(jira_oauth, "_http", client)


@pytest.fixture
def sqlite_sessions(sqlite_engine: Engine):
    """A session_scope-equivalent bound to the test SQLite engine."""
    return functools.partial(session_scope, sqlite_engine)


@pytest.fixture
def store() -> FixtureStore:
    return FixtureStore()


@pytest.fixture
def client(sqlite_engine: Engine) -> Iterator[TestClient]:
    """TestClient with api + ui session deps on the test SQLite engine."""

    def override() -> Iterator:
        with session_scope(sqlite_engine) as session:
            yield session

    app = api.app
    app.dependency_overrides[api.get_db_session] = override
    app.dependency_overrides[ui.get_db_session] = override
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


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
    """The migrated Postgres engine with all Helix state tables emptied."""
    with pg_engine.begin() as conn:
        for table in reversed(models.Base.metadata.sorted_tables):
            conn.execute(table.delete())
    return pg_engine
