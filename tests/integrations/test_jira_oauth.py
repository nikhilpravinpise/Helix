"""Jira over a stored OAuth connection (future_plans.md Stage 1, slice 5):
bearer requests through the Atlassian gateway, proactive and 401-driven
refresh with rotating refresh tokens, and the stored connection winning
over .env in the drain's resolution.
"""

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from benchmark import harness
from integrations.outbox import resolve_work_for_session
from integrations.work import JiraProvider, MemoryWorkProvider, WorkItemError, WorkItemSpec
from state import connections, models

CLOUD = "cloud-123"
SITE = "https://acme.atlassian.net"
API = f"https://api.atlassian.com/ex/jira/{CLOUD}"


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("HELIX_ENV_FILE", "")
    monkeypatch.setenv("HELIX_CONNECTIONS_KEY", "test connections key")
    monkeypatch.setenv("HELIX_JIRA_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("HELIX_JIRA_OAUTH_CLIENT_SECRET", "csecret")
    monkeypatch.delenv("HELIX_CONNECTIONS_ENABLED", raising=False)


def _spec() -> WorkItemSpec:
    return WorkItemSpec(
        incident_id="INC-1",
        title="t",
        description="d",
        severity="high",
        priority="high",
        labels=[],
    )


class Backend:
    """Mock Atlassian: records requests, serves the issue API and the
    token endpoint from scripted queues."""

    def __init__(self, *, issue_statuses=(201,), token_responses=()):
        self.seen: list[httpx.Request] = []
        self.issue_statuses = list(issue_statuses)
        self.token_responses = list(token_responses)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        if request.url.host == "auth.atlassian.com":
            status, body = self.token_responses.pop(0)
            return httpx.Response(status, json=body)
        status = self.issue_statuses.pop(0) if self.issue_statuses else 201
        if status >= 400:
            return httpx.Response(status, json={})
        return httpx.Response(status, json={"id": "10001", "key": "HELIX-1"})

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))

    def token_bodies(self):
        return [json.loads(r.content) for r in self.seen if r.url.host == "auth.atlassian.com"]


def _provider(backend, *, expires_in=3600, refresh="r1", saved=None):
    return JiraProvider.from_oauth(
        cloud_id=CLOUD,
        site_url=SITE,
        project_key="OPS",
        access_token="a1",
        refresh_token=refresh,
        expires_at=datetime.now(UTC) + timedelta(seconds=expires_in),
        client_id="cid",
        client_secret="csecret",
        on_refresh=(lambda *args: saved.append(args)) if saved is not None else None,
        client=backend.client(),
    )


ROTATED = (200, {"access_token": "a2", "refresh_token": "r2", "expires_in": 3600})


def test_bearer_mode_uses_atlassian_gateway_and_site_browse_url():
    backend = Backend()
    ref = _provider(backend).create_work_item(_spec())
    request = backend.seen[0]
    assert str(request.url) == f"{API}/rest/api/3/issue"
    assert request.headers["Authorization"] == "Bearer a1"
    assert ref.external_url == f"{SITE}/browse/HELIX-1"


def test_get_work_item_browse_url_uses_site_url():
    backend = Backend()
    ref = _provider(backend).get_work_item("HELIX-1")
    assert ref.external_url == f"{SITE}/browse/HELIX-1"


def test_near_expiry_refreshes_first_and_persists_rotated_pair():
    backend = Backend(token_responses=[ROTATED])
    saved = []
    _provider(backend, expires_in=30, saved=saved).create_work_item(_spec())
    assert backend.seen[0].url.host == "auth.atlassian.com"
    assert backend.token_bodies() == [
        {
            "grant_type": "refresh_token",
            "client_id": "cid",
            "client_secret": "csecret",
            "refresh_token": "r1",
        }
    ]
    assert backend.seen[1].headers["Authorization"] == "Bearer a2"
    assert len(saved) == 1 and saved[0][:2] == ("a2", "r2")
    assert saved[0][2] > datetime.now(UTC) + timedelta(minutes=50)


def test_fresh_token_does_not_refresh():
    backend = Backend()
    _provider(backend).create_work_item(_spec())
    assert backend.token_bodies() == []


def test_second_refresh_uses_the_rotated_refresh_token():
    backend = Backend(
        token_responses=[ROTATED, (200, {"access_token": "a3", "refresh_token": "r3"})]
    )
    provider = _provider(backend, expires_in=30)
    provider.create_work_item(_spec())
    provider._oauth.expires_at = datetime.now(UTC)
    provider.create_work_item(_spec())
    assert [b["refresh_token"] for b in backend.token_bodies()] == ["r1", "r2"]


