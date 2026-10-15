"""Demo seeding (helix/demo.py).

The non-empty early exit and the failure paths, on SQLite with the harness
stubbed. The real seed is tests/state/test_demo_seed.py (integration tier).
"""

import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from helix import demo
from state import models
from state.intake import Signal, intake_signal


@pytest.fixture
def sqlite_url(tmp_path) -> str:
    url = f"sqlite:///{tmp_path / 'demo.db'}"
    engine = create_engine(url)
    models.Base.metadata.create_all(engine)
    engine.dispose()
    return url


def _add_incident(url: str) -> None:
    engine = create_engine(url)
    with Session(engine) as session:
        intake_signal(
            session,
            Signal(source="kubernetes", service="payment-api", signature={"reason": "CrashLoop"}),
        )
        session.commit()
    engine.dispose()


def _boom(*_args, **_kwargs):
    raise AssertionError("the harness must not run")


def test_non_empty_database_is_left_alone(sqlite_url, monkeypatch, capsys):
    _add_incident(sqlite_url)
    monkeypatch.setattr(demo.harness, "run_benchmark", _boom)
    monkeypatch.setenv("HELIX_DATABASE_URL", sqlite_url)
    monkeypatch.setenv("HELIX_ENV_FILE", "")

    assert demo.seed_demo(sqlite_url) == 0
    assert demo.main() == 0
    assert "skipped" in capsys.readouterr().out


def test_skipped_scenario_is_a_seed_error(sqlite_url, monkeypatch):
    outcomes = [{"scenario": name, "skipped": None} for name in demo.DEMO_SCENARIOS]
    outcomes[2]["skipped"] = "intake failed"
    monkeypatch.setattr(demo.harness, "run_benchmark", lambda cfg: {"outcomes": outcomes})
    monkeypatch.setattr(demo, "_park_reviews", _boom)

    with pytest.raises(demo.SeedError, match=outcomes[2]["scenario"]):
        demo.seed_demo(sqlite_url)


def test_missing_scenarios_are_a_seed_error(sqlite_url, monkeypatch):
    monkeypatch.setattr(demo.harness, "run_benchmark", lambda cfg: {"outcomes": []})

    with pytest.raises(demo.SeedError, match="catalog mismatch"):
        demo.seed_demo(sqlite_url)


def test_main_reports_failure_with_nonzero_exit(sqlite_url, monkeypatch, capsys):
    def fail(*_args, **_kwargs):
        raise demo.SeedError("scenarios did not run: x")

    monkeypatch.setattr(demo, "seed_demo", fail)

    assert demo.main() == 1
    assert "scenarios did not run" in capsys.readouterr().err


def test_park_failure_is_a_seed_error(sqlite_url, monkeypatch):
    monkeypatch.setattr(
        demo.harness, "_ingest_declared_events", lambda scenario, url: ([], "event ingest failed")
    )

    with pytest.raises(demo.SeedError, match="event ingest failed"):
        demo._park_review(sqlite_url, "VrajVed", 101)


def test_parked_authors_are_the_live_registry_reviewers():
    from agent.ops.registry import load_registry

    registry = load_registry(demo.LIVE_REGISTRY)
    assert set(demo.PARKED_AUTHORS) == set(registry.services["payment-api"].default_reviewers)


def test_seeding_never_writes_live(sqlite_url, monkeypatch):
    """Seeding runs on every empty boot: whatever the process env says, the
    harness and the parked-review subprocesses get recorded writes."""
    monkeypatch.setenv("HELIX_GITHUB_WRITES", "live")
    monkeypatch.setenv("HELIX_DATABASE_URL", sqlite_url)
    monkeypatch.setenv("HELIX_ENV_FILE", "")
    seen: list[str | None] = []

    def run_benchmark(cfg):
        seen.append(os.environ.get("HELIX_GITHUB_WRITES"))
        return {"outcomes": [{"scenario": s, "skipped": None} for s in demo.DEMO_SCENARIOS]}

    def park_reviews(url):
        seen.append(os.environ.get("HELIX_GITHUB_WRITES"))
        return []

    monkeypatch.setattr(demo.harness, "run_benchmark", run_benchmark)
    monkeypatch.setattr(demo, "_park_reviews", park_reviews)

    demo.seed_demo(sqlite_url)

    assert seen == ["recorded", "recorded"]


def test_parked_review_subprocess_env_is_recorded(sqlite_url, monkeypatch):
    monkeypatch.setenv("HELIX_GITHUB_WRITES", "live")
    envs: list[dict] = []
    monkeypatch.setattr(
        demo.harness,
        "_ingest_declared_events",
        lambda scenario, url: ([{"incident_id": "inc-x"}], None),
    )

    def helix(args, env):
        envs.append(env)
        raise RuntimeError("stop after the first run")

    monkeypatch.setattr(demo.harness, "_helix", helix)

    with pytest.raises(RuntimeError):
        demo._park_review(sqlite_url, "VrajVed", 101)

    assert envs[0]["HELIX_GITHUB_WRITES"] == "recorded"


def test_parked_review_stays_offline_and_off_the_connections(sqlite_url, monkeypatch):
    """Seeding must never reach a connected Slack, Jira or GitHub account,
    whatever a sample run is allowed to do."""
    monkeypatch.setenv("HELIX_CONNECTIONS_ENABLED", "true")
    monkeypatch.setenv("HELIX_WORK_PROVIDER", "jira")
    envs: list[dict] = []
    monkeypatch.setattr(
        demo.harness,
        "_ingest_declared_events",
        lambda scenario, url: ([{"incident_id": "inc-x"}], None),
    )

    def helix(args, env):
        envs.append(env)
        raise RuntimeError("stop after the first run")

    monkeypatch.setattr(demo.harness, "_helix", helix)

    with pytest.raises(RuntimeError):
        demo._park_review(sqlite_url, "VrajVed", 101)

    env = envs[0]
    assert env["HELIX_CONNECTIONS_ENABLED"] == "false"
    assert env["HELIX_WORK_PROVIDER"] == "memory"
    assert env["HELIX_SLACK_BOT_TOKEN"] == ""
    assert env["HELIX_GITHUB_WRITES"] == "recorded"
    assert env["HELIX_SERVICE_REGISTRY"] == str(demo.LIVE_REGISTRY)


def test_the_benchmark_harness_env_stays_isolated():
    env = demo.harness._isolated_env()

    assert env["HELIX_CONNECTIONS_ENABLED"] == "false"
    assert env["HELIX_WORK_PROVIDER"] == "memory"
    assert env["HELIX_SERVICE_REGISTRY"] == str(demo.harness.BENCHMARK_REGISTRY)
