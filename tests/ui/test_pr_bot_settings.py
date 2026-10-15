"""The "Pull request bot" card on the Connections page (slice 10).

GitHub is faked with httpx.MockTransport behind integrations.github.oauth._http;
no request leaves the test. The page runs signed in, as it does in use.
"""

import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from helix import auth
from integrations.github import oauth
from state import connections, models
from state.intake import Signal, intake_signal

TOKEN = "gho_secret_pr_bot_token"


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("HELIX_ENV_FILE", "")
    monkeypatch.setenv("HELIX_PUBLIC_URL", "https://helix.example")
    monkeypatch.setenv("HELIX_SESSION_SECRET", "test-session-secret")
    monkeypatch.setenv("HELIX_CONNECTIONS_KEY", "test connections key")
    monkeypatch.setenv("HELIX_GITHUB_OAUTH_CLIENT_ID", "gh-cid")
    monkeypatch.setenv("HELIX_GITHUB_OAUTH_CLIENT_SECRET", "gh-csecret")
    monkeypatch.setenv("HELIX_ALLOWED_USERS", "VrajVed")
    monkeypatch.setenv("HELIX_GITHUB_REPO", "acme/server-default")
    for name in (
        "HELIX_GITHUB_TOKEN",
        "HELIX_GITHUB_WRITES",
        "HELIX_EVIDENCE_SOURCE",
        "HELIX_CONNECTIONS_ENABLED",
    ):
        monkeypatch.delenv(name, raising=False)


def _sign_in(client):
    client.cookies.clear()
    client.cookies.set(
        auth.SESSION_COOKIE,
        auth._sign({"login": "VrajVed", "exp": time.time() + 3600}),
        domain="helix.example",
    )


@pytest.fixture(autouse=True)
def https(client):
    client.base_url = "https://helix.example"
    _sign_in(client)


def _repo(name, push=True):
    return {"full_name": name, "permissions": {"push": push, "pull": True}}


