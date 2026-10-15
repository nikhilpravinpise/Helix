"""The fix-PR bot uses a connected GitHub account before HELIX_GITHUB_TOKEN
(future_plans.md Stage 1, slice 8). The store is faked or a throwaway SQLite
file; no request leaves the test."""

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from integrations.github import write as write_module
from integrations.github.client import GitHubEvidenceClient
from integrations.github.write import GitHubWriteClient
from state import connections, db, models
from state.connections import ConnectionRecord

STORED = "gho_stored_connection_token"
SERVER = "ghp_server_env_token"


def _record(token=STORED):
    return ConnectionRecord(
        provider="github",
        account="VrajVed",
        scopes=["repo"],
        access_token=token,
        refresh_token=None,
        expires_at=None,
        settings={},
    )


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("HELIX_ENV_FILE", "")
    monkeypatch.setenv("HELIX_GITHUB_TOKEN", SERVER)
    monkeypatch.setenv("HELIX_GITHUB_REPO", "acme/sbx")
    monkeypatch.delenv("HELIX_CONNECTIONS_ENABLED", raising=False)


@pytest.fixture
def connected(monkeypatch):
    """load_connection answers with a stored token and counts its calls."""
    asked: list[str] = []

    def fake(provider):
        asked.append(provider)
        return _record()

    monkeypatch.setattr(connections, "load_connection", fake)
    return asked


@pytest.fixture
def not_connected(monkeypatch):
    monkeypatch.setattr(connections, "load_connection", lambda provider: None)


def _write_client(seen, **kwargs):
    def handler(request):
        seen.append(request.headers.get("authorization"))
        return httpx.Response(200, json={"sha": "s"})

    return GitHubWriteClient(client=httpx.Client(transport=httpx.MockTransport(handler)), **kwargs)


def _evidence_client(seen, **kwargs):
    def handler(request):
        seen.append(request.headers.get("authorization"))
        return httpx.Response(200, json={"sha": "s", "commit": {}, "parents": []})

    return GitHubEvidenceClient(
        client=httpx.Client(
            base_url="https://api.github.com", transport=httpx.MockTransport(handler)
        ),
        **kwargs,
    )


def _write(client):
    client._request("GET", "/repos/acme/sbx")


def _read(client):
    try:
        client._get("/repos/acme/sbx")
    except Exception:  # only the Authorization header matters here
        pass


@pytest.mark.parametrize(
    ("make", "act"), [(_write_client, _write), (_evidence_client, _read)], ids=["write", "evidence"]
)
class TestTokenResolution:
    def test_connected_account_wins_over_env(self, make, act, connected):
        seen: list = []
        act(make(seen))
        assert seen == [f"Bearer {STORED}"]

    def test_falls_back_to_env_when_not_connected(self, make, act, not_connected):
        seen: list = []
        act(make(seen))
        assert seen == [f"Bearer {SERVER}"]

    def test_explicit_token_beats_both_and_skips_the_store(self, make, act, connected):
        seen: list = []
        act(make(seen, token="explicit"))
        assert seen == ["Bearer explicit"] and connected == []

    def test_resolved_once_not_at_construction(self, make, act, connected):
        seen: list = []
        client = make(seen)
        assert connected == []
        act(client)
        act(client)
        assert connected == ["github"]

    def test_isolation_ignores_the_store(self, make, act, monkeypatch):
        monkeypatch.setenv("HELIX_CONNECTIONS_ENABLED", "false")
        monkeypatch.setattr(
            db, "get_engine", lambda *a, **k: pytest.fail("the store must not be opened")
        )
        seen: list = []
        act(make(seen))
        assert seen == [f"Bearer {SERVER}"]


def test_write_client_without_any_token_still_refuses(not_connected, monkeypatch):
    monkeypatch.delenv("HELIX_GITHUB_TOKEN")
    with pytest.raises(write_module.WriteAuthError):
        _write(_write_client([]))


class TestLoadConnection:
    @pytest.fixture
    def store(self, tmp_path, monkeypatch):
        url = f"sqlite:///{tmp_path / 'helix.db'}"
        monkeypatch.setenv("HELIX_DATABASE_URL", url)
        monkeypatch.setenv("HELIX_CONNECTIONS_KEY", "test connections key")
        engine = create_engine(url)
        models.Base.metadata.create_all(engine)
        yield engine
        engine.dispose()
        db._engines.pop(url, None)

    def test_reads_the_stored_connection(self, store):
        with Session(store) as session:
            connections.save_connection(
                session,
                "github",
                account="VrajVed",
                scopes=["repo"],
                access_token=STORED,
                refresh_token=None,
                expires_at=None,
                settings={},
            )
            session.commit()
        record = connections.load_connection("github")
        assert record is not None and record.access_token == STORED

    def test_nothing_stored_is_none(self, store):
        assert connections.load_connection("github") is None

    def test_disabled_is_none_without_opening_the_store(self, store, monkeypatch):
        monkeypatch.setenv("HELIX_CONNECTIONS_ENABLED", "false")
        monkeypatch.setattr(db, "get_engine", lambda *a, **k: pytest.fail("opened"))
        assert connections.load_connection("github") is None

    def test_unreachable_store_is_none(self, tmp_path, monkeypatch):
        url = f"sqlite:///{tmp_path / 'missing' / 'helix.db'}"
        monkeypatch.setenv("HELIX_DATABASE_URL", url)
        monkeypatch.setenv("HELIX_CONNECTIONS_KEY", "test connections key")
        assert connections.load_connection("github") is None
        db._engines.pop(url, None)

    def test_missing_table_is_none(self, tmp_path, monkeypatch):
        url = f"sqlite:///{tmp_path / 'empty.db'}"
        monkeypatch.setenv("HELIX_DATABASE_URL", url)
        monkeypatch.setenv("HELIX_CONNECTIONS_KEY", "test connections key")
        assert connections.load_connection("github") is None
        db._engines.pop(url, None)
