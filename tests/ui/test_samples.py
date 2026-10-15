"""Run-a-sample on the hosted demo (helix/demo.py start_sample, POST /ui/samples).

The background drive (`harness._drive`, which shells out to `helix run`) is
stubbed, so these pin the route, the single-flight lock and the gating - not
the workflow, which tests/test_demo.py and the benchmark already cover.
"""

import threading

import pytest
from sqlalchemy import func, select

from benchmark import harness
from helix import demo
from state import models


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setenv("HELIX_ENV_FILE", "")
    monkeypatch.delenv("HELIX_GITHUB_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("HELIX_ENVIRONMENT", raising=False)
    monkeypatch.delenv("HELIX_EVIDENCE_SOURCE", raising=False)


@pytest.fixture
def demo_env(monkeypatch):
    monkeypatch.setenv("HELIX_ENVIRONMENT", "demo")
    monkeypatch.setenv("HELIX_EVIDENCE_SOURCE", "fixtures")


@pytest.fixture
def drives(monkeypatch):
    """Stub the harness drive: records calls, blocks until `release` is set."""
    calls: list[dict] = []
    release = threading.Event()
    finished = threading.Event()

    def fake_drive(incident_id, env, cfg, scenario):
        calls.append({"incident_id": incident_id, "env": env, "scenario": scenario})
        release.wait(5)
        finished.set()
        return []

    monkeypatch.setattr(harness, "_drive", fake_drive)
    release.set()
    return type("Drives", (), {"calls": calls, "release": release, "finished": finished})


def _count(sqlite_engine) -> int:
    from sqlalchemy.orm import Session

    with Session(sqlite_engine) as session:
        return session.scalar(select(func.count()).select_from(models.Incident))


def _wait_idle():
    assert demo._sample_lock.acquire(timeout=5)
    demo._sample_lock.release()


def test_post_creates_one_incident_and_redirects(client, sqlite_engine, demo_env, drives):
    resp = client.post("/ui/samples", follow_redirects=False)

    assert resp.status_code == 303
    assert _count(sqlite_engine) == 1
    incident_id = resp.headers["location"].rsplit("/", 1)[-1]
    assert resp.headers["location"] == f"/ui/incidents/{incident_id}"
    assert drives.finished.wait(5)
    _wait_idle()
    (call,) = drives.calls
    assert call["incident_id"] == incident_id
    assert call["scenario"]["evidence_fixture"] in {
        harness.catalog.load_scenarios()[name]["evidence_fixture"] for name in demo.SAMPLE_SCENARIOS
    }
    assert call["env"]["HELIX_FIXTURE_PATH"] == call["scenario"]["evidence_fixture"]
    assert call["env"]["HELIX_ARM"] == "A"
    assert call["env"]["UV_NO_SYNC"] == "1"
    assert client.get(resp.headers["location"]).status_code == 200


def test_sample_incident_page_polls_while_running(client, demo_env, drives):
    drives.release.clear()
    resp = client.post("/ui/samples", follow_redirects=False)
    page = client.get(resp.headers["location"])
    drives.release.set()
    _wait_idle()

    assert "live - polling" in page.text
    assert 'data-incident-status="' in page.text


def test_second_post_while_running_is_busy(client, sqlite_engine, demo_env, drives, monkeypatch):
    # The stubbed drive never finishes an incident, so a repeat scenario
    # would (correctly) dedupe onto the open one; pick distinct scenarios.
    picks = iter(demo.SAMPLE_SCENARIOS)
    monkeypatch.setattr(demo.random, "choice", lambda _seq: next(picks))
    drives.release.clear()
    first = client.post("/ui/samples", follow_redirects=False)
    second = client.post("/ui/samples", follow_redirects=False)
    try:
        assert second.status_code == 303
        assert second.headers["location"] == first.headers["location"] + "?busy=1"
        assert _count(sqlite_engine) == 1
        assert len(drives.calls) <= 1
        busy_page = client.get(second.headers["location"])
        assert "A sample is already running - showing it." in busy_page.text
        assert "A sample is already running" not in client.get(first.headers["location"]).text
    finally:
        drives.release.set()
    _wait_idle()

    third = client.post("/ui/samples", follow_redirects=False)
    assert "busy" not in third.headers["location"]
    assert _count(sqlite_engine) == 2
    _wait_idle()


def test_lock_released_when_drive_raises(client, demo_env, monkeypatch):
    def boom(*_args, **_kwargs):
        raise RuntimeError("drive exploded")

    monkeypatch.setattr(harness, "_drive", boom)
    resp = client.post("/ui/samples", follow_redirects=False)

    assert resp.status_code == 303
    _wait_idle()
    assert demo._running_incident is None


def test_lock_released_when_intake_fails(client, demo_env, monkeypatch):
    def boom(*_args, **_kwargs):
        raise RuntimeError("intake exploded")

    monkeypatch.setattr(demo, "intake_signal", boom)
    with pytest.raises(RuntimeError):
        client.post("/ui/samples", follow_redirects=False)
    _wait_idle()
    assert demo._running_incident is None


def test_not_available_outside_demo(client, sqlite_engine, drives):
    assert client.post("/ui/samples", follow_redirects=False).status_code == 404
    assert _count(sqlite_engine) == 0
    assert "Run a sample incident" not in client.get("/ui/").text


def test_not_available_on_live_evidence(client, monkeypatch, drives):
    monkeypatch.setenv("HELIX_ENVIRONMENT", "demo")
    assert client.post("/ui/samples", follow_redirects=False).status_code == 404


def test_button_renders_in_demo(client, demo_env, drives):
    page = client.get("/ui/").text
    assert "Run a sample incident" in page
    assert 'action="/ui/samples"' in page
    assert "helix intake --file" not in page


def test_button_renders_with_incidents_too(client, sqlite_engine, demo_env, drives):
    client.post("/ui/samples", follow_redirects=False)
    _wait_idle()
    page = client.get("/ui/").text
    assert page.count('action="/ui/samples"') == 1


def test_unauthenticated_post_starts_nothing(client, sqlite_engine, demo_env, drives, monkeypatch):
    monkeypatch.setenv("HELIX_GITHUB_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("HELIX_GITHUB_OAUTH_CLIENT_SECRET", "secret")
    monkeypatch.setenv("HELIX_SESSION_SECRET", "s3cret")

    resp = client.post("/ui/samples", follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"
    assert _count(sqlite_engine) == 0
    assert drives.calls == []
