"""Slack matching for the team: the invite email, GitHub verified emails and
the self-link from Slack. Slack and GitHub are faked; nothing leaves the test.
Every step is best effort - a Slack or GitHub failure never blocks an invite
or a sign-in."""

import time
from urllib.parse import parse_qs, quote, urlencode, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from helix import auth, ui
from helix.api import app, get_db_session
from integrations.slack import oauth
from state import connections, models
from state.db import session_scope

GOOD_SCOPES = ["chat:write", "channels:read", "users:read", "users:read.email"]
OLD_SCOPES = ["chat:write", "chat:write.public", "channels:read"]


@pytest.fixture
def db(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'team.db'}"
    engine = create_engine(url)
    models.Base.metadata.create_all(engine)
    monkeypatch.setenv("HELIX_DATABASE_URL", url)
    monkeypatch.delenv("HELIX_SERVICE_REGISTRY", raising=False)
    monkeypatch.delenv("HELIX_ALLOWED_USERS", raising=False)
    monkeypatch.setenv("HELIX_CONNECTIONS_KEY", "test connections key")
    with Session(engine) as s:
        s.add_all(
            [
                models.Person(login="VrajVed", name="Vraj Ved", role="admin", email="v@x.com"),
                models.Person(login="nikhil", name="Nikhil P", role="member"),
            ]
        )
        s.commit()
    yield engine
    engine.dispose()


