"""Seed an empty database with recorded example incidents (a developer tool).

`seed_demo` fills a fresh database for local development: the fixture
scenarios run through the benchmark harness on arm A - the same code path an
operator drives - and two change incidents are parked at `review_pending`
(one per live reviewer, see PARKED_AUTHORS), so the dashboard has pending
reviews to decide. It is a no-op on a database that already holds any
incident, so it never touches real data.

`start_sample` backs the dashboard's "Send a test incident" button: it
raises one signal-flow incident from the same recordings and drives it live
in a background thread, one at a time (the free host has no memory for two runs).
"""

import copy
import os
import random
import sys
import threading

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from benchmark import harness
from helix.config import get_settings
from helix.logging import get_logger
from state import models
from state.db import session_scope
from state.intake import Signal, intake_signal
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

#: The PR authors of the parked reviews: the live registry's logins, the only
#: people who can sign in to the demo. Review excludes the author, so each
#: review lands on the other login and each visitor has one to decide.
PARKED_AUTHORS = ("nikhilpravinpise", "VrajVed")


#: The signal-flow scenarios a visitor's "Run a sample incident" picks from.
SAMPLE_SCENARIOS = (
    "ci-test-regression",
    "ci-dependency-failure",
    "ci-docker-build-failure",
    "failed-check",
    "rejected-pr",
    "stale-branch",
)

# Single flight: _sample_lock is held from intake until the background drive
# ends; _start_guard makes a racing second caller wait out the intake so it
# sees the running incident's id.
_sample_lock = threading.Lock()
_start_guard = threading.Lock()
_running_incident: str | None = None


class SeedError(RuntimeError):
    """The demo data could not be seeded."""


def _incident_count(database_url: str) -> int:
    engine = create_engine(database_url)
    try:
        with session_scope(engine) as session:
            return session.scalar(select(func.count()).select_from(models.Incident)) or 0
    finally:
        engine.dispose()


def _park_review(database_url: str, author: str, number: int) -> str:
    """Ingest the change scenario's PR, authored by `author`, and run it to
    `review_pending` (triage, owner, review request) without the scripted
    reviewer deciding - the visitor does that from the dashboard. The PR
    number, branch and sha are made distinct per author: triage escalates a
    second open change on the same service and branch as a duplicate.
    Returns the incident id."""
    scenario = copy.deepcopy(harness.catalog.load_scenarios()[PARKED_SCENARIO])
    event = scenario["events"][0]
    event["actor"] = author
    meta = event["metadata"]
    meta["pr"] = number
    meta["branch"] = f"{meta['branch']}-{author}"
    meta["sha"] = f"{number:04x}" + meta["sha"][4:]
    results, error = harness._ingest_declared_events(scenario, database_url)
    if error:
        raise SeedError(f"{PARKED_SCENARIO}: {error}")
    incident_id = results[-1]["incident_id"]
    env = harness._isolated_env()
    env.update(harness.FIXTURE_RUN_ENV)
    env.update(
        HELIX_ARM="A",
        HELIX_DATABASE_URL=database_url,
        HELIX_FIXTURE_PATH=PARKED_FIXTURE,
        # Blank reads the people and services tables (the live team), not the
        # benchmark cast the harness pins: reviews must name people who can sign in.
        HELIX_SERVICE_REGISTRY="",
        HELIX_GITHUB_WRITES="recorded",
    )
    # Triage and review-request are separate stopping points: two runs.
    for _hop in range(2):
        proc = harness._helix(["run", incident_id], env)
        if proc.returncode not in (0, 1):
            raise SeedError(f"{PARKED_SCENARIO}: helix run failed: {proc.stderr.strip()[:300]}")
    status = harness._current_status(database_url, incident_id)
    if status != IncidentStatus.REVIEW_PENDING.value:
        raise SeedError(f"{PARKED_SCENARIO}: expected review_pending, ended at {status}")
    return incident_id


def _park_reviews(database_url: str) -> list[str]:
    """Park one review per PARKED_AUTHORS entry; returns the incident ids."""
    return [
        _park_review(database_url, author, 101 + index)
        for index, author in enumerate(PARKED_AUTHORS)
    ]


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
    # Seeding runs on every empty boot and must never open a real PR. The
    # harness builds its subprocess env from os.environ, and `helix
    # seed-demo` is its own process, so this reaches every scenario run.
    os.environ["HELIX_GITHUB_WRITES"] = "recorded"
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
    _park_reviews(url)
    return len(DEMO_SCENARIOS) + len(PARKED_AUTHORS)


def _drive_sample(incident_id: str, scenario: dict, database_url: str) -> None:
    """Background body of a sample: the harness's own hop loop, then free
    the slot - whatever happened."""
    global _running_incident
    try:
        env = harness._isolated_env()
        env.update(harness.FIXTURE_RUN_ENV)
        env.update(
            HELIX_ARM="A",
            HELIX_DATABASE_URL=database_url,
            HELIX_FIXTURE_PATH=scenario["evidence_fixture"],
            UV_NO_SYNC="1",
            # A sample behaves like a real incident on the connected tools:
            # Slack, Jira and the PR bot (connection, repo, limit) apply,
            # and it names the live team (the database). Work tracking stays
            # `memory`, which yields to a connected Jira; the server's own
            # .env Slack and Notion tokens stay blanked, so only a connection
            # delivers.
            HELIX_CONNECTIONS_ENABLED="true",
            HELIX_SERVICE_REGISTRY="",
        )
        cfg = harness.HarnessConfig(arms=["A"], mode="fixture", database_url=database_url)
        errors = harness._drive(incident_id, env, cfg, scenario)
        if errors:
            get_logger().warning(
                "sample drive reported errors", incident_id=incident_id, errors=errors
            )
    except Exception as exc:
        get_logger().error("sample drive failed", incident_id=incident_id, error=str(exc))
    finally:
        _running_incident = None
        _sample_lock.release()


def start_sample(session: Session) -> tuple[str, bool]:
    """Raise one random sample incident and drive it in the background.

    Returns (incident_id, already_running). While a sample runs, a second
    call starts nothing and returns the running incident's id with True.
    Intake commits on `session` before the thread starts, so the drive
    always finds the incident."""
    global _running_incident
    with _start_guard:
        if not _sample_lock.acquire(blocking=False):
            return _running_incident or "", True
        try:
            scenario = harness.catalog.load_scenarios()[random.choice(SAMPLE_SCENARIOS)]
            result = intake_signal(session, Signal.model_validate(scenario["signal"]))
            session.commit()
            _running_incident = result.incident_id
            threading.Thread(
                target=_drive_sample,
                args=(result.incident_id, scenario, get_settings().database_url),
                daemon=True,
            ).start()
        except BaseException:
            _running_incident = None
            _sample_lock.release()
            raise
        return result.incident_id, False


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