def test_401_refreshes_once_and_retries_once():
    backend = Backend(issue_statuses=[401, 201], token_responses=[ROTATED])
    saved = []
    ref = _provider(backend, saved=saved).create_work_item(_spec())
    assert ref.external_key == "HELIX-1"
    assert [r.url.host for r in backend.seen] == [
        "api.atlassian.com",
        "auth.atlassian.com",
        "api.atlassian.com",
    ]
    assert backend.seen[2].headers["Authorization"] == "Bearer a2"
    assert saved[0][:2] == ("a2", "r2")


def test_second_401_raises_without_looping():
    backend = Backend(issue_statuses=[401, 401, 401], token_responses=[ROTATED, ROTATED])
    with pytest.raises(WorkItemError):
        _provider(backend).create_work_item(_spec())
    assert len(backend.seen) == 3


def test_invalid_grant_tells_the_operator_to_reconnect():
    backend = Backend(token_responses=[(403, {"error": "invalid_grant"})])
    saved = []
    with pytest.raises(WorkItemError, match="reconnect Jira") as info:
        _provider(backend, expires_in=0, saved=saved).create_work_item(_spec())
    assert "invalid_grant" in str(info.value)
    assert "r1" not in str(info.value) and "csecret" not in str(info.value)
    assert saved == []


def test_missing_oauth_client_settings_raises_reconnect_error():
    backend = Backend()
    provider = _provider(backend, expires_in=0)
    provider._oauth.client_secret = None
    with pytest.raises(WorkItemError, match="reconnect Jira"):
        provider.create_work_item(_spec())


def test_basic_mode_is_unchanged():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(201, json={"id": "1", "key": "HELIX-1"})

    provider = JiraProvider(
        "https://jira.test", "bot@x.io", "tok", "HELIX",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )  # fmt: skip
    provider.create_work_item(_spec())
    assert seen[0].url.host == "jira.test"
    assert seen[0].headers["Authorization"].startswith("Basic ")


# --- resolution -------------------------------------------------------------


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    models.Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as s:
        yield s
    engine.dispose()


def _connect(session, **settings):
    connections.save_connection(
        session,
        "jira",
        account="Acme",
        scopes=["write:jira-work"],
        access_token="stored-access",
        refresh_token="stored-refresh",
        expires_at=datetime.now(UTC) + timedelta(seconds=10),
        settings={"cloud_id": CLOUD, "site_url": SITE, **settings},
    )
    session.commit()


def test_stored_connection_wins_over_env(session, monkeypatch):
    monkeypatch.setenv("HELIX_JIRA_API_TOKEN", "env-token")
    _connect(session, project_key="STORED")
    provider = resolve_work_for_session(session, "jira")
    assert isinstance(provider, JiraProvider)
    assert provider._oauth is not None
    assert provider._base_url == API
    assert provider._project_key == "STORED"


def test_stored_project_key_absent_falls_back_to_env_project(session, monkeypatch):
    monkeypatch.setenv("HELIX_JIRA_PROJECT_KEY", "ENVPROJ")
    _connect(session)
    assert resolve_work_for_session(session, "jira")._project_key == "ENVPROJ"


def test_no_connection_uses_env_path(session, monkeypatch):
    monkeypatch.setenv("HELIX_JIRA_API_TOKEN", "env-token")
    provider = resolve_work_for_session(session, "jira")
    assert isinstance(provider, JiraProvider) and provider._oauth is None


def test_other_kinds_ignore_the_jira_connection(session):
    _connect(session)
    assert isinstance(resolve_work_for_session(session, "memory"), MemoryWorkProvider)


def test_connection_without_cloud_id_falls_back(session, monkeypatch):
    monkeypatch.setenv("HELIX_JIRA_API_TOKEN", "")
    _connect(session, cloud_id="")
    assert resolve_work_for_session(session, "jira") is None


def test_benchmark_isolation_ignores_stored_connection(session, monkeypatch):
    _connect(session)
    for name, value in harness._BENCHMARK_ISOLATION.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("HELIX_JIRA_API_TOKEN", "")
    assert resolve_work_for_session(session, "jira") is None


def test_resolver_saves_rotated_tokens_in_its_own_transaction(session):
    _connect(session)
    backend = Backend(token_responses=[ROTATED])
    provider = resolve_work_for_session(session, "jira")
    provider._client = backend.client()
    # The drain session is mid-transaction with unrelated work, then rolls back.
    session.add(
        models.Incident(
            incident_id="INC-X",
            kind=models.IncidentKind("change"),
            service="s",
            environment="sandbox",
            severity="high",
            status="received",
            fingerprint="fp-x",
        )
    )
    provider.create_work_item(_spec())
    session.rollback()
    record = connections.get_connection(session, "jira")
    assert (record.access_token, record.refresh_token) == ("a2", "r2")
