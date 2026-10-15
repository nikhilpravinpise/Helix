"""The Team page, invites and the admin gate (future_plans.md "Team and
services"). The team lives in a file SQLite database so the sign-in check
(which reads the people table through load_registry) and the page see the
same rows. GitHub is faked; no request leaves the test."""

from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from helix import auth, ui
from helix.api import app, get_db_session
from state import models, team
from state.db import session_scope


@pytest.fixture
def db(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'team.db'}"
    engine = create_engine(url)
    models.Base.metadata.create_all(engine)
    monkeypatch.setenv("HELIX_DATABASE_URL", url)
    monkeypatch.delenv("HELIX_SERVICE_REGISTRY", raising=False)
    monkeypatch.delenv("HELIX_ALLOWED_USERS", raising=False)
    with Session(engine) as s:
        s.add_all(
            [
                models.Person(login="VrajVed", name="Vraj Ved", role="admin"),
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
    with TestClient(app, base_url="https://helix.example") as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def sign_in_on(monkeypatch):
    monkeypatch.setenv("HELIX_GITHUB_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("HELIX_GITHUB_OAUTH_CLIENT_SECRET", "csecret")


def _sign_in(client, monkeypatch, login, name=None):
    monkeypatch.setattr(auth, "github_login", lambda code, uri: auth.GitHubUser(login, name))
    start = client.get("/auth/github", follow_redirects=False)
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    return client.get(f"/auth/github/callback?code=c&state={state}", follow_redirects=False)


def _invite(db, login="newbie", role="member", **kw):
    with Session(db) as s:
        inv = team.invite(s, login, role, "VrajVed", **kw)
        s.commit()
        return inv.id, inv.token


def _people(db):
    with Session(db) as s:
        return {p.login: p for p in s.scalars(select(models.Person))}


# ---- sign-in and invites ----------------------------------------------------


def test_a_pending_invite_signs_in_and_creates_the_person(client, db, sign_in_on, monkeypatch):
    _invite(db, "Newbie", role="admin", email="n@example.com")
    done = _sign_in(client, monkeypatch, "newbie", "New Bie")
    assert done.status_code == 303 and done.headers["location"].endswith("/ui/")
    person = _people(db)["newbie"]
    assert (person.name, person.role, person.email) == ("New Bie", "admin", "n@example.com")
    assert person.last_signed_in_at is not None
    page = client.get("/ui/team")
    assert page.status_code == 200 and "No pending invites" in page.text


def test_an_expired_or_cancelled_invite_gets_the_denied_page(client, db, sign_in_on, monkeypatch):
    inv_id, _ = _invite(db, "late")
    with Session(db) as s:
        s.get(models.Invitation, inv_id).expires_at = datetime.now(UTC) - timedelta(seconds=1)
        s.commit()
    refused = _sign_in(client, monkeypatch, "late")
    assert refused.status_code == 403 and "not allowed" in refused.text
    inv2, _ = _invite(db, "gone")
    with Session(db) as s:
        team.cancel(s, inv2)
        s.commit()
    assert _sign_in(client, monkeypatch, "gone").status_code == 403
    assert "late" not in _people(db) and "gone" not in _people(db)


def test_sign_in_records_last_signed_in(client, db, sign_in_on, monkeypatch):
    assert _people(db)["nikhil"].last_signed_in_at is None
    assert _sign_in(client, monkeypatch, "nikhil").status_code == 303
    assert _people(db)["nikhil"].last_signed_in_at is not None


def test_a_configured_allow_list_stays_exact_and_ignores_invites(
    client, db, sign_in_on, monkeypatch
):
    monkeypatch.setenv("HELIX_ALLOWED_USERS", "VrajVed")
    _invite(db, "newbie")
    assert _sign_in(client, monkeypatch, "newbie").status_code == 403
    assert "newbie" not in _people(db)
    assert _sign_in(client, monkeypatch, "VrajVed").status_code == 303


def test_a_profile_without_a_name_still_signs_in(client, db, sign_in_on, monkeypatch):
    monkeypatch.setattr(auth, "github_login", lambda code, uri: auth.GitHubUser("nikhil"))
    start = client.get("/auth/github", follow_redirects=False)
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    done = client.get(f"/auth/github/callback?code=c&state={state}", follow_redirects=False)
    assert done.status_code == 303


# ---- the public invite page -------------------------------------------------


def test_invite_page_pending(client, db, sign_in_on):
    _, token = _invite(db, role="admin")
    page = client.get(f"/invite/{token}")
    assert page.status_code == 200
    assert "<b>Vraj Ved</b> invited you to join the team on Helix as an admin" in page.text
    assert 'href="/auth/github"' in page.text and "Sign in with GitHub to accept" in page.text


def test_invite_page_as_member(client, db, sign_in_on):
    _, token = _invite(db)
    assert "as a member" in client.get(f"/invite/{token}").text


def test_invite_page_states(client, db, sign_in_on):
    inv_id, token = _invite(db, "late")
    with Session(db) as s:
        s.get(models.Invitation, inv_id).expires_at = datetime.now(UTC) - timedelta(seconds=1)
        s.commit()
    page = client.get(f"/invite/{token}")
    assert "This invite has expired - ask an admin to resend it" in page.text
    assert "Sign in with GitHub to accept" not in page.text

    inv2, tok2 = _invite(db, "gone")
    with Session(db) as s:
        team.cancel(s, inv2)
        s.commit()
    assert "This invite was cancelled" in client.get(f"/invite/{tok2}").text

    _invite(db, "done")
    with Session(db) as s:
        row = s.scalar(select(models.Invitation).where(models.Invitation.login == "done"))
        tok3 = row.token
        team.accept_on_sign_in(s, "done", "Done")
        s.commit()
    assert "already accepted" in client.get(f"/invite/{tok3}").text


def test_invite_page_unknown_token_is_404(client, db, sign_in_on):
    page = client.get("/invite/not-a-real-token")
    assert page.status_code == 404


def test_only_the_invite_path_became_public(client, db, sign_in_on):
    for path in ["/invite", "/invite/", "/inviteabc", "/ui/team", "/ui/", "/incidents"]:
        r = client.get(path, follow_redirects=False)
        assert r.status_code in (303, 401), path
    assert client.get("/ui/team", follow_redirects=False).headers["location"].endswith("/login")
    assert client.get("/incidents").status_code == 401


# ---- the admin gate ---------------------------------------------------------

TEAM_POSTS = [
    "/ui/team/invite",
    "/ui/team/invites/1/resend",
    "/ui/team/invites/1/cancel",
    "/ui/team/people/nikhil/role",
    "/ui/team/people/nikhil/rename",
    "/ui/team/people/nikhil/remove",
]


def test_a_member_gets_403_on_every_team_post_and_connections_post(
    client, db, sign_in_on, monkeypatch
):
    _sign_in(client, monkeypatch, "nikhil")
    for path in [
        *TEAM_POSTS,
        "/ui/connections/slack/disconnect",
        "/ui/connections/pr-bot/settings",
    ]:
        r = client.post(path, data={"login": "x", "role": "admin"}, follow_redirects=False)
        assert r.status_code == 403, path
        assert "Only admins" in r.text
    assert _people(db)["nikhil"].role == "member"


def test_a_member_can_still_read_the_team_and_connections(client, db, sign_in_on, monkeypatch):
    _sign_in(client, monkeypatch, "nikhil")
    assert client.get("/ui/team").status_code == 200
    assert client.get("/ui/connections").status_code == 200


def test_an_admin_can_invite_and_post_connections(client, db, sign_in_on, monkeypatch):
    _sign_in(client, monkeypatch, "VrajVed")
    done = client.post(
        "/ui/team/invite", data={"login": "fresh", "role": "member"}, follow_redirects=False
    )
    assert done.status_code == 303 and "/ui/team?notice=" in done.headers["location"]
    with Session(db) as s:
        assert [i.login for i in team.pending_invites(s)] == ["fresh"]
    assert (
        client.post("/ui/connections/slack/disconnect", follow_redirects=False).status_code == 303
    )


def test_sign_in_disabled_is_trusted_as_admin(client, db):
    done = client.post(
        "/ui/team/invite", data={"login": "fresh", "role": "admin"}, follow_redirects=False
    )
    assert done.status_code == 303
    with Session(db) as s:
        assert team.pending_invites(s)[0].invited_by


def test_a_user_from_the_allow_list_override_without_a_row_is_trusted(
    client, db, sign_in_on, monkeypatch
):
    monkeypatch.setenv("HELIX_ALLOWED_USERS", "operator")
    _sign_in(client, monkeypatch, "operator")
    r = client.post("/ui/connections/slack/disconnect", follow_redirects=False)
    assert r.status_code == 303


# ---- the page ---------------------------------------------------------------


def test_team_page_lists_members_and_pending_invites(client, db):
    with Session(db) as s:
        s.get(models.Person, "nikhil").slack_id = "U123"
        s.get(models.Person, "VrajVed").last_signed_in_at = datetime.now(UTC) - timedelta(hours=3)
        s.commit()
    _, token = _invite(db, "fresh", email="f@example.com")
    _, _ = _invite(db, "stale")
    with Session(db) as s:
        stale = s.scalar(select(models.Invitation).where(models.Invitation.login == "stale"))
        stale.expires_at = datetime.now(UTC) - timedelta(days=1)
        s.commit()
    page = client.get("/ui/team")
    assert page.status_code == 200
    text = page.text
    assert "Vraj Ved" in text and "VrajVed" in text and "Nikhil P" in text
    assert "Slack linked" in text and "Not linked to Slack" in text
    assert "Never signed in" in text and "Last signed in 3h ago" in text
    assert "fresh" in text and "Invited by Vraj Ved" in text and "Expires in 7 days" in text
    assert "stale" not in text
    assert f"https://helix.example/invite/{token}" in text
    for label in ("Resend", "Cancel", "Make member", "Make admin", "Rename", "Remove"):
        assert label in text


def test_accepted_and_cancelled_invites_are_not_listed(client, db):
    inv, _ = _invite(db, "cancelled-one")
    with Session(db) as s:
        team.cancel(s, inv)
        s.commit()
    _invite(db, "accepted-one")
    with Session(db) as s:
        team.accept_on_sign_in(s, "accepted-one", "A")
        s.commit()
    text = client.get("/ui/team").text
    assert "cancelled-one" not in text
    assert "No pending invites" in text


def test_team_page_is_read_only_for_members(client, db, sign_in_on, monkeypatch):
    _invite(db, "fresh")
    _sign_in(client, monkeypatch, "nikhil")
    text = client.get("/ui/team").text
    assert "Only admins can change the team." in text
    for hidden in ("Resend", "Make admin", "Rename", 'action="/ui/team/invite"'):
        assert hidden not in text


def test_team_page_notices_and_errors_come_back_as_notices(client, db):
    done = client.post(
        "/ui/team/invite", data={"login": "-bad", "role": "member"}, follow_redirects=True
    )
    assert done.status_code == 200
    assert "notice-bad" in done.text and "GitHub login" in done.text
    refused = client.post("/ui/team/people/VrajVed/remove", follow_redirects=True)
    assert "Helix needs at least one admin." in refused.text


def test_team_posts_work_end_to_end(client, db):
    client.post("/ui/team/people/nikhil/role", data={"role": "admin"})
    assert _people(db)["nikhil"].role == "admin"
    client.post("/ui/team/people/nikhil/rename", data={"name": "Nik"})
    assert _people(db)["nikhil"].name == "Nik"
    inv, token = _invite(db, "fresh")
    client.post(f"/ui/team/invites/{inv}/resend")
    with Session(db) as s:
        assert s.get(models.Invitation, inv).token != token
    client.post(f"/ui/team/invites/{inv}/cancel")
    with Session(db) as s:
        assert team.pending_invites(s) == []
    client.post("/ui/team/people/nikhil/remove")
    assert "nikhil" not in _people(db)


def test_sidebar_has_the_team_link_and_marks_it_active(client, db):
    on_team = client.get("/ui/team").text
    assert 'href="/ui/team" aria-current="page"' in on_team
    other = client.get("/ui/connections").text
    assert 'href="/ui/team"' in other and 'href="/ui/team" aria-current' not in other
