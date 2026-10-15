"""Demo seeding (helix/demo.py).

The non-empty early exit and the failure paths, on SQLite with the harness
stubbed. The real seed is tests/state/test_demo_seed.py (integration tier).
"""

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
    monkeypatch.setattr(demo, "_park_review", _boom)

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
        demo._park_review(sqlite_url)
