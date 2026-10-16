"""Invite emails on the Team page. Resend's API is faked; nothing leaves the
test. Every send is best effort - a provider failure never blocks an invite."""

import json
from datetime import UTC, datetime
from urllib.parse import parse_qs, quote, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from helix import auth, invite_email, ui
from helix.api import app, get_db_session
from integrations import email
from state import models
from state.db import session_scope


@pytest.fixture
def db(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'team.db'}"
    engine = create_engine(url)
    models.Base.metadata.create_all(engine)
    monkeypatch.setenv("HELIX_DATABASE_URL", url)
    monkeypatch.delenv("HELIX_SERVICE_REGISTRY", raising=False)
    monkeypatch.delenv("HELIX_ALLOWED_USERS", raising=False)
    monkeypatch.delenv("HELIX_EMAIL_API_KEY", raising=False)
    monkeypatch.delenv("HELIX_EMAIL_FROM", raising=False)
    with Session(engine) as s:
        s.add(models.Person(login="VrajVed", name="Vraj Ved", role="owner", main_owner=True))
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
def mail(monkeypatch):
    """Fake Resend: `sent` is the JSON bodies posted; set `status` to fail."""
    sent: list[dict] = []
    state = {"status": 200}

    def handler(request: httpx.Request) -> httpx.Response:
        if state["status"] == "down":
            raise httpx.ConnectError("down")
        sent.append(json.loads(request.content))
        if state["status"] != 200:
            return httpx.Response(state["status"], json={"message": "nope"})
        return httpx.Response(200, json={"id": f"em_{len(sent)}"})

    monkeypatch.setattr(
        email, "_http", lambda: httpx.Client(transport=httpx.MockTransport(handler), timeout=5)
    )
    return type("Mail", (), {"sent": sent, "state": state})


@pytest.fixture
def email_on(monkeypatch):
    monkeypatch.setenv("HELIX_EMAIL_API_KEY", "re_secret_key")
    monkeypatch.setenv("HELIX_EMAIL_FROM", "Helix <invites@example.com>")


def _sign_in(client, monkeypatch, login="VrajVed"):
    monkeypatch.setattr(auth, "github_login", lambda code, uri: auth.GitHubUser(login))
    start = client.get("/auth/github", follow_redirects=False)
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    client.get(f"/auth/github/callback?code=c&state={state}", follow_redirects=False)


def _invite(client, email_="new@x.com", role="member"):
    return client.post(
        "/ui/team/invite",
        data={"login": "newbie", "role": role, "email": email_},
        follow_redirects=True,
    )


def _row(db):
    with Session(db) as s:
        return s.scalar(select(models.Invitation).where(models.Invitation.login == "newbie"))


def test_invite_with_an_email_sends_one_email_with_the_link(
    client, db, mail, email_on, monkeypatch
):
    _sign_in(client, monkeypatch)
    page = _invite(client, role="admin")
    row = _row(db)
    assert len(mail.sent) == 1
    message = mail.sent[0]
    link = f"https://helix.example/invite/{row.token}"
    assert message["to"] == ["new@x.com"] and message["from"] == "Helix <invites@example.com>"
    assert message["subject"] == "Vraj Ved invited you to Helix"
    day = row.expires_at
    expires = f"{day:%B} {day.day}, {day.year}"
    for body in (message["html"], message["text"]):
        assert link in body and "Vraj Ved" in body and "an admin" in body
        assert "newbie" in body and expires in body
    assert row.emailed_at is not None
    assert "Invited newbie. Emailed the invite link to new@x.com." in page.text


def test_email_off_creates_the_invite_and_says_so(client, db, mail, monkeypatch):
    _sign_in(client, monkeypatch)
    page = _invite(client)
    assert mail.sent == [] and _row(db).emailed_at is None
    assert "Email is not set up, so copy the invite link and send it to them." in page.text


@pytest.mark.parametrize("missing", ["HELIX_EMAIL_API_KEY", "HELIX_EMAIL_FROM"])
def test_email_needs_both_settings(client, db, mail, email_on, monkeypatch, missing):
    monkeypatch.delenv(missing)
    _sign_in(client, monkeypatch)
    page = _invite(client)
    assert mail.sent == [] and _row(db) is not None
    assert "Email is not set up" in page.text


@pytest.mark.parametrize(
    ("status", "code"), [(403, "http_403"), (500, "http_500"), ("down", "transport_error")]
)
def test_a_provider_failure_never_blocks_the_invite(
    client, db, mail, email_on, monkeypatch, status, code
):
    mail.state["status"] = status
    _sign_in(client, monkeypatch)
    page = _invite(client)
    assert _row(db) is not None and _row(db).emailed_at is None
    assert f"Could not email them ({code}); copy the invite link and send it to them." in page.text
    assert "re_secret_key" not in page.text


def test_a_crafted_error_code_cannot_put_words_in_the_notice(
    client, db, mail, email_on, monkeypatch
):
    _sign_in(client, monkeypatch)
    _invite(client)
    page = client.get(
        "/ui/team?notice=invited&login=newbie&mail=failed&mailerr=" + quote("<b>hi you</b>")
    )
    assert "hi you" not in page.text and "Could not email them (error)" in page.text


def test_an_invite_without_an_email_sends_nothing(client, db, mail, email_on, monkeypatch):
    _sign_in(client, monkeypatch)
    page = _invite(client, email_="")
    assert mail.sent == [] and _row(db) is not None
    assert "Invited newbie. Copy the invite link and send it to them." in page.text


def test_resend_emails_the_new_link_and_updates_emailed_at(client, db, mail, email_on, monkeypatch):
    _sign_in(client, monkeypatch)
    _invite(client)
    first = _row(db)
    old_token, first_at = first.token, first.emailed_at
    page = client.post(f"/ui/team/invites/{first.id}/resend", follow_redirects=True)
    row = _row(db)
    assert row.token != old_token and len(mail.sent) == 2
    assert f"https://helix.example/invite/{row.token}" in mail.sent[1]["text"]
    assert old_token not in mail.sent[1]["text"]
    assert row.emailed_at > first_at
    assert "Emailed the new link to new@x.com." in page.text


def test_resend_survives_a_failed_email(client, db, mail, email_on, monkeypatch):
    _sign_in(client, monkeypatch)
    _invite(client)
    mail.state["status"] = 403
    page = client.post(f"/ui/team/invites/{_row(db).id}/resend", follow_redirects=True)
    assert "Could not email them (http_403)" in page.text and _row(db) is not None


def test_the_html_escapes_every_value():
    _, html, text = invite_email.build(
        '<script>alert("x")</script>',
        "member",
        '"><img src=x>',
        'https://h.example/invite/a"b<c',
        datetime(2026, 10, 23, tzinfo=UTC),
    )
    assert "<script>" not in html and "<img" not in html and 'a"b<c' not in html
    assert "&lt;script&gt;" in html and "October 23, 2026" in html
    assert '<script>alert("x")</script>' in text
