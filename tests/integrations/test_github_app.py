"""The Helix GitHub App's auth (integrations/github/app.py): the app JWT, the
cached installation token, and the installation reads. GitHub is faked with
httpx.MockTransport behind app._http; no request leaves the test.
"""

import base64
import json

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from integrations.evidence import EvidenceAuthError
from integrations.github import app
from integrations.github.client import GitHubEvidenceClient
from integrations.github.write import GitHubWriteClient
from integrations.writes import WriteAuthError
from state import connections, db
from state.connections import ConnectionRecord

NOW = 1_800_000_000.0


@pytest.fixture(scope="module")
def key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def pem(key) -> str:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


@pytest.fixture(autouse=True)
def env(monkeypatch, pem):
    monkeypatch.setenv("HELIX_ENV_FILE", "")
    monkeypatch.setenv("HELIX_GITHUB_APP_ID", "12345")
    monkeypatch.setenv("HELIX_GITHUB_APP_PRIVATE_KEY", pem)
    monkeypatch.setattr(app, "_now", lambda: NOW)
    app._token_cache.clear()
    yield
    app._token_cache.clear()


@pytest.fixture
def github(monkeypatch):
    """Fake GitHub: `replies` maps (method, path) to (status, JSON) or a
    callable returning one; `calls` records each request."""
    calls: list[dict] = []
    replies: dict = {
        ("POST", "/app/installations/77/access_tokens"): (
            201,
            {"token": "ghs_first", "expires_at": "2027-01-15T09:00:00Z"},
        ),
        ("GET", "/app/installations/77"): (
            200,
            {
                "id": 77,
                "account": {"login": "VrajVed", "type": "User"},
                "permissions": {"contents": "write", "metadata": "read"},
            },
        ),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(
            {
                "method": request.method,
                "path": request.url.path,
                "query": dict(request.url.params),
                "auth": request.headers.get("authorization", ""),
            }
        )
        reply = replies.get((request.method, request.url.path), (404, {}))
        status, body = reply(request) if callable(reply) else reply
        return httpx.Response(status, json=body)

    monkeypatch.setattr(
        app, "_http", lambda: httpx.Client(transport=httpx.MockTransport(handler), timeout=5)
    )
    return type("GitHub", (), {"calls": calls, "replies": replies})


def _b64(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def test_app_jwt_claims_and_rs256_signature(key):
    token = app.app_jwt()
    header, claims, signature = token.split(".")
    assert json.loads(_b64(header)) == {"alg": "RS256", "typ": "JWT"}
    assert json.loads(_b64(claims)) == {"iat": NOW - 60, "exp": NOW + 540, "iss": "12345"}
    key.public_key().verify(
        _b64(signature), f"{header}.{claims}".encode(), padding.PKCS1v15(), hashes.SHA256()
    )


def test_a_key_with_literal_backslash_n_works(monkeypatch, pem, key):
    monkeypatch.setenv("HELIX_GITHUB_APP_PRIVATE_KEY", pem.strip().replace("\n", "\\n"))
    header, claims, signature = app.app_jwt().split(".")
    key.public_key().verify(
        _b64(signature), f"{header}.{claims}".encode(), padding.PKCS1v15(), hashes.SHA256()
    )


@pytest.mark.parametrize(
    ("env_name", "value", "code"),
    [
        ("HELIX_GITHUB_APP_ID", "", "not_configured"),
        ("HELIX_GITHUB_APP_PRIVATE_KEY", "", "not_configured"),
        ("HELIX_GITHUB_APP_PRIVATE_KEY", "not a pem", "bad_private_key"),
    ],
)
def test_a_missing_or_unreadable_key_is_a_safe_error(monkeypatch, env_name, value, code):
    monkeypatch.setenv(env_name, value)
    with pytest.raises(app.GitHubAppError) as raised:
        app.app_jwt()
    assert str(raised.value) == code


def test_installation_token_is_minted_with_the_app_jwt(github):
    assert app.installation_token("77") == "ghs_first"
    call = github.calls[0]
    assert call["method"] == "POST" and call["auth"].startswith("Bearer ")
    assert call["auth"].count(".") == 2


def test_installation_token_is_cached_until_near_expiry(github, monkeypatch):
    assert app.installation_token("77") == "ghs_first"
    assert app.installation_token("77") == "ghs_first"
    assert len(github.calls) == 1
    expires = app._parse_time("2027-01-15T09:00:00Z")
    monkeypatch.setattr(app, "_now", lambda: expires - 10 * 60)
    assert app.installation_token("77") == "ghs_first"
    assert len(github.calls) == 1


def test_installation_token_is_reminted_within_five_minutes_of_expiry(github, monkeypatch):
    app.installation_token("77")
    github.replies[("POST", "/app/installations/77/access_tokens")] = (
        201,
        {"token": "ghs_second", "expires_at": "2027-01-15T10:00:00Z"},
    )
    expires = app._parse_time("2027-01-15T09:00:00Z")
    monkeypatch.setattr(app, "_now", lambda: expires - 4 * 60)
    assert app.installation_token("77") == "ghs_second"
    assert len(github.calls) == 2


def test_tokens_are_cached_per_installation(github):
    github.replies[("POST", "/app/installations/88/access_tokens")] = (
        201,
        {"token": "ghs_other", "expires_at": "2027-01-15T09:00:00Z"},
    )
    assert app.installation_token("77") == "ghs_first"
    assert app.installation_token("88") == "ghs_other"
    assert app.installation_token("77") == "ghs_first"


@pytest.mark.parametrize("installation_id", ["", "../x", "7/7", "abc", "1" * 30])
def test_a_malformed_installation_id_never_reaches_github(github, installation_id):
    with pytest.raises(app.GitHubAppError) as raised:
        app.installation_token(installation_id)
    assert str(raised.value) == "bad_installation"
    with pytest.raises(app.GitHubAppError):
        app.get_installation(installation_id)
    assert github.calls == []


def test_a_refused_mint_is_a_short_code_and_is_not_cached(github):
    github.replies[("POST", "/app/installations/77/access_tokens")] = (401, {"message": "no"})
    with pytest.raises(app.GitHubAppError) as raised:
        app.installation_token("77")
    assert str(raised.value) == "http_401"
    assert app._token_cache == {}


@pytest.mark.parametrize("body", [{}, {"token": "t"}, {"expires_at": "2027-01-15T09:00:00Z"}])
def test_a_malformed_mint_reply_is_refused(github, body):
    github.replies[("POST", "/app/installations/77/access_tokens")] = (201, body)
    with pytest.raises(app.GitHubAppError) as raised:
        app.installation_token("77")
    assert str(raised.value) == "malformed_response"


def test_transport_failure_is_a_safe_code(monkeypatch):
    def boom(request):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(
        app, "_http", lambda: httpx.Client(transport=httpx.MockTransport(boom), timeout=5)
    )
    with pytest.raises(app.GitHubAppError) as raised:
        app.installation_token("77")
    assert str(raised.value) == "transport_error"


def test_get_installation_uses_the_app_jwt(github):
    found = app.get_installation("77")
    assert found["account"]["login"] == "VrajVed"
    assert github.calls[-1]["path"] == "/app/installations/77"
    assert github.calls[-1]["auth"].count(".") == 2


def test_get_installation_404_is_refused(github):
    github.replies[("GET", "/app/installations/99")] = (404, {"message": "Not Found"})
    with pytest.raises(app.GitHubAppError) as raised:
        app.get_installation("99")
    assert str(raised.value) == "http_404"


def test_list_installation_repos_follows_pages_with_the_installation_token(github):
    def repos(request):
        page = int(request.url.params["page"])
        names = [f"acme/r{n}" for n in range(100)] if page == 1 else ["acme/last"]
        return 200, {"total_count": 101, "repositories": [{"full_name": n} for n in names]}

    github.replies[("GET", "/installation/repositories")] = repos
    found = app.list_installation_repos("77")
    assert len(found) == 101 and found[-1] == "acme/last"
    listing = [c for c in github.calls if c["path"] == "/installation/repositories"]
    assert [c["query"]["page"] for c in listing] == ["1", "2"]
    assert all(c["auth"] == "Bearer ghs_first" for c in listing)


def test_list_installation_repos_is_bounded(github):
    github.replies[("GET", "/installation/repositories")] = (
        200,
        {"repositories": [{"full_name": f"acme/r{n}"} for n in range(100)]},
    )
    assert len(app.list_installation_repos("77")) == 100 * app.MAX_REPO_PAGES


def test_list_installation_repos_malformed_reply(github):
    github.replies[("GET", "/installation/repositories")] = (200, {"repositories": "x"})
    with pytest.raises(app.GitHubAppError) as raised:
        app.list_installation_repos("77")
    assert str(raised.value) == "malformed_response"


def test_nothing_secret_appears_in_an_error_or_log(github, caplog, pem):
    github.replies[("POST", "/app/installations/77/access_tokens")] = (500, {"message": "x"})
    with pytest.raises(app.GitHubAppError) as raised:
        app.installation_token("77")
    text = str(raised.value) + caplog.text + repr(raised.value)
    assert "PRIVATE KEY" not in text and "ghs_" not in text
    assert github.calls[0]["auth"].split(" ", 1)[1] not in text


# -- the clients: explicit token > connected installation > HELIX_GITHUB_TOKEN


SERVER = "ghp_server_env_token"


def _record(installation_id="77", token=None):
    return ConnectionRecord(
        provider="github",
        account="VrajVed",
        scopes=["contents:write"],
        access_token=token,
        refresh_token=None,
        expires_at=None,
        settings={"installation_id": installation_id, "account_type": "User"},
    )


@pytest.fixture
def installed(monkeypatch):
    asked: list[str] = []

    def fake(provider):
        asked.append(provider)
        return _record()

    monkeypatch.setattr(connections, "load_connection", fake)
    return asked


@pytest.fixture
def not_installed(monkeypatch):
    monkeypatch.setattr(connections, "load_connection", lambda provider: None)


@pytest.fixture
def server_token(monkeypatch):
    monkeypatch.setenv("HELIX_GITHUB_TOKEN", SERVER)
    monkeypatch.setenv("HELIX_GITHUB_REPO", "acme/sbx")


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
class TestInstallationTokenResolution:
    def test_the_installation_token_wins_over_the_env_token(
        self, make, act, github, installed, server_token
    ):
        seen: list = []
        act(make(seen))
        assert seen == ["Bearer ghs_first"]

    def test_falls_back_to_the_env_token_when_not_installed(
        self, make, act, github, not_installed, server_token
    ):
        seen: list = []
        act(make(seen))
        assert seen == [f"Bearer {SERVER}"] and github.calls == []

    def test_an_explicit_token_beats_the_installation(
        self, make, act, github, installed, server_token
    ):
        seen: list = []
        act(make(seen, token="explicit"))
        assert seen == ["Bearer explicit"] and installed == [] and github.calls == []

    def test_the_store_is_read_once_and_not_at_construction(
        self, make, act, github, installed, server_token
    ):
        seen: list = []
        client = make(seen)
        assert installed == []
        act(client)
        act(client)
        assert installed == ["github"]

    def test_each_request_resolves_through_the_cache(
        self, make, act, github, installed, server_token, monkeypatch
    ):
        """An installation token lasts an hour, so it is resolved per request:
        a long run re-mints near expiry instead of failing mid-way."""
        seen: list = []
        client = make(seen)
        act(client)
        act(client)
        assert len([c for c in github.calls if c["method"] == "POST"]) == 1
        github.replies[("POST", "/app/installations/77/access_tokens")] = (
            201,
            {"token": "ghs_second", "expires_at": "2027-01-15T11:00:00Z"},
        )
        monkeypatch.setattr(app, "_now", lambda: NOW + 3500)
        act(client)
        assert seen == ["Bearer ghs_first", "Bearer ghs_first", "Bearer ghs_second"]

    def test_isolation_ignores_the_installation(self, make, act, github, server_token, monkeypatch):
        monkeypatch.setenv("HELIX_CONNECTIONS_ENABLED", "false")
        monkeypatch.setattr(
            db, "get_engine", lambda *a, **k: pytest.fail("the store must not be opened")
        )
        seen: list = []
        act(make(seen))
        assert seen == [f"Bearer {SERVER}"] and github.calls == []


def test_the_write_client_refuses_when_the_installation_token_cannot_be_minted(
    github, installed, server_token
):
    github.replies[("POST", "/app/installations/77/access_tokens")] = (401, {"message": "no"})
    with pytest.raises(WriteAuthError):
        _write(_write_client([]))


def test_the_evidence_client_fails_auth_when_the_installation_token_cannot_be_minted(
    github, installed, server_token
):
    github.replies[("POST", "/app/installations/77/access_tokens")] = (401, {"message": "no"})
    with pytest.raises(EvidenceAuthError):
        _evidence_client([])._get("/repos/acme/sbx")


def test_a_refusal_logs_githubs_reason_but_never_the_token(github, monkeypatch):
    """A bare http_401 cannot tell a wrong App ID from an undecodable key;
    GitHub's own message can, and it carries no secret."""
    logged = []

    class Recorder:
        def warning(self, event, **fields):
            logged.append((event, fields))

    monkeypatch.setattr(app, "get_logger", lambda: Recorder())
    github.replies[("GET", "/app/installations/77")] = (
        401,
        {"message": "A JSON web token could not be decoded"},
    )
    with pytest.raises(app.GitHubAppError):
        app.get_installation("77")
    assert logged == [
        (
            "github app call refused",
            {
                "path": "/app/installations/77",
                "status": 401,
                "reason": "A JSON web token could not be decoded",
            },
        )
    ]
