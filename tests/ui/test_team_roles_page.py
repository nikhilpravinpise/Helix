"""Owner > Admin > Member over HTTP: who may manage services and connections,
what the Team page shows each viewer, and the main owner hand-over. A file
SQLite database so the sign-in check and the page see the same rows; GitHub is
faked."""

import re
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
    url = f"sqlite:///{tmp_path / 'roles.db'}"
    engine = create_engine(url)
    models.Base.metadata.create_all(engine)
    monkeypatch.setenv("HELIX_DATABASE_URL", url)
    monkeypatch.delenv("HELIX_SERVICE_REGISTRY", raising=False)
    monkeypatch.delenv("HELIX_ALLOWED_USERS", raising=False)
    with Session(engine) as s:
        s.add_all(
            [
                models.Person(login="main", name="Main Owner", role="owner", main_owner=True),
                models.Person(login="co", name="Co Owner", role="owner"),
                models.Person(login="adm", name="An Admin", role="admin"),
                models.Person(login="mem", name="A Member", role="member"),
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


def _sign_in(client, monkeypatch, login, name=None):
    monkeypatch.setattr(auth, "github_login", lambda code, uri: auth.GitHubUser(login, name))
    start = client.get("/auth/github", follow_redirects=False)
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    return client.get(f"/auth/github/callback?code=c&state={state}", follow_redirects=False)


def _people(db):
    with Session(db) as s:
        return {p.login: p for p in s.scalars(select(models.Person))}


def _flash(response):
    """The red notice a refused change comes back as."""
    match = re.search(r'class="notice notice-bad"[^>]*>([^<]*)<', response.text)
    return match.group(1) if match else ""


def _row(page_text, login):
    """The HTML of one person's row on the Team page."""
    rows = re.findall(r'<li class="team-row">.*?</li>', page_text, flags=re.S)
    return next(r for r in rows if f'<span class="mono small muted">{login}</span>' in r)


# ---- services and connections need an admin or above --------------------------


SERVICE_FORM = {"name": "orders", "team": "core", "repo": "acme/orders", "owner": ["main"]}


@pytest.mark.parametrize(
    ("viewer", "allowed"),
    [("main", True), ("co", True), ("adm", True), ("mem", False)],
)
def test_may_manage_by_rank(client, db, monkeypatch, viewer, allowed):
    _sign_in(client, monkeypatch, viewer)
    service = client.post("/ui/services", data=SERVICE_FORM, follow_redirects=False)
    connection = client.post("/ui/connections/slack/disconnect", follow_redirects=False)
    if allowed:
        assert service.status_code == 303 and connection.status_code == 303
    else:
        assert service.status_code == 403 and connection.status_code == 403
    with Session(db) as s:
        assert (s.get(models.Service, "orders") is not None) is allowed


def test_sign_in_off_acts_as_an_owner_but_cannot_hand_over_main_ownership(db, monkeypatch):
    monkeypatch.delenv("HELIX_GITHUB_OAUTH_CLIENT_ID", raising=False)

    def override():
        with session_scope(db) as session:
            yield session

    app.dependency_overrides[get_db_session] = override
    app.dependency_overrides[ui.get_db_session] = override
    try:
        with TestClient(app, base_url="https://helix.example") as c:
            done = c.post("/ui/team/people/co/role", data={"role": "admin"}, follow_redirects=False)
            assert done.status_code == 303 and _people(db)["co"].role == "admin"
            refused = c.post("/ui/team/people/co/main-owner", follow_redirects=True)
            assert "Only the main owner" in _flash(refused)
    finally:
        app.dependency_overrides.clear()


def test_a_user_from_the_allow_list_override_without_a_row_acts_as_an_admin(
    client, db, monkeypatch
):
    """Decided in issue #45: someone the HELIX_ALLOWED_USERS override lets in
    without a people row acts as an admin, not an owner."""
    monkeypatch.setenv("HELIX_ALLOWED_USERS", "operator")
    _sign_in(client, monkeypatch, "operator")
    done = client.post(
        "/ui/team/invite", data={"login": "fresh", "role": "member"}, follow_redirects=False
    )
    assert done.status_code == 303
    refused = client.post(
        "/ui/team/invite", data={"login": "boss", "role": "owner"}, follow_redirects=True
    )
    assert _flash(refused)
    with Session(db) as s:
        assert [(i.login, i.role) for i in team.pending_invites(s)] == [("fresh", "member")]
    client.post("/ui/team/people/mem/rename", data={"name": "Renamed"})
    assert _people(db)["mem"].name == "Renamed"
    refused = client.post(
        "/ui/team/people/adm/role", data={"role": "member"}, follow_redirects=True
    )
    assert _flash(refused) and _people(db)["adm"].role == "admin"


# ---- the rules reach the page as notices --------------------------------------


def test_an_admin_is_refused_on_owners_and_admins_with_a_plain_notice(client, db, monkeypatch):
    _sign_in(client, monkeypatch, "adm")
    for path, data in [
        ("/ui/team/people/co/role", {"role": "member"}),
        ("/ui/team/people/adm/remove", {}),
        ("/ui/team/people/main/rename", {"name": "X"}),
        ("/ui/team/people/mem/role", {"role": "admin"}),
        ("/ui/team/invite", {"login": "boss", "role": "owner"}),
    ]:
        refused = client.post(path, data=data, follow_redirects=True)
        assert _flash(refused), path
    people = _people(db)
    assert (people["co"].role, people["mem"].role, people["main"].name) == (
        "owner",
        "member",
        "Main Owner",
    )
    assert "adm" in people
    ok = client.post("/ui/team/people/mem/role", data={"role": "member"}, follow_redirects=False)
    assert ok.status_code == 303
    assert client.post("/ui/team/people/mem/remove", follow_redirects=False).status_code == 303
    assert "mem" not in _people(db)


def test_an_admin_cannot_resend_or_cancel_an_admin_or_owner_invite(client, db, monkeypatch):
    with Session(db) as s:
        owner_invite = team.invite(s, "future-owner", "owner", "main")
        member_invite = team.invite(s, "future-member", "member", "main")
        s.commit()
        ids = (owner_invite.id, member_invite.id)
    _sign_in(client, monkeypatch, "adm")
    for action in ("resend", "cancel"):
        refused = client.post(f"/ui/team/invites/{ids[0]}/{action}", follow_redirects=True)
        assert "Admins can only change invites for members" in _flash(refused)
    done = client.post(f"/ui/team/invites/{ids[1]}/cancel", follow_redirects=False)
    assert done.status_code == 303
    with Session(db) as s:
        assert [i.login for i in team.pending_invites(s)] == ["future-owner"]


def test_signing_in_on_an_owner_invite_makes_a_co_owner(client, db, monkeypatch):
    with Session(db) as s:
        team.invite(s, "future-owner", "owner", "main")
        s.commit()
    assert _sign_in(client, monkeypatch, "future-owner", "FO").status_code == 303
    person = _people(db)["future-owner"]
    assert (person.role, person.main_owner) == ("owner", False)


# ---- handing over main ownership ----------------------------------------------


def test_the_main_owner_hands_over_from_the_page(client, db, monkeypatch):
    _sign_in(client, monkeypatch, "main")
    done = client.post("/ui/team/people/co/main-owner", follow_redirects=True)
    # Someone on the team is named by display name, like everywhere else.
    assert "Co Owner is now the main owner." in done.text
    people = _people(db)
    assert (people["co"].main_owner, people["main"].main_owner, people["main"].role) == (
        True,
        False,
        "owner",
    )


def test_a_co_owner_cannot_take_main_ownership(client, db, monkeypatch):
    _sign_in(client, monkeypatch, "co")
    refused = client.post("/ui/team/people/co/main-owner", follow_redirects=True)
    assert "Only the main owner" in _flash(refused)
    assert _people(db)["main"].main_owner


# ---- what the Team page shows -------------------------------------------------


def test_badges_the_main_owner_tag_and_the_role_blurb(client, db, monkeypatch):
    _sign_in(client, monkeypatch, "mem")
    text = client.get("/ui/team").text
    assert "Owners manage everything" in text and "Admins manage services" in text
    assert text.count("Main owner</span>") == 1 and "Main owner</span>" in _row(text, "main")
    for login, badge in [("main", "Owner"), ("co", "Owner"), ("adm", "Admin"), ("mem", "Member")]:
        assert f">{badge}</span>" in _row(text, login)
    assert "Main owner</span>" not in _row(text, "co")


def test_the_main_owner_sees_every_action_except_on_themselves(client, db, monkeypatch):
    _sign_in(client, monkeypatch, "main")
    text = client.get("/ui/team").text
    me = _row(text, "main")
    assert "Rename" in me and "Remove" not in me and "Change role" not in me
    assert "Main owner</span>" in me and "Make main owner" not in me
    co = _row(text, "co")
    for label in ("Change role", "Rename", "Remove", "Find in Slack", "Make main owner"):
        assert label in co
    # The role dropdown shows their current role selected, then what they can become.
    assert re.findall(r'<option value="(\w+)"', co) == ["owner", "admin", "member"]
    assert '<option value="owner" selected>' in co
    adm = _row(text, "adm")
    assert "Make main owner" not in adm
    assert re.findall(r'<option value="(\w+)"', adm) == ["owner", "admin", "member"]
    assert '<option value="admin" selected>' in adm


def test_a_co_owner_cannot_touch_the_main_owner_or_make_a_main_owner(client, db, monkeypatch):
    _sign_in(client, monkeypatch, "co")
    text = client.get("/ui/team").text
    main = _row(text, "main")
    for label in ("Rename", "Remove", "Change role", "Find in Slack", "Make main owner"):
        assert label not in main
    assert "Make main owner" not in text
    assert "Remove" in _row(text, "co") and "Remove" in _row(text, "adm")


def test_an_admin_sees_actions_only_on_members(client, db, monkeypatch):
    _sign_in(client, monkeypatch, "adm")
    text = client.get("/ui/team").text
    for login in ("main", "co", "adm"):
        row = _row(text, login)
        assert "Remove" not in row and "Change role" not in row, login
        assert "Rename" not in row or login == "adm", login
    assert "Rename" in _row(text, "adm")  # themselves
    mem = _row(text, "mem")
    for label in ("Rename", "Remove"):
        assert label in mem
    # admins can only grant member, and a member already is one
    assert "Change role" not in mem
    assert "Make main owner" not in text


def test_a_member_sees_a_read_only_page_with_only_their_own_rename(client, db, monkeypatch):
    _sign_in(client, monkeypatch, "mem")
    text = client.get("/ui/team").text
    assert "Only owners and admins can change the team." in text
    assert 'action="/ui/team/invite"' not in text
    assert text.count("Rename") == 1 and "Rename" in _row(text, "mem")
    for label in ("Change role", "Remove", "Resend", "Make main owner"):
        assert label not in text


@pytest.mark.parametrize(
    ("viewer", "options"),
    [
        ("main", ["owner", "admin", "member"]),
        ("co", ["owner", "admin", "member"]),
        ("adm", ["member"]),
    ],
)
def test_the_invite_form_lists_only_roles_the_viewer_may_grant(
    client, db, monkeypatch, viewer, options
):
    _sign_in(client, monkeypatch, viewer)
    text = client.get("/ui/team").text
    select_html = re.search(r'<select id="invite-role".*?</select>', text, flags=re.S).group(0)
    assert re.findall(r'<option value="(\w+)"', select_html) == options
    assert '<option value="member" selected>' in select_html


def test_pending_invites_show_their_role_and_members_get_no_controls(client, db, monkeypatch):
    with Session(db) as s:
        team.invite(s, "future-owner", "owner", "main")
        team.invite(s, "future-member", "member", "main")
        s.commit()
    _sign_in(client, monkeypatch, "adm")
    text = client.get("/ui/team").text
    invites = text[text.index('id="invites"') :]
    assert ">Owner</span>" in invites and ">Member</span>" in invites
    # an admin may act on the member invite only
    assert invites.count("Resend") == 1
    _sign_in(client, monkeypatch, "mem")
    text = client.get("/ui/team").text
    assert "Resend" not in text and 'action="/ui/team/invite"' not in text
