"""Connect GitHub on the Connections page, as an installation of the Helix
GitHub App (replaces the OAuth App connect of slice 8).

GitHub is faked with httpx.MockTransport behind integrations.github.app._http
(and oauth._http for the branch check); no request leaves the test. The page
runs signed in, as it does in use.
"""

import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy.orm import Session

from helix import auth
from integrations.github import app, oauth
from state import connections, models

INSTALLED_PATH = "/ui/connections/github/installed"
INSTALLATION = {
    "id": 77,
    "account": {"login": "VrajVed", "type": "User"},
    "permissions": {"pull_requests": "write", "contents": "write", "metadata": "read"},
}
SCOPES = ["contents:write", "metadata:read", "pull_requests:write"]


@pytest.fixture(scope="module")
def pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


@pytest.fixture(autouse=True)
def env(monkeypatch, pem):
    monkeypatch.setenv("HELIX_ENV_FILE", "")
    monkeypatch.setenv("HELIX_PUBLIC_URL", "https://helix.example")
    monkeypatch.setenv("HELIX_SESSION_SECRET", "test-session-secret")
    monkeypatch.setenv("HELIX_CONNECTIONS_KEY", "test connections key")
    # The sign-in OAuth App is what turns the sign-in gate on.
    monkeypatch.setenv("HELIX_GITHUB_OAUTH_CLIENT_ID", "gh-cid")
    monkeypatch.setenv("HELIX_GITHUB_OAUTH_CLIENT_SECRET", "gh-csecret")
    monkeypatch.setenv("HELIX_GITHUB_APP_ID", "12345")
    monkeypatch.setenv("HELIX_GITHUB_APP_SLUG", "helix-bot")
    monkeypatch.setenv("HELIX_GITHUB_APP_PRIVATE_KEY", pem)
    monkeypatch.setenv("HELIX_ALLOWED_USERS", "VrajVed,nikhilpravinpise")
    monkeypatch.setenv("HELIX_GITHUB_REPO", "acme/server-default")
    for name in ("HELIX_GITHUB_TOKEN", "HELIX_CONNECTIONS_ENABLED", "HELIX_GITHUB_WRITES"):
        monkeypatch.delenv(name, raising=False)
    app._token_cache.clear()
    yield
    app._token_cache.clear()


def _sign_in(client, login="VrajVed"):
    client.cookies.clear()
    client.cookies.set(
        auth.SESSION_COOKIE,
        auth._sign({"login": login, "exp": time.time() + 3600}),
        domain="helix.example",
    )


@pytest.fixture(autouse=True)
def https(client):
    """The state cookie is Secure under the https public URL."""
    client.base_url = "https://helix.example"
    _sign_in(client)


