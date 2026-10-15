"""Helix is a product, not a demo: no page frames itself as one, the test
incident button follows recorded evidence (not an environment label), and
the hosted start script does not seed."""

import re
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from state import models
from state.machine import IncidentStatus

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setenv("HELIX_ENV_FILE", "")
    monkeypatch.delenv("HELIX_GITHUB_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("HELIX_ENVIRONMENT", raising=False)
    monkeypatch.delenv("HELIX_EVIDENCE_SOURCE", raising=False)


def _visible(html: str) -> str:
    html = re.sub(r"(?is)<(script|style).*?</\1>", "", html)
    return re.sub(r"(?s)<[^>]+>", " ", html)


def _incident(engine) -> str:
    with Session(engine) as session:
        session.add(
            models.Incident(
                incident_id="INC-0000000a",
                fingerprint="fp-a",
                service="payment-api",
                environment="sandbox",
                severity="high",
                status=IncidentStatus.RECOVERED,
            )
        )
        session.commit()
    return "INC-0000000a"


@pytest.mark.parametrize("label", ["demo", "production"])
def test_no_page_says_demo(client, sqlite_engine, monkeypatch, label):
    monkeypatch.setenv("HELIX_ENVIRONMENT", label)
    monkeypatch.setenv("HELIX_EVIDENCE_SOURCE", "fixtures")
    incident_id = _incident(sqlite_engine)
    for path in [
        "/ui/",
        f"/ui/incidents/{incident_id}",
        "/ui/approvals",
        "/ui/connections",
        "/login",
    ]:
        resp = client.get(path)
        assert resp.status_code == 200, path
        text = _visible(resp.text)
        assert "recorded evidence" not in text.lower(), path
        assert not re.search(r"demo", text, re.I), path


def test_test_incident_button_on_recorded_evidence(client, monkeypatch):
    monkeypatch.setenv("HELIX_EVIDENCE_SOURCE", "fixtures")
    page = client.get("/ui/incidents").text
    assert "Send a test incident" in page
    assert "Run a sample incident" not in page
    assert 'action="/ui/samples"' in page
    assert "No incidents yet. Send a test incident" in " ".join(_visible(page).split())


def test_test_incident_post_works_on_recorded_evidence(client, monkeypatch):
    from helix import demo

    monkeypatch.setenv("HELIX_EVIDENCE_SOURCE", "fixtures")
    monkeypatch.setenv("HELIX_ENVIRONMENT", "production")
    monkeypatch.setattr(demo, "start_sample", lambda session: ("INC-00000001", False))
    resp = client.post("/ui/samples", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/ui/incidents/INC-00000001"


def test_test_incident_hidden_on_live_evidence(client, monkeypatch):
    monkeypatch.setenv("HELIX_EVIDENCE_SOURCE", "live")
    page = client.get("/ui/incidents").text
    assert "Send a test incident" not in page
    assert "helix intake --file" in page
    assert client.post("/ui/samples", follow_redirects=False).status_code == 404


def test_render_start_does_not_seed():
    script = (ROOT / "scripts" / "render-start.sh").read_text()
    assert "seed-demo" not in script
    assert "HELIX_ENVIRONMENT" not in script


def test_render_blueprint_is_production():
    blueprint = (ROOT / "render.yaml").read_text()
    assert "value: demo" not in blueprint
    assert "value: production" in blueprint