@pytest.fixture
def github(monkeypatch):
    """Fake GitHub. /user/repos is two pages (page 1 is full of one repo to
    prove pagination is followed); `branches` is the set that exists."""
    calls: list[dict] = []
    state = {
        "repos": [
            [_repo("acme/server-default"), _repo("acme/read-only", push=False)]
            + [_repo(f"acme/filler-{n}") for n in range(98)],
            [_repo("VrajVed/mine")],
        ],
        "branches": {"main", "develop", "release/1.0"},
        "repos_status": 200,
        "branch_status": None,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(
            {
                "path": request.url.path,
                "query": dict(request.url.params),
                "auth": request.headers.get("authorization", ""),
            }
        )
        path = request.url.path
        if path == "/user/repos":
            if state["repos_status"] != 200:
                return httpx.Response(state["repos_status"], json={})
            page = int(request.url.params.get("page", 1))
            pages = state["repos"]
            return httpx.Response(200, json=pages[page - 1] if page <= len(pages) else [])
        if "/branches/" in path:
            if state["branch_status"]:
                return httpx.Response(state["branch_status"], json={})
            name = path.split("/branches/", 1)[1]
            ok = name in state["branches"]
            return httpx.Response(200 if ok else 404, json={"name": name} if ok else {})
        return httpx.Response(404, json={})

    monkeypatch.setattr(
        oauth, "_http", lambda: httpx.Client(transport=httpx.MockTransport(handler), timeout=5)
    )
    return type("GitHub", (), {"calls": calls, "state": state})


@pytest.fixture
def connected(sqlite_engine):
    with Session(sqlite_engine) as session:
        connections.save_connection(
            session,
            "github",
            account="VrajVed",
            scopes=["repo"],
            access_token=TOKEN,
            refresh_token=None,
            expires_at=None,
            settings={},
        )
        session.commit()


def _card(page: str) -> str:
    return page.split('id="pr-bot"')[1].split("</section>")[0]


def _row(sqlite_engine):
    with Session(sqlite_engine) as session:
        return session.get(models.PRBotSettings, 1)


def _save(client, **fields):
    return client.post("/ui/connections/pr-bot/settings", data=fields, follow_redirects=False)


def _notice(response) -> str:
    return response.headers["location"].split("notice=")[1].split("&")[0]


def _add_live_prs(sqlite_engine, count, *, age_hours=1, first=0):
    created = datetime.now(UTC) - timedelta(hours=age_hours)
    with Session(sqlite_engine) as session:
        incident_id = intake_signal(
            session,
            Signal(source="kubernetes", service="payment-api", signature={"reason": "CrashLoop"}),
        ).incident_id
        pk = session.scalar(
            select(models.Incident.id).where(models.Incident.incident_id == incident_id)
        )
        for attempt in range(first, first + count):
            session.add(
                models.Execution(
                    incident_id=pk,
                    attempt=attempt,
                    action="open_fix_pr",
                    params={},
                    fingerprint="f",
                    policy_decision="allow",
                    origin="live",
                    result=models.ExecutionResult.SUCCESS,
                    detail="",
                    started_at=created,
                    created_at=created,
                )
            )
        session.commit()


# -- the card ---------------------------------------------------------------


def test_card_renders_with_server_defaults_marked(client, github, connected):
    card = _card(client.get("/ui/connections").text)
    assert "Pull request bot" in card
    assert "acme/server-default" in card and "main" in card
    assert "server default" in card.lower()
    assert 'action="/ui/connections/pr-bot/settings"' in card
    assert TOKEN not in card


def test_card_lists_only_repos_the_token_can_push_to_following_pages(client, github, connected):
    card = _card(client.get("/ui/connections").text)
    select = card.split('name="repo"')[1].split("</select>")[0]
    assert "VrajVed/mine" in select and "acme/server-default" in select
    assert "acme/read-only" not in select
    first = github.calls[0]
    assert first["path"] == "/user/repos" and first["query"]["per_page"] == "100"
    assert first["query"]["affiliation"] == "owner,collaborator,organization_member"
    assert first["auth"] == f"Bearer {TOKEN}"
    assert [c["query"].get("page") for c in github.calls if c["path"] == "/user/repos"][:2] == [
        "1",
        "2",
    ]


def test_card_shows_stored_values(client, github, connected, sqlite_engine):
    with Session(sqlite_engine) as session:
        session.add(
            models.PRBotSettings(
                id=1,
                real_prs=True,
                repo="VrajVed/mine",
                base_branch="develop",
                draft=True,
                daily_limit=4,
            )
        )
        session.commit()
    card = _card(client.get("/ui/connections").text)
    assert 'value="VrajVed/mine" selected' in card
    assert 'value="develop"' in card
    assert 'name="daily_limit"' in card and 'value="4"' in card


def test_counter_without_a_limit(client, github, connected, sqlite_engine):
    _add_live_prs(sqlite_engine, 2)
    card = _card(client.get("/ui/connections").text)
    assert "2 real PRs in the last 24 hours" in card


def test_counter_with_a_limit_shows_n_of_l_and_ignores_old_prs(
    client, github, connected, sqlite_engine
):
    _add_live_prs(sqlite_engine, 2)
    _add_live_prs(sqlite_engine, 5, age_hours=30, first=10)
    with Session(sqlite_engine) as session:
        session.add(models.PRBotSettings(id=1, daily_limit=3))
        session.commit()
    card = _card(client.get("/ui/connections").text)
    assert "2 of 3" in card


def test_repo_list_failure_never_breaks_the_page(client, github, connected):
    github.state["repos_status"] = 500
    response = client.get("/ui/connections")
    assert response.status_code == 200
    card = _card(response.text)
    assert "Could not load" in card and "acme/server-default" in card


def test_without_any_github_token_the_card_still_renders(client, github):
    response = client.get("/ui/connections")
    assert response.status_code == 200
    assert "Pull request bot" in response.text
    assert not [c for c in github.calls if c["path"] == "/user/repos"]


def test_env_forced_recorded_is_said_on_the_card(client, github, connected, monkeypatch):
    monkeypatch.setenv("HELIX_GITHUB_WRITES", "recorded")
    assert "HELIX_GITHUB_WRITES=recorded" in _card(client.get("/ui/connections").text)


# -- saving -----------------------------------------------------------------


def test_save_stores_every_field(client, github, connected, sqlite_engine):
    response = _save(
        client,
        real_prs="on",
        repo="VrajVed/mine",
        base_branch="develop",
        draft="on",
        daily_limit="7",
    )
    assert response.status_code == 303 and _notice(response) == "pr-bot-saved"
    row = _row(sqlite_engine)
    assert (row.real_prs, row.repo, row.base_branch, row.draft, row.daily_limit) == (
        True,
        "VrajVed/mine",
        "develop",
        True,
        7,
    )
    # The branch was checked in the repo being saved.
    assert any(c["path"] == "/repos/VrajVed/mine/branches/develop" for c in github.calls)


def test_blank_fields_mean_the_server_default(client, github, connected, sqlite_engine):
    _save(client, real_prs="on", repo="VrajVed/mine", base_branch="develop", daily_limit="7")
    response = _save(client, real_prs="", repo="", base_branch="", draft="", daily_limit="")
    assert _notice(response) == "pr-bot-saved"
    row = _row(sqlite_engine)
    assert (row.real_prs, row.repo, row.base_branch, row.draft, row.daily_limit) == (
        None,
        None,
        None,
        None,
        None,
    )


def test_off_is_stored_as_false_not_default(client, github, connected, sqlite_engine):
    _save(client, real_prs="off", draft="off")
    row = _row(sqlite_engine)
    assert row.real_prs is False and row.draft is False


@pytest.mark.parametrize("limit", ["1", "1000"])
def test_limit_boundaries_are_accepted(client, github, connected, sqlite_engine, limit):
    assert _notice(_save(client, daily_limit=limit)) == "pr-bot-saved"
    assert _row(sqlite_engine).daily_limit == int(limit)


@pytest.mark.parametrize("limit", ["0", "1001", "-1", "abc", "5.5", "1e2", "99999999999999"])
def test_bad_limit_is_refused_and_nothing_changes(client, github, connected, sqlite_engine, limit):
    _save(client, daily_limit="3")
    response = _save(client, daily_limit=limit, draft="on")
    assert _notice(response) == "pr-bot-limit-refused"
    row = _row(sqlite_engine)
    assert row.daily_limit == 3 and row.draft is None


@pytest.mark.parametrize("field", ["real_prs", "draft"])
def test_bad_toggle_value_is_refused(client, github, connected, sqlite_engine, field):
    response = _save(client, **{field: "maybe"})
    assert _notice(response) == "pr-bot-invalid"
    assert _row(sqlite_engine) is None


def test_unlisted_repo_is_refused(client, github, connected, sqlite_engine):
    for repo in ("evil/other", "acme/read-only", "acme/server-default; x"):
        response = _save(client, repo=repo)
        assert _notice(response) == "pr-bot-repo-refused", repo
    assert _row(sqlite_engine) is None


def test_repo_list_failure_refuses_a_change_without_saving(
    client, github, connected, sqlite_engine
):
    github.state["repos_status"] = 500
    response = _save(client, repo="VrajVed/mine")
    assert _notice(response) == "pr-bot-repo-unavailable"
    assert _row(sqlite_engine) is None


def test_unchanged_stored_repo_is_kept_when_the_list_is_down(
    client, github, connected, sqlite_engine
):
    _save(client, repo="VrajVed/mine")
    github.state["repos_status"] = 500
    response = _save(client, repo="VrajVed/mine", draft="on")
    assert _notice(response) == "pr-bot-saved"
    assert _row(sqlite_engine).draft is True


def test_missing_branch_is_refused(client, github, connected, sqlite_engine):
    response = _save(client, base_branch="nope")
    assert _notice(response) == "pr-bot-branch-refused"
    assert _row(sqlite_engine) is None


def test_branch_is_checked_in_the_default_repo_when_none_is_chosen(client, github, connected):
    _save(client, base_branch="release/1.0")
    assert any(c["path"] == "/repos/acme/server-default/branches/release/1.0" for c in github.calls)


@pytest.mark.parametrize("branch", ["a b", "../x", "x;y", "-bad", "a" * 300])
def test_malformed_branch_is_refused_without_a_request(client, github, connected, branch):
    response = _save(client, base_branch=branch)
    assert _notice(response) == "pr-bot-branch-refused"
    assert not [c for c in github.calls if "/branches/" in c["path"]]


def test_branch_check_failure_is_unavailable_not_saved(client, github, connected, sqlite_engine):
    github.state["branch_status"] = 500
    response = _save(client, base_branch="develop")
    assert _notice(response) == "pr-bot-branch-unavailable"
    assert _row(sqlite_engine) is None


def test_save_without_a_github_token_cannot_validate_a_repo_or_branch(
    client, github, sqlite_engine
):
    assert _notice(_save(client, repo="VrajVed/mine")) == "pr-bot-repo-unavailable"
    assert _notice(_save(client, base_branch="develop")) == "pr-bot-branch-unavailable"
    assert _row(sqlite_engine) is None
    assert _notice(_save(client, real_prs="on", draft="on", daily_limit="2")) == "pr-bot-saved"


def test_notice_text_shows_on_the_page(client, github, connected):
    page = client.get("/ui/connections?notice=pr-bot-saved").text
    assert "Saved the pull request bot settings." in page


# -- sign-in ----------------------------------------------------------------


def test_everything_requires_sign_in(client, github, connected):
    client.cookies.clear()
    response = client.post(
        "/ui/connections/pr-bot/settings", data={"draft": "on"}, follow_redirects=False
    )
    assert response.status_code == 303 and response.headers["location"].endswith("/login")