@pytest.fixture
def github(monkeypatch):
    """Fake GitHub: `replies` maps (method, path) to (status, JSON)."""
    calls: list[dict] = []
    replies: dict[tuple[str, str], tuple[int, object]] = {
        ("GET", "/app/installations/77"): (200, INSTALLATION),
        ("POST", "/app/installations/77/access_tokens"): (
            201,
            {"token": "ghs_installation", "expires_at": "2099-01-01T00:00:00Z"},
        ),
        ("GET", "/installation/repositories"): (
            200,
            {
                "total_count": 2,
                "repositories": [{"full_name": "VrajVed/helix-sandbox"}, {"full_name": "acme/x"}],
            },
        ),
        ("GET", "/repos/VrajVed/helix-sandbox/branches/main"): (200, {"name": "main"}),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(
            {
                "method": request.method,
                "path": request.url.path,
                "auth": request.headers.get("authorization", ""),
            }
        )
        status, body = replies.get((request.method, request.url.path), (404, {}))
        return httpx.Response(status, json=body)

    def make():
        return httpx.Client(transport=httpx.MockTransport(handler), timeout=5)

    monkeypatch.setattr(app, "_http", make)
    monkeypatch.setattr(oauth, "_http", make)
    return type("GitHub", (), {"calls": calls, "replies": replies})


def _start(client) -> str:
    response = client.post("/ui/connections/github/connect", follow_redirects=False)
    return parse_qs(urlparse(response.headers["location"]).query)["state"][0]


def _installed(client, state=None, query="installation_id=77&setup_action=install"):
    state = state or _start(client)
    return client.get(f"{INSTALLED_PATH}?{query}&state={state}", follow_redirects=False)


def _stored(sqlite_engine):
    with Session(sqlite_engine) as session:
        return connections.get_connection(session, "github")


def _save(sqlite_engine, installation_id="77", account="VrajVed"):
    with Session(sqlite_engine) as session:
        connections.save_connection(
            session,
            "github",
            account=account,
            scopes=SCOPES,
            access_token=None,
            refresh_token=None,
            expires_at=None,
            settings={"installation_id": installation_id, "account_type": "User"},
        )
        session.commit()


def _card(page: str) -> str:
    return page.split('id="github"')[1].split("</section>")[0]


# -- the card ---------------------------------------------------------------


def test_card_not_connected_offers_install_on_github(client):
    card = _card(client.get("/ui/connections").text)
    assert "Not connected" in card and "Install on GitHub" in card
    assert 'action="/ui/connections/github/connect"' in card
    assert "disabled" not in card.split("github/connect")[1].split("</form>")[0]


def test_card_not_connected_with_server_token_says_so(client, monkeypatch):
    monkeypatch.setenv("HELIX_GITHUB_TOKEN", "server-github-token")
    card = _card(client.get("/ui/connections").text)
    assert "HELIX_GITHUB_TOKEN" in card
    assert "server-github-token" not in card


@pytest.mark.parametrize(
    "name",
    [
        "HELIX_CONNECTIONS_KEY",
        "HELIX_GITHUB_APP_ID",
        "HELIX_GITHUB_APP_SLUG",
        "HELIX_GITHUB_APP_PRIVATE_KEY",
    ],
)
def test_install_disabled_naming_the_missing_setting(client, monkeypatch, name):
    monkeypatch.delenv(name)
    card = _card(client.get("/ui/connections").text)
    assert "disabled" in card.split("github/connect")[1].split("</form>")[0]
    assert name in card
    refused = client.post("/ui/connections/github/connect", follow_redirects=False)
    assert refused.status_code == 303
    assert "github.com" not in refused.headers["location"]
    assert refused.headers["location"].startswith("/ui/connections?")


def test_install_disabled_without_a_session_secret(client, monkeypatch):
    """Sign-in is off too (it needs the secret), so the page opens unsigned."""
    monkeypatch.delenv("HELIX_SESSION_SECRET")
    monkeypatch.delenv("HELIX_GITHUB_OAUTH_CLIENT_ID")
    card = _card(client.get("/ui/connections").text)
    assert "disabled" in card.split("github/connect")[1].split("</form>")[0]
    assert "HELIX_SESSION_SECRET" in card


def test_card_connected_shows_account_repositories_and_permissions(client, github, sqlite_engine):
    _save(sqlite_engine)
    card = _card(client.get("/ui/connections").text)
    assert "Connected" in card and "VrajVed" in card
    assert "2 repositories" in card and "VrajVed/helix-sandbox" in card
    assert "pull_requests:write" in card and "contents:write" in card
    assert 'href="https://github.com/settings/installations/77"' in card
    assert "Manage repositories" in card
    assert "/ui/connections/github/test" in card
    assert "/ui/connections/github/disconnect" in card
    assert "Install on GitHub" not in card
    assert "ghs_installation" not in card


def test_card_says_uninstalling_is_done_on_github(client, github, sqlite_engine):
    _save(sqlite_engine)
    card = _card(client.get("/ui/connections").text)
    assert "uninstall" in card.lower() and "GitHub" in card


def test_card_connected_survives_a_repository_list_failure(client, github, sqlite_engine):
    _save(sqlite_engine)
    github.replies[("GET", "/installation/repositories")] = (500, {})
    page = client.get("/ui/connections")
    assert page.status_code == 200
    assert "Could not load the repository list" in _card(page.text)


# -- starting the install ---------------------------------------------------


def test_connect_redirects_to_the_app_install_page_with_state(client):
    response = client.post("/ui/connections/github/connect", follow_redirects=False)
    assert response.status_code == 303
    target = urlparse(response.headers["location"])
    assert (target.netloc, target.path) == ("github.com", "/apps/helix-bot/installations/new")
    assert len(parse_qs(target.query)["state"][0]) >= 16
    assert "helix_connect_state" in response.headers["set-cookie"]


def test_the_setup_url_is_behind_the_sign_in_gate(client, github, sqlite_engine):
    state = _start(client)
    client.cookies.delete(auth.SESSION_COOKIE, domain="helix.example")
    response = client.get(
        f"{INSTALLED_PATH}?installation_id=77&setup_action=install&state={state}",
        follow_redirects=False,
    )
    assert response.status_code == 303 and response.headers["location"] == "/login"
    assert github.calls == [] and _stored(sqlite_engine) is None


# -- coming back from GitHub ------------------------------------------------


@pytest.mark.parametrize(
    "query",
    [
        "installation_id=77&setup_action=install&state=wrong",
        "installation_id=77&setup_action=install",
        "installation_id=77&setup_action=update&state=wrong",
        "installation_id=77&state=x",
    ],
)
def test_bad_or_missing_state_refuses_and_saves_nothing(client, github, sqlite_engine, query):
    _start(client)
    response = client.get(f"{INSTALLED_PATH}?{query}", follow_redirects=False)
    assert response.status_code == 400
    assert github.calls == []
    assert _stored(sqlite_engine) is None


def test_no_cookie_at_all_refuses(client, github, sqlite_engine):
    response = client.get(
        f"{INSTALLED_PATH}?installation_id=77&setup_action=install&state=x", follow_redirects=False
    )
    assert response.status_code == 400 and _stored(sqlite_engine) is None


def test_a_state_another_user_started_is_refused(client, github, sqlite_engine):
    state = _start(client)
    cookie = client.cookies.get("helix_connect_state", domain="helix.example")
    _sign_in(client, "nikhilpravinpise")
    client.cookies.set("helix_connect_state", cookie, domain="helix.example")
    response = client.get(
        f"{INSTALLED_PATH}?installation_id=77&setup_action=install&state={state}",
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert github.calls == [] and _stored(sqlite_engine) is None


def test_a_forged_installation_id_is_refused_and_saves_nothing(client, github, sqlite_engine):
    github.replies[("GET", "/app/installations/999")] = (404, {"message": "Not Found"})
    response = _installed(client, query="installation_id=999&setup_action=install")
    assert "notice=github-failed&detail=http_404" in response.headers["location"]
    assert _stored(sqlite_engine) is None
    page = client.get(response.headers["location"]).text
    assert "http_404" in page and "Nothing was saved" in page


@pytest.mark.parametrize("installation_id", ["", "abc", "7%2F7", "..%2Fx"])
def test_a_malformed_installation_id_is_refused_without_a_request(
    client, github, sqlite_engine, installation_id
):
    response = _installed(client, query=f"installation_id={installation_id}&setup_action=install")
    assert "notice=github-failed" in response.headers["location"]
    assert github.calls == [] and _stored(sqlite_engine) is None


def test_success_saves_account_permissions_and_installation_id(client, github, sqlite_engine):
    response = _installed(client)
    assert response.status_code == 303
    assert "notice=github-connected" in response.headers["location"]
    verify = github.calls[0]
    assert (verify["method"], verify["path"]) == ("GET", "/app/installations/77")
    assert verify["auth"].count(".") == 2  # the app JWT, not an installation token
    stored = _stored(sqlite_engine)
    assert stored is not None
    assert (stored.account, stored.scopes) == ("VrajVed", SCOPES)
    assert stored.settings == {"installation_id": "77", "account_type": "User"}
    assert stored.access_token is None and stored.refresh_token is None
    with Session(sqlite_engine) as session:
        row = session.get(models.Connection, "github")
        assert row.access_token is None
    assert "helix_connect_state" in response.headers["set-cookie"]


def test_the_state_is_single_use_for_the_browser(client, github, sqlite_engine):
    state = _start(client)
    _installed(client, state=state)
    again = client.get(
        f"{INSTALLED_PATH}?installation_id=77&setup_action=install&state={state}",
        follow_redirects=False,
    )
    assert again.status_code == 400


def test_update_with_state_refreshes_the_stored_details(client, github, sqlite_engine):
    _save(sqlite_engine)
    github.replies[("GET", "/app/installations/77")] = (
        200,
        {**INSTALLATION, "permissions": {"contents": "read"}},
    )
    response = _installed(client, query="installation_id=77&setup_action=update")
    assert "notice=github-connected" in response.headers["location"]
    assert _stored(sqlite_engine).scopes == ["contents:read"]


def test_update_redirect_from_github_without_state_refreshes_the_same_installation(
    client, github, sqlite_engine
):
    """Changing repositories on github.com redirects to the Setup URL with no
    state; that is accepted only for the installation already connected."""
    _save(sqlite_engine)
    github.replies[("GET", "/app/installations/77")] = (
        200,
        {**INSTALLATION, "permissions": {"contents": "read"}},
    )
    response = client.get(
        f"{INSTALLED_PATH}?installation_id=77&setup_action=update", follow_redirects=False
    )
    assert "notice=github-connected" in response.headers["location"]
    assert _stored(sqlite_engine).scopes == ["contents:read"]


def test_update_without_state_for_another_installation_is_refused(client, github, sqlite_engine):
    _save(sqlite_engine)
    github.replies[("GET", "/app/installations/88")] = (200, {**INSTALLATION, "id": 88})
    response = client.get(
        f"{INSTALLED_PATH}?installation_id=88&setup_action=update", follow_redirects=False
    )
    assert response.status_code == 400
    assert _stored(sqlite_engine).settings["installation_id"] == "77"


def test_update_without_state_when_nothing_is_connected_is_refused(client, github, sqlite_engine):
    response = client.get(
        f"{INSTALLED_PATH}?installation_id=77&setup_action=update", follow_redirects=False
    )
    assert response.status_code == 400 and _stored(sqlite_engine) is None


def test_installing_without_a_key_saves_nothing(client, github, sqlite_engine, monkeypatch):
    state = _start(client)
    monkeypatch.delenv("HELIX_CONNECTIONS_KEY")
    response = client.get(
        f"{INSTALLED_PATH}?installation_id=77&setup_action=install&state={state}",
        follow_redirects=False,
    )
    assert "notice=github-unconfigured" in response.headers["location"]
    assert _stored(sqlite_engine) is None


# -- test and disconnect ----------------------------------------------------


def test_test_button_mints_a_token_and_lists_repositories(client, github, sqlite_engine):
    _save(sqlite_engine)
    response = client.post("/ui/connections/github/test", follow_redirects=False)
    assert "notice=github-test-ok" in response.headers["location"]
    assert github.calls[-1]["path"] == "/installation/repositories"
    assert github.calls[-1]["auth"] == "Bearer ghs_installation"


def test_test_button_reports_a_failed_mint(client, github, sqlite_engine):
    _save(sqlite_engine)
    github.replies[("POST", "/app/installations/77/access_tokens")] = (401, {"message": "no"})
    response = client.post("/ui/connections/github/test", follow_redirects=False)
    assert "notice=github-test-failed&detail=http_401" in response.headers["location"]
    assert _stored(sqlite_engine) is not None


def test_test_button_reports_a_failed_listing(client, github, sqlite_engine):
    _save(sqlite_engine)
    github.replies[("GET", "/installation/repositories")] = (403, {})
    response = client.post("/ui/connections/github/test", follow_redirects=False)
    assert "notice=github-test-failed&detail=http_403" in response.headers["location"]


def test_test_button_when_not_connected(client, github):
    response = client.post("/ui/connections/github/test", follow_redirects=False)
    assert "notice=github-not-connected" in response.headers["location"]
    assert github.calls == []


def test_disconnect_deletes_the_row_and_points_to_github_for_uninstalling(
    client, github, sqlite_engine
):
    _save(sqlite_engine)
    response = client.post("/ui/connections/github/disconnect", follow_redirects=False)
    assert "notice=github-disconnected" in response.headers["location"]
    assert _stored(sqlite_engine) is None
    assert github.calls == []
    page = client.get(response.headers["location"]).text
    assert "github.com/settings/installations" in page


def test_disconnect_when_not_connected_is_harmless(client, github):
    response = client.post("/ui/connections/github/disconnect", follow_redirects=False)
    assert "notice=github-disconnected" in response.headers["location"]


# -- the pull request bot follows the installation ---------------------------


def _pr_card(page: str) -> str:
    return page.split('id="pr-bot"')[1].split("</section>")[0]


def test_bot_card_lists_the_installations_repositories(client, github, sqlite_engine):
    _save(sqlite_engine)
    card = _pr_card(client.get("/ui/connections").text)
    assert "VrajVed/helix-sandbox" in card and "acme/x" in card
    assert not any(c["path"] == "/user/repos" for c in github.calls)


def test_bot_save_accepts_a_listed_repo_and_checks_the_branch_with_the_installation_token(
    client, github, sqlite_engine
):
    _save(sqlite_engine)
    response = client.post(
        "/ui/connections/pr-bot/settings",
        data={"repo": "VrajVed/helix-sandbox", "base_branch": "main"},
        follow_redirects=False,
    )
    assert "notice=pr-bot-saved" in response.headers["location"]
    branch = next(c for c in github.calls if "/branches/" in c["path"])
    assert branch["auth"] == "Bearer ghs_installation"
    with Session(sqlite_engine) as session:
        assert session.get(models.PRBotSettings, 1).repo == "VrajVed/helix-sandbox"


def test_bot_save_refuses_a_repo_the_installation_cannot_reach(client, github, sqlite_engine):
    _save(sqlite_engine)
    response = client.post(
        "/ui/connections/pr-bot/settings",
        data={"repo": "stranger/other"},
        follow_redirects=False,
    )
    assert "notice=pr-bot-repo-refused" in response.headers["location"]
    with Session(sqlite_engine) as session:
        assert session.get(models.PRBotSettings, 1) is None


def test_bot_save_when_the_installation_list_is_down_changes_nothing(client, github, sqlite_engine):
    _save(sqlite_engine)
    github.replies[("GET", "/installation/repositories")] = (500, {})
    response = client.post(
        "/ui/connections/pr-bot/settings",
        data={"repo": "VrajVed/helix-sandbox"},
        follow_redirects=False,
    )
    assert "notice=pr-bot-repo-unavailable" in response.headers["location"]
