"""Design-system checks for the operator UI: the sign-in page is styled by
classes that style.css really defines, self-hosted fonts are served, the
dashboard stat tiles count correctly and the sidebar marks the active page."""

import re
import time
from html.parser import HTMLParser
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from helix import auth
from state import models
from state.machine import IncidentStatus

UI_STATIC = Path(__file__).resolve().parents[2] / "ui" / "static"


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setenv("HELIX_ENV_FILE", "")
    monkeypatch.delenv("HELIX_ENVIRONMENT", raising=False)
    monkeypatch.delenv("HELIX_EVIDENCE_SOURCE", raising=False)
    monkeypatch.delenv("HELIX_GITHUB_OAUTH_CLIENT_ID", raising=False)


@pytest.fixture
def oauth(monkeypatch):
    monkeypatch.setenv("HELIX_GITHUB_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("HELIX_GITHUB_OAUTH_CLIENT_SECRET", "secret")
    monkeypatch.setenv("HELIX_SESSION_SECRET", "s3cret")


class _Anchors(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.found: list[dict[str, str]] = []

    def handle_starttag(self, tag, attrs):
        if tag in ("a", "button"):
            self.found.append({k: v or "" for k, v in attrs})


def _css() -> str:
    return (UI_STATIC / "style.css").read_text()


def test_login_page_uses_defined_classes_and_shared_layout(client):
    resp = client.get("/login")
    assert resp.status_code == 200
    parser = _Anchors()
    parser.feed(resp.text)
    signin = [a for a in parser.found if a.get("href") == "/auth/github"]
    assert signin, "sign-in link missing"
    classes = signin[0].get("class", "").split()
    assert classes, "sign-in control has no classes"
    css = _css()
    for cls in classes:
        assert f".{cls}" in css, f"class {cls!r} is not defined in style.css"
    assert 'href="/static/style.css"' in resp.text
    assert 'class="brand"' in resp.text
    assert "Sign in with GitHub" in resp.text


def _state_cookie(client, state: str) -> None:
    client.cookies.set(auth.STATE_COOKIE, auth._sign({"state": state, "exp": time.time() + 600}))


def test_not_allowed_page_escapes_login_and_keeps_403(client, oauth, monkeypatch):
    monkeypatch.setattr(auth, "github_login", lambda code, uri: "<script>alert(1)</script>")
    monkeypatch.setenv("HELIX_ALLOWED_USERS", "someone")
    _state_cookie(client, "abc")
    resp = client.get("/auth/github/callback?code=x&state=abc", follow_redirects=False)
    assert resp.status_code == 403
    assert "<script>alert(1)</script>" not in resp.text
    assert "&lt;script&gt;" in resp.text
    assert 'class="brand"' in resp.text


def test_expired_page_keeps_400_and_offers_retry(client, oauth):
    resp = client.get("/auth/github/callback?code=x&state=nope", follow_redirects=False)
    assert resp.status_code == 400
    assert 'href="/login"' in resp.text
    assert 'class="brand"' in resp.text


def test_font_files_declared_in_css_exist_and_are_served(client):
    urls = re.findall(r"url\(['\"]?(/static/fonts/[^)'\"]+)", _css())
    assert len(urls) >= 2
    for url in urls:
        assert (UI_STATIC / url.removeprefix("/static/")).is_file(), url
        resp = client.get(url)
        assert resp.status_code == 200, url
        assert len(resp.content) > 1000
    assert "font-display: swap" in _css()


def _add(engine, statuses: list[IncidentStatus]) -> None:
    with Session(engine) as session:
        for n, status in enumerate(statuses):
            session.add(
                models.Incident(
                    incident_id=f"INC-{n:08x}",
                    fingerprint=f"fp-{n}",
                    service="payment-api",
                    environment="sandbox",
                    severity="high",
                    status=status,
                )
            )
        session.commit()


def test_incident_stat_tiles_count_total_recovered_escalated_open(client, sqlite_engine):
    _add(
        sqlite_engine,
        [
            IncidentStatus.RECOVERED,
            IncidentStatus.RECOVERED,
            IncidentStatus.RECOVERED,
            IncidentStatus.ESCALATED,
            IncidentStatus.DIAGNOSING,
            IncidentStatus.AWAITING_APPROVAL,
            IncidentStatus.FAILED,
        ],
    )
    body = client.get("/ui/").text
    tiles = dict(re.findall(r'data-stat="(\w+)"[^>]*>\s*<span class="stat-value">(\d+)<', body))
    assert tiles == {"total": "7", "recovered": "3", "escalated": "1", "open": "2"}


def test_sidebar_marks_active_page(client):
    incidents = client.get("/ui/").text
    approvals = client.get("/ui/approvals").text
    for body, here, other in (
        (incidents, 'href="/ui/"', 'href="/ui/approvals"'),
        (approvals, 'href="/ui/approvals"', 'href="/ui/"'),
    ):
        current = re.findall(r'<a [^>]*aria-current="page"[^>]*>', body)
        assert len(current) == 1
        assert here in current[0]
        assert other not in current[0]
