"""Seed a hosted demo's empty database with recorded incidents.

A fresh Render deploy has an empty dashboard and no way for a visitor to
raise an incident (no cluster, no Jenkins, no intake token to hand out).
`seed_demo` fills it once: the fixture scenarios run through the benchmark
harness on arm A - the same code path an operator drives - and one change
incident is parked at `review_pending`, so the dashboard has a pending
review to decide. It is a no-op on a database that already holds any
incident, so it never touches real data and is safe on every boot.
"""

import os
import sys

from sqlalchemy import create_engine, func, select

from benchmark import harness
from helix.config import get_settings
from state import models
from state.db import session_scope
from state.machine import IncidentStatus

#: Fixture-mode scenarios the demo replays to their end state (seven
#: recover, the missing-owner one escalates). The org-* scenarios declare no
#: recording, so fixture mode skips them.
DEMO_SCENARIOS = (
    "ci-test-regression",
    "ci-dependency-failure",
    "ci-docker-build-failure",
    "failed-check",
    "rejected-pr",
    "stale-branch",
    "xsys-pr-deploy-failure",
    "xsys-runtime-missing-owner",
)

#: The change scenario parked at review, and the recording its runs replay
#: (a change incident needs one to triage, even though it never deploys).
PARKED_SCENARIO = "org-known-owner-change"
PARKED_FIXTURE = "ci/docker-build-failure"


class SeedError(RuntimeError):
    """The demo data could not be seeded."""


def _incident_count(database_url: str) -> int:
    engine = create_engine(database_url)
    try:
        with session_scope(engine) as session:
            return session.scalar(select(func.count()).select_from(models.Incident)) or 0
    finally:
        engine.dispose()


def _park_review(database_url: str) -> str:
    """Ingest the change scenario's PR and run it to `review_pending`
    (triage, owner, review request) without the scripted reviewer deciding
    - the visitor does that from the dashboard. Returns the incident id."""
    scenario = harness.catalog.load_scenarios()[PARKED_SCENARIO]
    results, error = harness._ingest_declared_events(scenario, database_url)
    if error:
        raise SeedError(f"{PARKED_SCENARIO}: {error}")
    incident_id = results[-1]["incident_id"]
    env = harness._isolated_env()
    env.update(harness.FIXTURE_RUN_ENV)
    env.update(HELIX_ARM="A", HELIX_DATABASE_URL=database_url, HELIX_FIXTURE_PATH=PARKED_FIXTURE)
    # Triage and review-request are separate stopping points: two runs.
    for _hop in range(2):
        proc = harness._helix(["run", incident_id], env)
        if proc.returncode not in (0, 1):
            raise SeedError(f"{PARKED_SCENARIO}: helix run failed: {proc.stderr.strip()[:300]}")
    status = harness._current_status(database_url, incident_id)
    if status != IncidentStatus.REVIEW_PENDING.value:
        raise SeedError(f"{PARKED_SCENARIO}: expected review_pending, ended at {status}")
    return incident_id


def seed_demo(database_url: str | None = None) -> int:
    """Seed the demo incidents; returns how many scenarios were seeded, or
    0 when the database already has incidents. Raises SeedError on failure
    (incidents seeded before the failure stay - a later boot sees a
    non-empty database and leaves it alone)."""
    url = database_url or get_settings().database_url
    if _incident_count(url):
        return 0
    # The harness shells out to `uv run helix`; the image has no dev group
    # and must not try to re-sync (or reach the network) per scenario.
    os.environ.setdefault("UV_NO_SYNC", "1")
    cfg = harness.HarnessConfig(
        arms=["A"],
        scenarios=list(DEMO_SCENARIOS),
        mode="fixture",
        database_url=url,
    )
    report = harness.run_benchmark(cfg)
    failed = [o["scenario"] for o in report["outcomes"] if o["skipped"]]
    if failed or len(report["outcomes"]) != len(DEMO_SCENARIOS):
        raise SeedError(f"scenarios did not run: {', '.join(failed) or 'catalog mismatch'}")
    _park_review(url)
    return len(DEMO_SCENARIOS) + 1


def main() -> int:
    """`helix seed-demo`: 0 on seeded or already-populated, 1 on failure."""
    try:
        seeded = seed_demo()
    except (SeedError, OSError) as exc:
        print(f"helix seed-demo: {exc}", file=sys.stderr)
        return 1
    if seeded:
        print(f"seeded {seeded} demo incidents")
    else:
        print("database already has incidents - demo seeding skipped")
    return 0