@pytest.fixture
def client(db, monkeypatch):
    def override():
        with session_scope(db) as session:
            yield session

    app.dependency_overrides[get_db_session] = override
    app.dependency_overrides[ui.get_db_session] = override
    monkeypatch.setenv("HELIX_SESSION_SECRET", "test-session-secret")
    monkeypatch.setenv("HELIX_PUBLIC_URL", "https://helix.example")
    monkeypatch.setenv("HELIX_GITHUB_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("HELIX_GITHUB_OAUTH_CLIENT_SECRET", "csecret")
    with TestClient(app, base_url="https://helix.example") as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def slack(monkeypatch):
    """Fake Slack Web API: `users` maps email -> member id; `fail` maps a
    method to a Slack error code (or "transport")."""
    calls: list[tuple[str, dict]] = []
    users: dict[str, str] = {}
    fail: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        calls.append((method, form))
        if fail.get(method) == "transport":
            raise httpx.ConnectError("down")
        if method in fail:
            return httpx.Response(200, json={"ok": False, "error": fail[method]})
        if method == "users.lookupByEmail":
            if form["email"] in users:
                return httpx.Response(200, json={"ok": True, "user": {"id": users[form["email"]]}})
            return httpx.Response(200, json={"ok": False, "error": "users_not_found"})
        return httpx.Response(200, json={"ok": True, "ts": "1.1"})

    monkeypatch.setattr(
        oauth, "_http", lambda: httpx.Client(transport=httpx.MockTransport(handler), timeout=5)
    )
    return type("Slack", (), {"calls": calls, "users": users, "fail": fail})


def _connect(db, scopes=GOOD_SCOPES):
    with Session(db) as s:
        connections.save_connection(
            s,
            "slack",
            account="Acme",
            scopes=scopes,
            access_token="xoxb-secret",
            refresh_token=None,
            expires_at=None,
            settings={"team_id": "T1"},
        )
        s.commit()


def _sign_in(client, monkeypatch, login, name=None, emails=(), next_=None):
    monkeypatch.setattr(
        auth, "github_login", lambda code, uri: auth.GitHubUser(login, name, tuple(emails))
    )
    url = "/auth/github" + (f"?next={quote(next_, safe='')}" if next_ is not None else "")
    start = client.get(url, follow_redirects=False)
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    return client.get(f"/auth/github/callback?code=c&state={state}", follow_redirects=False)


def _person(db, login):
    with Session(db) as s:
        return s.get(models.Person, login)


def _invite_row(db, login):
    with Session(db) as s:
        return s.scalar(select(models.Invitation).where(models.Invitation.login == login))


def _methods(slack):
    return [m for m, _ in slack.calls]


# ---- step 1: the email on the invite ----------------------------------------


def _invite(client, email="new@x.com", role="member"):
    return client.post(
        "/ui/team/invite",
        data={"login": "newbie", "role": role, "email": email},
        follow_redirects=True,
    )


def test_invite_with_a_found_email_stores_the_id_and_sends_the_dm(client, db, slack, monkeypatch):
    _connect(db)
    slack.users["new@x.com"] = "U77"
    _sign_in(client, monkeypatch, "VrajVed")
    page = _invite(client, role="admin")
    row = _invite_row(db, "newbie")
    assert row.slack_id == "U77"
    sent = [f for m, f in slack.calls if m == "chat.postMessage"]
    assert len(sent) == 1 and sent[0]["channel"] == "U77"
    assert sent[0]["text"] == (
        f"Vraj Ved invited you to the team on Helix as an admin. "
        f"Accept: https://helix.example/invite/{row.token}"
    )
    assert "Found them in Slack and sent them the invite" in page.text


def test_invite_with_an_email_not_in_slack_sends_nothing(client, db, slack, monkeypatch):
    _connect(db)
    _sign_in(client, monkeypatch, "VrajVed")
    page = _invite(client)
    assert _invite_row(db, "newbie").slack_id is None
    assert "chat.postMessage" not in _methods(slack)
    assert "could not find that email in Slack" in page.text


def test_invite_without_slack_connected_still_works(client, db, slack, monkeypatch):
    _sign_in(client, monkeypatch, "VrajVed")
    page = _invite(client)
    assert _invite_row(db, "newbie") is not None and slack.calls == []
    assert "Invited newbie" in page.text and "Found them" not in page.text


def test_invite_without_an_email_never_calls_slack(client, db, slack, monkeypatch):
    _connect(db)
    _sign_in(client, monkeypatch, "VrajVed")
    slack.calls.clear()  # signing in looks up the admin's own stored email
    _invite(client, email="")
    assert _invite_row(db, "newbie") is not None and slack.calls == []


def test_invite_with_old_stored_scopes_skips_slack_and_says_reconnect(
    client, db, slack, monkeypatch
):
    _connect(db, OLD_SCOPES)
    _sign_in(client, monkeypatch, "VrajVed")
    page = _invite(client)
    assert slack.calls == [] and _invite_row(db, "newbie") is not None
    assert "Reconnect Slack on the Connections page" in page.text


def test_invite_when_slack_reports_missing_scope_says_reconnect(client, db, slack, monkeypatch):
    _connect(db)
    slack.fail["users.lookupByEmail"] = "missing_scope"
    _sign_in(client, monkeypatch, "VrajVed")
    page = _invite(client)
    assert _invite_row(db, "newbie") is not None
    assert "Reconnect Slack on the Connections page" in page.text


def test_invite_survives_a_failed_dm_and_keeps_the_id(client, db, slack, monkeypatch):
    _connect(db)
    slack.users["new@x.com"] = "U77"
    slack.fail["chat.postMessage"] = "channel_not_found"
    _sign_in(client, monkeypatch, "VrajVed")
    page = _invite(client)
    assert _invite_row(db, "newbie").slack_id == "U77"
    assert "could not message them" in page.text


def test_invite_survives_slack_being_down(client, db, slack, monkeypatch):
    _connect(db)
    slack.fail["users.lookupByEmail"] = "transport"
    _sign_in(client, monkeypatch, "VrajVed")
    page = _invite(client)
    assert _invite_row(db, "newbie") is not None
    assert "Slack did not answer" in page.text


def test_accepting_the_invite_gives_the_person_the_slack_id(client, db, slack, monkeypatch):
    _connect(db)
    slack.users["new@x.com"] = "U77"
    _sign_in(client, monkeypatch, "VrajVed")
    _invite(client)
    client.cookies.clear()
    assert _sign_in(client, monkeypatch, "newbie", "New Bie").status_code == 303
    assert _person(db, "newbie").slack_id == "U77"


def test_reconnect_hint_follows_the_stored_scopes_for_admins_only(client, db, slack, monkeypatch):
    hint = "Reconnect Slack on the Connections page so Helix can find people by email."
    _sign_in(client, monkeypatch, "VrajVed")
    assert hint not in client.get("/ui/team").text  # not connected: nothing to reconnect
    _connect(db, OLD_SCOPES)
    assert hint in client.get("/ui/team").text
    assert slack.calls == []  # decided from the stored scopes, no network
    client.cookies.clear()
    _sign_in(client, monkeypatch, "nikhil")
    assert hint not in client.get("/ui/team").text
    _connect(db, GOOD_SCOPES)
    client.cookies.clear()
    _sign_in(client, monkeypatch, "VrajVed")
    assert hint not in client.get("/ui/team").text


# ---- step 1: Find in Slack on a person row ----------------------------------


def _find(client, email="nik@x.com", login="nikhil"):
    return client.post(
        f"/ui/team/people/{login}/find-slack", data={"email": email}, follow_redirects=True
    )


def test_find_in_slack_links_the_person_and_saves_the_email(client, db, slack, monkeypatch):
    _connect(db)
    slack.users["nik@x.com"] = "U55"
    _sign_in(client, monkeypatch, "VrajVed")
    page = _find(client)
    person = _person(db, "nikhil")
    assert (person.slack_id, person.email) == ("U55", "nik@x.com")
    assert "Linked nikhil to their Slack account" in page.text
    assert "Find in Slack" in client.get("/ui/team").text


def test_find_in_slack_not_found_changes_nothing(client, db, slack, monkeypatch):
    _connect(db)
    _sign_in(client, monkeypatch, "VrajVed")
    page = _find(client)
    assert _person(db, "nikhil").slack_id is None and _person(db, "nikhil").email is None
    assert "could not find that email in Slack" in page.text


def test_find_in_slack_when_slack_is_not_connected(client, db, slack, monkeypatch):
    _sign_in(client, monkeypatch, "VrajVed")
    page = _find(client)
    assert "Slack is not connected" in page.text and slack.calls == []


def test_find_in_slack_asks_to_reconnect_on_old_scopes(client, db, slack, monkeypatch):
    _connect(db, OLD_SCOPES)
    _sign_in(client, monkeypatch, "VrajVed")
    assert "Reconnect Slack on the Connections page" in _find(client).text
    assert slack.calls == []


def test_find_in_slack_asks_to_reconnect_on_missing_scope_from_slack(
    client, db, slack, monkeypatch
):
    _connect(db)
    slack.fail["users.lookupByEmail"] = "missing_scope"
    _sign_in(client, monkeypatch, "VrajVed")
    assert "Reconnect Slack on the Connections page" in _find(client).text


def test_find_in_slack_needs_an_email_and_an_unclaimed_id(client, db, slack, monkeypatch):
    _connect(db)
    slack.users["v@x.com"] = "U1"
    _sign_in(client, monkeypatch, "VrajVed")
    assert "Enter an email" in _find(client, email=" ").text
    assert _find(client, "v@x.com", "VrajVed").status_code == 200
    page = _find(client, "v@x.com", "nikhil")
    assert "already linked to Vraj Ved" in page.text and _person(db, "nikhil").slack_id is None


def test_find_in_slack_is_admin_only(client, db, slack, monkeypatch):
    _connect(db)
    _sign_in(client, monkeypatch, "nikhil")
    r = client.post("/ui/team/people/nikhil/find-slack", data={"email": "a@x.com"})
    assert r.status_code == 403 and slack.calls == []


# ---- step 2: GitHub verified emails at sign-in ------------------------------


def test_sign_in_links_slack_on_the_second_verified_email(client, db, slack, monkeypatch):
    _connect(db)
    with Session(db) as s:
        s.get(models.Person, "nikhil").email = "stored@x.com"
        s.commit()
    slack.users["second@x.com"] = "U31"
    done = _sign_in(client, monkeypatch, "nikhil", emails=("first@x.com", "second@x.com"))
    assert done.status_code == 303
    assert _person(db, "nikhil").slack_id == "U31"
    looked = [f["email"] for m, f in slack.calls if m == "users.lookupByEmail"]
    assert looked == ["stored@x.com", "first@x.com", "second@x.com"]  # stored email first


def test_sign_in_stops_at_the_first_match_and_fills_an_empty_email(client, db, slack, monkeypatch):
    _connect(db)
    slack.users["a@x.com"] = "U1"
    slack.users["b@x.com"] = "U2"
    _sign_in(client, monkeypatch, "nikhil", emails=("a@x.com", "b@x.com"))
    person = _person(db, "nikhil")
    assert (person.slack_id, person.email) == ("U1", "a@x.com")
    assert len(slack.calls) == 1


def test_sign_in_without_emails_or_slack_does_nothing(client, db, slack, monkeypatch):
    assert _sign_in(client, monkeypatch, "nikhil", emails=("a@x.com",)).status_code == 303
    assert slack.calls == [] and _person(db, "nikhil").slack_id is None
    _connect(db)
    assert _sign_in(client, monkeypatch, "nikhil").status_code == 303
    assert slack.calls == []


def test_sign_in_skips_a_person_who_is_already_linked(client, db, slack, monkeypatch):
    _connect(db)
    with Session(db) as s:
        s.get(models.Person, "nikhil").slack_id = "U9"
        s.commit()
    slack.users["a@x.com"] = "U1"
    assert _sign_in(client, monkeypatch, "nikhil", emails=("a@x.com",)).status_code == 303
    assert slack.calls == [] and _person(db, "nikhil").slack_id == "U9"


def test_sign_in_survives_slack_failures(client, db, slack, monkeypatch):
    _connect(db)
    for failure in ("transport", "missing_scope", "ratelimited"):
        slack.fail["users.lookupByEmail"] = failure
        assert _sign_in(client, monkeypatch, "nikhil", emails=("a@x.com",)).status_code == 303
        client.cookies.clear()
    assert _person(db, "nikhil").slack_id is None


def test_sign_in_does_not_steal_an_id_linked_to_someone_else(client, db, slack, monkeypatch):
    _connect(db)
    with Session(db) as s:
        s.get(models.Person, "VrajVed").slack_id = "U1"
        s.commit()
    slack.users["a@x.com"] = "U1"
    assert _sign_in(client, monkeypatch, "nikhil", emails=("a@x.com",)).status_code == 303
    assert _person(db, "nikhil").slack_id is None


def _github_fake(monkeypatch, emails_response):
    def post(url, **kw):
        return httpx.Response(200, json={"access_token": "gho_secret"})

    def get(url, **kw):
        if url.endswith("/user/emails"):
            return emails_response(url)
        return httpx.Response(
            200, json={"login": "nikhil", "name": "Nikhil P"}, request=httpx.Request("GET", url)
        )

    monkeypatch.setattr(auth.httpx, "post", post)
    monkeypatch.setattr(auth.httpx, "get", get)


def test_github_login_returns_only_verified_emails(monkeypatch):
    _github_fake(
        monkeypatch,
        lambda url: httpx.Response(
            200,
            json=[
                {"email": "ok@x.com", "verified": True, "primary": True},
                {"email": "nope@x.com", "verified": False},
                {"email": "also@x.com", "verified": True},
            ],
            request=httpx.Request("GET", url),
        ),
    )
    user = auth.github_login("code", "https://helix.example/auth/github/callback")
    assert user.login == "nikhil" and user.verified_emails == ("ok@x.com", "also@x.com")


@pytest.mark.parametrize("status", [403, 500])
def test_github_login_survives_the_emails_call_failing(monkeypatch, status):
    _github_fake(
        monkeypatch,
        lambda url: httpx.Response(status, json={}, request=httpx.Request("GET", url)),
    )
    user = auth.github_login("code", "https://helix.example/auth/github/callback")
    assert user.login == "nikhil" and user.verified_emails == ()


def test_github_login_survives_the_emails_call_raising(monkeypatch):
    def boom(url):
        raise httpx.ConnectError("down")

    _github_fake(monkeypatch, boom)
    assert auth.github_login("c", "u").verified_emails == ()


def test_github_user_defaults_to_no_verified_emails():
    assert auth.GitHubUser("x").verified_emails == ()


def test_github_sign_in_asks_for_the_email_scope(client):
    start = client.get("/auth/github", follow_redirects=False)
    assert parse_qs(urlparse(start.headers["location"]).query)["scope"] == ["read:user user:email"]


# ---- step 3: self-link from Slack -------------------------------------------


def _token(slack_id="U500", name="carol", team_id="T1", exp=None):
    return auth._sign(
        {
            "purpose": "slack-link",
            "slack_id": slack_id,
            "slack_name": name,
            "team": team_id,
            "exp": exp if exp is not None else time.time() + 900,
        }
    )


def _link_url(token):
    return "/ui/team/link-slack?" + urlencode({"t": token})


def test_link_page_shows_a_confirm_card_and_changes_nothing(client, db, monkeypatch):
    _sign_in(client, monkeypatch, "nikhil")
    page = client.get(_link_url(_token()))
    assert page.status_code == 200
    assert "Link Slack user carol to your Helix account nikhil?" in page.text
    assert 'method="post"' in page.text and 'action="/ui/team/link-slack"' in page.text
    assert _person(db, "nikhil").slack_id is None


def test_confirming_links_the_slack_id_to_the_signed_in_person(client, db, monkeypatch):
    _sign_in(client, monkeypatch, "nikhil")
    done = client.post("/ui/team/link-slack", data={"t": _token()}, follow_redirects=False)
    assert done.status_code == 200 and "Linked Slack user carol" in done.text
    assert _person(db, "nikhil").slack_id == "U500"
    assert "Slack linked" in client.get("/ui/team").text


def test_an_expired_or_tampered_token_is_refused(client, db, monkeypatch):
    _sign_in(client, monkeypatch, "nikhil")
    for bad in (_token(exp=time.time() - 1), _token() + "x", "garbage", ""):
        assert "not valid or has expired" in client.get(_link_url(bad)).text
        refused = client.post("/ui/team/link-slack", data={"t": bad})
        assert "not valid or has expired" in refused.text
    assert _person(db, "nikhil").slack_id is None


def test_a_signed_value_that_is_not_a_link_token_is_refused(client, db, monkeypatch):
    """Session cookies share the signing key; only a slack-link token links."""
    _sign_in(client, monkeypatch, "nikhil")
    imposter = auth._sign({"slack_id": "U500", "login": "nikhil", "exp": time.time() + 900})
    assert "not valid or has expired" in client.get(_link_url(imposter)).text
    refused = client.post("/ui/team/link-slack", data={"t": imposter})
    assert "not valid or has expired" in refused.text
    assert _person(db, "nikhil").slack_id is None


def test_a_slack_id_linked_to_someone_else_is_refused(client, db, monkeypatch):
    with Session(db) as s:
        s.get(models.Person, "VrajVed").slack_id = "U500"
        s.commit()
    _sign_in(client, monkeypatch, "nikhil")
    for page in (
        client.get(_link_url(_token())),
        client.post("/ui/team/link-slack", data={"t": _token()}),
    ):
        assert "already linked to someone else" in page.text and "ask an admin" in page.text
    assert _person(db, "nikhil").slack_id is None


def test_someone_who_is_not_a_person_on_the_team_is_refused(client, db, monkeypatch):
    monkeypatch.setenv("HELIX_ALLOWED_USERS", "operator")
    _sign_in(client, monkeypatch, "operator")
    for page in (
        client.get(_link_url(_token())),
        client.post("/ui/team/link-slack", data={"t": _token()}),
    ):
        assert "not on the team" in page.text
    with Session(db) as s:
        assert s.scalar(select(models.Person).where(models.Person.slack_id == "U500")) is None


def test_the_link_needs_a_signed_in_visitor_and_comes_back_after_sign_in(client, db, monkeypatch):
    url = _link_url(_token())
    gate = client.get(url, follow_redirects=False)
    assert gate.status_code == 303
    login = urlparse(gate.headers["location"])
    assert login.path == "/login" and parse_qs(login.query)["next"] == [url]
    signed_out_post = client.post(
        "/ui/team/link-slack", data={"t": _token()}, follow_redirects=False
    )
    assert signed_out_post.status_code == 303
    # /login carries it on to GitHub, and the callback lands back on the link.
    page = client.get(gate.headers["location"])
    assert "next=" in page.text
    done = _sign_in(client, monkeypatch, "nikhil", next_=url)
    assert done.status_code == 303 and done.headers["location"] == url


@pytest.mark.parametrize(
    "bad",
    [
        "https://evil.example",
        "//evil.example",
        "///evil.example/ui/x",
        "/ui/../x",
        "/ui/team/../../x",
        "/team",
        "/incidents",
        "ui/team",
        "/ui",
        "javascript:alert(1)",
        "/ui/\\evil.example",
        "/ui/x\r\nSet-Cookie: a=b",
        "",
    ],
)
def test_next_open_redirects_fall_back_to_the_dashboard(client, db, monkeypatch, bad):
    done = _sign_in(client, monkeypatch, "nikhil", next_=bad)
    assert done.status_code == 303 and done.headers["location"] == "/ui/"


def test_next_to_an_ordinary_ui_page_is_accepted(client, db, monkeypatch):
    done = _sign_in(client, monkeypatch, "nikhil", next_="/ui/team")
    assert done.headers["location"] == "/ui/team"


def test_a_forged_next_is_not_trusted_from_the_query_on_the_callback(client, db, monkeypatch):
    # next only travels in the signed state cookie, never on the callback URL
    monkeypatch.setattr(auth, "github_login", lambda code, uri: auth.GitHubUser("nikhil"))
    start = client.get("/auth/github", follow_redirects=False)
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    done = client.get(
        f"/auth/github/callback?code=c&state={state}&next=//evil.example", follow_redirects=False
    )
    assert done.headers["location"] == "/ui/"
