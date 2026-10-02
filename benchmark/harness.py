"""Benchmark harness (subphase 1.11): drive scenarios through the real
CLI code paths and collect what actually happened.

Flow per (scenario, arm):

1. Validate the scenario document (`catalog.validate_scenario`) - a
   malformed catalog is a defect, not a run.
2. Check `requires` against live availability - unmet requirements SKIP
   the scenario honestly (no partial credit, no fabricated result).
3. Inject: `inject: live` runs `scripts/<fault_script>`; `inject:
   fixture` points `HELIX_FIXTURE_PATH` at the declared evidence fixture.
4. Intake happens one of two ways, chosen by `HarnessConfig.path`:
   `direct` (default, unchanged Phase 1 behaviour) shells out to `helix
   intake`, exactly what a CLI operator would run. `event` (subphase E7)
   classifies the scenario's signal and ingests it through
   `events.ingest.ingest_event` instead - the same entry point the Kafka
   consumer drives - to measure what the event layer bought against the
   identical scenario set.
5. `helix run` in a hop loop until the incident reaches a terminal
   status, parks (same status twice - arm B's `verifying` end-state or a
   suspended approval that stays undecided), or the hop bound trips.
   `awaiting_approval` hops route through the scripted approver (D2).
6. Collect the durable record (incident, hypothesis, selection,
   executions, approvals, verifications, transitions) and measure the
   expected final state - the recorded write for fixture scenarios,
   kubectl probes for live ones.
7. `scoring.score_outcome` folds the record into metrics; the report
   lands under benchmark/reports/.

`flow: change` scenarios (Phase 2, subphase 2.9/2.11) run differently in
steps 4-6: the declared `events` are ingested in order through
`events.ingest.ingest_event` (never `helix intake`), the hop loop also
handles `review_pending` (scripted reviewer, benchmark/reviewer.py),
`fix_required` (an updated-commit event is injected) and `deploying`
(the declared `deploy_outcome` event is injected), and after the drive
`_settle_integrations` converges the integration projections - the same
drain+reconcile code `helix sync` runs, in-process with one memory
provider seeded from the mirror, because the subprocess-per-hop shape
cannot carry provider state the way one long-lived Helix process does.
The collected record additionally carries triage results, review
requests, work-item/knowledge-page mirrors, the outbox ledger, the
ordered transition list, the duplicate edge and decision-evidence
coverage - the Phase 2 metrics in benchmark/scoring.py read those.

The harness never short-circuits the graph: everything it scores was
produced by real node executions against real durable state.

Cross-arm isolation: a multi-arm invocation (`--arm B --arm C`) runs
every arm against the SAME Postgres with the SAME scenario catalog, so
arm N+1 emits the identical signal arm N just emitted. `state/intake.py`
correctly attaches a repeated signal to any OPEN incident sharing its
fingerprint - that is Phase 1 dedup working as designed, not a bug. But
if arm N left its incident non-terminal (arm B's designed end state is
`verifying`, which is not in `state.machine.TERMINAL_STATUSES`; a stuck
approval or an exhausted hop bound can park any arm the same way), arm
N+1's identical signal silently attaches to arm N's still-open incident
instead of opening its own, and inherits arm N's hypothesis, selected
action and verification data. See `_close_arm_incidents` for the fix and
docs/benchmark/methodology.md for the contamination this was caught from.
"""

import argparse
import json
import os
import subprocess
import sys
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from benchmark import catalog, reviewer
from benchmark.approver import service_suspended_run
from benchmark.scoring import OutcomeRecord, dumps_report, render_markdown, score_outcome, summarize
from events.classify import UnclassifiedSignal, classify
from events.ingest import ingest_event
from events.schemas import EngineeringEvent, EventSource, EventType
from helix.config import get_settings
from state import models
from state.db import session_scope
from state.intake import Signal, build_event, fingerprint
from state.machine import TERMINAL_STATUSES, IncidentStatus, transition

REPO_ROOT = Path(__file__).resolve().parents[1]
REPORTS_DIR = REPO_ROOT / "benchmark" / "reports"

TERMINAL = {
    IncidentStatus.RECOVERED.value,
    IncidentStatus.ESCALATED.value,
    IncidentStatus.FAILED.value,
}

#: Env the `helix run`/`helix intake` subprocesses inherit for fixture
#: benchmark runs - short verification windows keep the run honest but
#: fast; the functional leg is off (headless), and verification records
#: it as `skipped` rather than fabricating a pass.
FIXTURE_RUN_ENV = {
    "HELIX_EVIDENCE_SOURCE": "fixtures",
    "HELIX_VERIFY_WINDOW": "2",
    "HELIX_VERIFY_POLL": "1",
    "HELIX_VERIFY_FUNCTIONAL": "false",
}


@dataclass
class HarnessConfig:
    """One benchmark invocation's bounds."""

    arms: list[str] = field(default_factory=lambda: ["A"])
    scenarios: list[str] | None = None  # None = all
    mode: str = "auto"  # auto | fixture | live
    out_dir: Path = REPORTS_DIR
    # `helix run` invocations before declaring a stall. Change flows are
    # the longest paths: reject -> fix -> re-review -> deploy can take
    # eight hops before the recovery handoff even starts.
    max_hops: int = 14
    database_url: str | None = None  # None = settings.database_url
    path: str = "direct"  # direct | event (subphase E7) - see module docstring


# --- Requirement checks ------------------------------------------------------


def _postgres_ok(url: str) -> bool:
    try:
        # psycopg's default connect wait is the OS TCP timeout (~90s on a
        # dead port) - a requirement probe must fail fast, so postgres URLs
        # get a bound. Other drivers (sqlite) take no connect_timeout.
        kwargs = {"connect_args": {"connect_timeout": 3}} if "postgres" in url else {}
        engine = create_engine(url, **kwargs)
        with engine.connect():
            return True
    except Exception:
        return False


def _kubectl_ok(context: str) -> bool:
    try:
        proc = subprocess.run(
            ["kubectl", "get", "nodes", "--context", context, "-o", "name"],
            capture_output=True,
            timeout=15,
        )
        return proc.returncode == 0 and bool(proc.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        return False


def _http_ok(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status < 500
    except Exception:
        return False


def check_requirements(requires: list[str], database_url: str) -> list[str]:
    """Return the UNMET requirements (empty = all satisfied). Always
    requires the state store - intake/run cannot work without it."""
    settings = get_settings()
    unmet: list[str] = []
    if not _postgres_ok(database_url):
        unmet.append("postgres")
    for req in requires:
        if req == "postgres":
            continue  # already checked
        if req == "none":
            continue
        if req == "kind" and not _kubectl_ok(settings.kube_context):
            unmet.append("kind")
        elif req == "elasticsearch" and not _http_ok(settings.es_url):
            unmet.append("elasticsearch")
        elif req == "jenkins" and not _http_ok(settings.jenkins_url):
            unmet.append("jenkins")
    return unmet


# --- Driving the real CLI -----------------------------------------------------


def _helix(
    args: list[str], env: dict[str, str], stdin_text: str | None = None
) -> subprocess.CompletedProcess:
    """Invoke the real `helix` console script in a subprocess - the same
    process-local entry point an operator uses."""
    return subprocess.run(
        ["uv", "run", "helix", *args],
        cwd=REPO_ROOT,
        env=env,
        input=stdin_text,
        capture_output=True,
        text=True,
        # A single `helix run` may span several verify cycles: settle (up to
        # HELIX_VERIFY_SETTLE) + stability window + the functional leg, times
        # a replan hop or two. 900s covers that with headroom.
        timeout=900,
    )


def _ingest_via_events(
    scenario: dict[str, Any], database_url: str
) -> tuple[str | None, str | None]:
    """Event-path intake (subphase E7): classify the scenario's signal,
    build the canonical envelope, and ingest it through
    `events.ingest.ingest_event` - the same entry point the Kafka consumer
    drives - instead of shelling out to `helix intake`.

    Writes nothing beyond what `ingest_event` itself writes. Earlier this
    also backfilled a `state.models.Signal` row so the graph's intake node
    had something to diagnose from - `ingest_event` never produces one,
    and the graph could not read anything else. That made the comparison
    dishonest: the event path was really "events plus a hand-written
    Signal row", not the event layer standing on its own. `agent/graph.py`
    now falls back to the incident's `EventRecord` envelope
    (`_signal_from_events`) when there is no Signal row, which is the real
    fix and belongs in the graph, not here.

    Returns `(incident_id, skip_reason)` - exactly one of the two is not
    `None`. An unclassifiable signal is an honest skip, matching how
    `events.classify` is documented to behave - never a fabricated event
    type.
    """
    try:
        signal = Signal.model_validate(scenario["signal"])
    except ValidationError as exc:
        return None, f"invalid signal for event path: {exc}"
    try:
        event_type = classify(signal.source.value, signal.signature)
    except UnclassifiedSignal as exc:
        return None, f"unclassifiable signal for event path: {exc}"
    event = build_event(signal, event_type)
    engine = create_engine(database_url)
    with session_scope(engine) as session:
        result = ingest_event(session, event)
    return result.incident_id, None


def _inject_live(scenario: dict[str, Any]) -> str | None:
    """Run the declared fault script; None on success, else the failure
    detail the record carries."""
    script = REPO_ROOT / "scripts" / scenario["fault_script"]
    try:
        proc = subprocess.run(
            ["bash", str(script)], cwd=REPO_ROOT, capture_output=True, text=True, timeout=420
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"fault script could not run: {exc}"
    if proc.returncode != 0:
        return f"fault script exited {proc.returncode}: {proc.stderr.strip()[:300]}"
    return None


# --- Change-flow driving (subphase 2.9/2.11) ---------------------------------


def _ingest_declared_events(
    scenario: dict[str, Any], database_url: str
) -> tuple[list[dict[str, Any]], str | None]:
    """Change-flow intake: ingest every declared `events` item through
    `events.ingest.ingest_event`, in order, each in its own transaction -
    the same entry point the Kafka consumer drives, so the change
    lifecycle is exercised exactly as production drives it.

    `offset_seconds` spaces `occurred_at` deterministically: the timeline
    is anchored so the LAST event lands ~now (older events sit in the
    past), which keeps both the correlation window and the coordinator's
    "newer than status-entry" checks honest.

    Returns `(results, error)` - one entry per event, in declared order.
    """
    specs = scenario.get("events") or []
    if not specs:
        return [], "change flow declares no events"
    max_offset = max(float(spec.get("offset_seconds") or 0) for spec in specs)
    base = datetime.now(UTC) - timedelta(seconds=max_offset)
    engine = create_engine(database_url)
    results: list[dict[str, Any]] = []
    try:
        for spec in specs:
            event = EngineeringEvent(
                event_type=EventType(spec["type"]),
                source=EventSource(spec["source"]),
                timestamp=base + timedelta(seconds=float(spec.get("offset_seconds") or 0)),
                service=spec["service"],
                environment=spec["environment"],
                actor=spec.get("actor") or "system",
                metadata=spec.get("metadata") or {},
            )
            with session_scope(engine) as session:
                result = ingest_event(session, event)
            results.append(
                {
                    "type": spec["type"],
                    "incident_id": result.incident_id,
                    "created": result.created,
                    "correlated": result.correlated,
                }
            )
    except (ValidationError, ValueError, KeyError) as exc:
        return results, f"event ingest failed: {exc}"
    return results, None


def _ingest_followup(event: EngineeringEvent, database_url: str) -> str | None:
    """Ingest one follow-up event the lifecycle is waiting on (an updated
    commit at fix_required, a deploy outcome at deploying). Stamped at
    ingest time, which is strictly after the status was entered - the
    coordinator's `occurred_at > entered_at` check then sees it. The event
    attaches by its correlation tokens, the same way a real vendor event
    arrives. Returns None on success."""
    engine = create_engine(database_url)
    try:
        with session_scope(engine) as session:
            ingest_event(session, event)
    except (ValidationError, ValueError) as exc:
        return f"follow-up event ingest failed: {exc}"
    return None


def _push_update(scenario: dict[str, Any], incident_id: str, database_url: str) -> str | None:
    """The reject-loop follow-up: a `pr.updated` carrying `fixed_sha` /
    `updated_files`, attached to the incident by service+branch tokens -
    the same way a real GitHub webhook would arrive."""
    first = (scenario["events"][0] if scenario.get("events") else {}) or {}
    metadata = dict(first.get("metadata") or {})
    if scenario.get("fixed_sha"):
        metadata["sha"] = scenario["fixed_sha"]
    if scenario.get("updated_files"):
        metadata["files"] = list(scenario["updated_files"])
    metadata.setdefault("action", "synchronize")
    event = EngineeringEvent(
        event_type=EventType.PR_UPDATED,
        source=EventSource(first.get("source") or EventSource.GITHUB.value),
        correlation_id=incident_id,
        service=first.get("service") or "unknown",
        environment=first.get("environment") or "sandbox",
        actor=first.get("actor") or "system",
        metadata=metadata,
    )
    return _ingest_followup(event, database_url)


def _deploy_outcome_event(
    scenario: dict[str, Any], incident_id: str, database_url: str
) -> str | None:
    """At `deploying`, inject the build/deploy verdict the declared
    `deploy_outcome` names - the same `build.passed`/`build.failed`/
    `deployment.succeeded`/`deployment.failed` vocabulary the CI and CD
    adapters emit."""
    outcome = scenario.get("deploy_outcome") or "passed"
    first = (scenario["events"][0] if scenario.get("events") else {}) or {}
    metadata = dict(scenario.get("deploy_metadata") or {})
    metadata.setdefault("deployment", first.get("service") or "unknown")
    event = EngineeringEvent(
        event_type=(
            EventType.DEPLOYMENT_SUCCEEDED if outcome == "passed" else EventType.DEPLOYMENT_FAILED
        ),
        source=EventSource(scenario.get("deploy_source") or EventSource.JENKINS.value),
        correlation_id=incident_id,
        service=first.get("service") or "unknown",
        environment=first.get("environment") or "sandbox",
        metadata=metadata,
    )
    return _ingest_followup(event, database_url)


def _settle_integrations(database_url: str) -> list[str]:
    """Converge the integration projections the lifecycle enqueued.

    `helix run` drains the outbox at the end of every hop, but each hop
    is a subprocess whose memory provider dies with it: dependent ops
    (assign/transition/comment) and reconcile's remote lookups cannot see
    provider state that lived in a dead process. A real deployment runs
    one long-lived process holding ONE provider, so this settle
    reconstructs that view - it seeds a shared `MemoryWorkProvider` from
    the durable `work_items` mirror (the canonical record of what was
    delivered, written in the same transaction as delivery), then runs
    the same drain + reconcile code `helix sync` invokes until no pending
    rows remain. Genuine drift the reconcile finds - a recovered incident
    whose item is still open, a deleted remote - repairs through the real
    2.9 path; that repair is what the stale-work-item scenario measures.

    Only applies to the memory provider: a configured real provider is
    resolved by the drain itself. Errors surface in the record, never
    silently.
    """
    errors: list[str] = []
    from integrations.outbox import drain_outbox
    from integrations.reconcile import reconcile_knowledge_pages, reconcile_work_items
    from integrations.work.memory import MemoryWorkProvider

    try:
        from integrations.notion import get_notion_client

        notion = get_notion_client()  # None when unconfigured - reconcile no-ops
    except ImportError:
        notion = None

    engine = create_engine(database_url)
    try:
        work = MemoryWorkProvider()
        inject = get_settings().work_provider == work.name
        if inject:
            # Seed the provider's remote view from the mirror - the state
            # a single persistent process would have accumulated.
            with session_scope(engine) as session:
                for item in session.scalars(select(models.WorkItem)).all():
                    if item.provider != work.name:
                        continue
                    work.items[item.external_id] = {
                        "incident_id": item.incident.incident_id,
                        "title": item.title,
                        "status": item.status,
                        "assignee": item.assignee,
                        "priority": item.priority,
                        "comments": [],
                        "linked": [],
                    }
        for _pass in range(8):
            with session_scope(engine) as session:
                drain_outbox(session, work=work if inject else None)
                if inject:
                    reconcile_work_items(session, work)
                if notion is not None:
                    reconcile_knowledge_pages(session, notion)
                pending = session.scalar(
                    select(models.IntegrationOutbox.id).where(
                        models.IntegrationOutbox.status == models.OutboxStatus.PENDING
                    )
                )
            if pending is None:
                break
    except Exception as exc:  # a settle defect is a harness error, not a result
        errors.append(f"integration settle failed: {exc}")
    return errors


def _drive_change(
    incident_id: str, env: dict[str, str], cfg: HarnessConfig, scenario: dict
) -> list[str]:
    """`helix run` hop loop for `flow: change`, parallel to `_drive`.

    The change lifecycle parks at statuses the Ops loop never visits:
    `review_pending` (scripted reviewer via state.reviews), `fix_required`
    (an updated commit is injected), `deploying` (a build/deploy outcome
    event is injected). `awaiting_approval` still routes through the
    scripted approver - a deploy-failure handoff enters the Phase 1
    recovery loop unchanged. Returns harness error notes (empty = clean).
    """
    errors: list[str] = []
    seen: list[str] = []
    url = cfg.database_url or get_settings().database_url
    review_rounds = 0
    injected: set[str] = set()
    max_rounds = int(scenario.get("max_review_rounds") or 1)
    for _hop in range(cfg.max_hops):
        proc = _helix(["run", incident_id], env)
        if proc.returncode not in (0, 1):
            errors.append(f"helix run failed: {proc.stderr.strip()[:300]}")
            break
        status = _current_status(url, incident_id)
        if status in TERMINAL:
            break
        acted = False
        if status == IncidentStatus.AWAITING_APPROVAL.value:
            outcome = service_suspended_run(_session_factory(cfg), incident_id, scenario)
            if outcome.outcome == "no_pending":
                errors.append("awaiting_approval with no pending approvals row")
                break
            if outcome.outcome == "error":
                errors.append(f"approver error: {outcome.detail}")
                break
            acted = True
        elif status == IncidentStatus.REVIEW_PENDING.value:
            decision = reviewer.decision_for_round(scenario, review_rounds)
            if decision == "timeout":
                break  # the scripted reviewer never answers - honestly parked
            if review_rounds >= max_rounds:
                errors.append(
                    f"review round {review_rounds + 1} exceeds max_review_rounds={max_rounds}"
                )
                break
            outcome = reviewer.service_suspended_review(
                _session_factory(cfg), incident_id, scenario, round_index=review_rounds
            )
            review_rounds += 1
            if outcome.outcome == "no_pending":
                errors.append("review_pending with no pending review_requests row")
                break
            if outcome.outcome == "error":
                errors.append(f"reviewer error: {outcome.detail}")
                break
            acted = True
        elif status == IncidentStatus.FIX_REQUIRED.value and "fix" not in injected:
            injected.add("fix")
            fail = _push_update(scenario, incident_id, url)
            if fail:
                errors.append(fail)
                break
            acted = True
        elif status == IncidentStatus.DEPLOYING.value and "deploy" not in injected:
            injected.add("deploy")
            if not scenario.get("deploy_outcome"):
                break  # no declared outcome - the incident honestly waits for one
            fail = _deploy_outcome_event(scenario, incident_id, url)
            if fail:
                errors.append(fail)
                break
            acted = True
        if seen and seen[-1] == status and not acted:
            break  # parked - same status twice with nothing new injected
        seen.append(status)
    else:
        errors.append(f"hop bound {cfg.max_hops} exhausted")
    return errors


def _drive(incident_id: str, env: dict[str, str], cfg: HarnessConfig, scenario: dict) -> list[str]:
    """`helix run` hop loop with the scripted approver. Returns harness
    error notes (empty = clean drive)."""
    errors: list[str] = []
    seen: list[str] = []
    for _hop in range(cfg.max_hops):
        proc = _helix(["run", incident_id], env)
        if proc.returncode not in (0, 1):
            errors.append(f"helix run failed: {proc.stderr.strip()[:300]}")
            break
        status = _current_status(cfg.database_url or get_settings().database_url, incident_id)
        if status in TERMINAL:
            break
        if status == IncidentStatus.AWAITING_APPROVAL.value:
            outcome = service_suspended_run(_session_factory(cfg), incident_id, scenario)
            if outcome.outcome == "no_pending":
                errors.append("awaiting_approval with no pending approvals row")
                break
            if outcome.outcome == "error":
                errors.append(f"approver error: {outcome.detail}")
                break
        if seen and seen[-1] == status:
            break  # parked (arm B at verifying, or a stuck suspension)
        seen.append(status)
    else:
        errors.append(f"hop bound {cfg.max_hops} exhausted")
    return errors


def _session_factory(cfg: HarnessConfig):
    url = cfg.database_url or get_settings().database_url
    engine = create_engine(url)
    return lambda: session_scope(engine)


def _close_preexisting_incidents(database_url: str, scenarios: dict[str, Any]) -> int:
    """Close any still-open incident a scenario in this run would dedupe
    onto, before the run starts. Returns how many were closed.

    `_close_arm_incidents` isolates arms from each other *within* one
    invocation. This closes the other half of the same hole: an incident
    left open by an *earlier* invocation. Intake dedup matches on
    `source | service | environment | signature`, which is identical
    across invocations, so a leftover attaches silently.

    That is not hypothetical. An arm-B smoke test left an incident parked
    at `verifying`; the next full ablation's arm A deduped onto it,
    inherited arm B's hypothesis and candidate, drove it to `recovered`
    and reported the result as arm A's. The candidate row still read
    `components={"arm": "B"}`. Arm A scored 0.83 instead of 1.00 on
    reasoning it never did, and nothing in the report said so - the
    incident ids were all distinct, because the contamination arrived
    before the run rather than during it.

    Scoped to fingerprints this run will actually produce, so it cannot
    touch unrelated incidents, and it closes through the ordinary
    validated transition rather than deleting anything.
    """
    fingerprints: set[str] = set()
    for scenario in scenarios.values():
        try:
            fingerprints.add(fingerprint(Signal.model_validate(scenario["signal"])))
        except (ValidationError, KeyError):
            continue  # a malformed scenario is caught by catalog validation
    if not fingerprints:
        return 0

    closed = 0
    engine = create_engine(database_url)
    with Session(engine) as session:
        stale = session.scalars(
            select(models.Incident)
            .where(models.Incident.fingerprint.in_(sorted(fingerprints)))
            .where(models.Incident.status.not_in(TERMINAL_STATUSES))
        ).all()
        for incident in stale:
            transition(
                incident,
                IncidentStatus.ESCALATED,
                cause="benchmark: closing an incident left open by an earlier run",
                session=session,
            )
            closed += 1
        session.commit()
    return closed


def _close_arm_incidents(database_url: str, incident_ids: set[str], arm: str) -> None:
    """Force every incident `arm` touched into a terminal status, so the
    next arm in this invocation cannot attach to it (see module docstring).

    Why this shape, over the alternative: the other option considered was
    scoping the harness's signal fingerprint per arm (e.g. folding `arm`
    into `service` before intake), which avoids the collision without
    mutating any status at all. It was rejected because `agent/graph.py`
    reads `incident.service` straight into the state the LLM reasons over
    (the `"service": incident.service` entry built in the intake node) -
    arm-scoping it would mean arms B/C/E diagnose against a fabricated
    service name instead of the real one, which is a second, subtler form
    of exactly the contamination this function exists to remove. Closing
    the incident instead touches no field the graph ever reads.

    Every status change goes through `state.machine.transition` - the same
    validated, event-emitting path every other transition in Helix uses.
    `state/intake.py`'s dedup rule (attach-to-open-incident-by-fingerprint)
    is never touched or special-cased; this only ever changes which
    incidents are OPEN when the next arm's intake runs.

    Tradeoff accepted, stated plainly: ESCALATED is not literally true of
    every incident this closes - an arm-A run that actually recovered
    would, if closed, be relabelled ESCALATED in the `incidents` table.
    (In practice arm A's own drive loop already ends at a terminal status
    for a scenario it completes, so this is a no-op for it; the case that
    matters is arm B's designed `verifying` end-state and any arm that
    parks on a stuck approval or an exhausted hop bound.) No row is
    deleted, the transition's `cause` records exactly why the change
    happened, and the true observed final status is already captured in
    the benchmark report (`OutcomeRecord.final_status`) before this runs -
    the report never reads it back off the mutated incident. This also
    only ever fires BETWEEN arms of one multi-arm invocation (see the
    `arm_index` guard in `run_benchmark`): a single-arm run never calls
    this, so single-arm behaviour is unchanged. It does not, and is not
    meant to, clean up incidents left open by an earlier SEPARATE
    benchmark invocation - an operator wanting a clean slate across full
    benchmark runs still needs a fresh database or a manual truncate.
    """
    engine = create_engine(database_url)
    with session_scope(engine) as session:
        for incident_id in incident_ids:
            incident = session.scalar(
                select(models.Incident).where(models.Incident.incident_id == incident_id)
            )
            if incident is None or IncidentStatus(incident.status) in TERMINAL_STATUSES:
                continue  # already terminal (or gone) - nothing to isolate
            transition(
                incident,
                IncidentStatus.ESCALATED,
                cause=(
                    f"benchmark isolation: force-closed after arm {arm} finished so a "
                    "later arm's identical signal cannot attach to this incident "
                    "(benchmark/harness.py:_close_arm_incidents)"
                ),
                session=session,
            )


def _current_status(database_url: str, incident_id: str) -> str | None:
    try:
        engine = create_engine(database_url)
        with Session(engine) as session:
            return session.scalar(
                select(models.Incident.status).where(models.Incident.incident_id == incident_id)
            )
    except Exception:
        return None


# --- Collection + measurement -------------------------------------------------


def _first_observed_at(incident: models.Incident) -> str | None:
    """When the failure was first observed, for the MTTR clock.

    The direct path records a `signals` row and this is its timestamp. The
    event path (E7) deliberately writes no signal row - the graph reads
    the canonical event instead (D22) - so reading only `signals` made
    `signal_at` None on every event-path run, and MTTR came back null
    while the comparison still reported NO REGRESSION, because MTTR is not
    one of its gated metrics. A metric silently becoming unmeasurable is
    exactly the kind of hole that verdict is supposed to catch.

    Helix's own lifecycle events are excluded: they are stamped when Helix
    reacted, so starting the clock at one would measure from Helix's
    response rather than from the failure, flattering MTTR.
    """
    if incident.signals:
        return incident.signals[-1].created_at.isoformat()
    observed = [
        record.occurred_at
        for record in incident.event_records
        if record.source != EventSource.HELIX.value
    ]
    return min(observed).isoformat() if observed else None


def _collect(database_url: str, incident_id: str) -> dict[str, Any]:
    """Read back the durable record the run produced.

    Phase 2 (2.9/2.11) adds the change-lifecycle record: triage results,
    review requests, work-item/knowledge-page mirrors, the outbox ledger,
    the ordered transition list, the owner, the duplicate edge, and the
    evidence-ref coverage of the decision rows. Everything here is read
    from the canonical store - never inferred."""
    engine = create_engine(database_url)
    with Session(engine) as session:
        incident = session.scalar(
            select(models.Incident).where(models.Incident.incident_id == incident_id)
        )
        if incident is None:
            return {}
        transitions = list(incident.transitions)
        recovered = next((t for t in transitions if t.to_status == IncidentStatus.RECOVERED), None)
        triage = incident.triage_results[-1] if incident.triage_results else None
        candidates = list(incident.candidates)
        hypotheses = list(incident.hypotheses)
        return {
            "incident_id": incident.incident_id,
            "final_status": IncidentStatus(incident.status).value,
            "signal_at": _first_observed_at(incident),
            "recovered_at": recovered.created_at.isoformat() if recovered else None,
            "last_event_at": transitions[-1].created_at.isoformat() if transitions else None,
            "hypothesis_claim": (incident.hypotheses[-1].claim if incident.hypotheses else None),
            "selected_action": (
                next(
                    (c.action for c in reversed(incident.candidates) if c.selected),
                    None,
                )
            ),
            "executions": [
                {
                    "action": e.action,
                    "result": e.result.value,
                    "policy_decision": e.policy_decision,
                    "approval_id": e.approval_id,
                    "decided_by": e.decided_by,
                    "compensates_id": e.compensates_id,
                    "origin": e.origin,
                    "output": e.output,
                }
                for e in incident.executions
            ],
            "approvals": [
                {"action": a.action, "status": a.status.value, "decided_by": a.decided_by}
                for a in incident.approvals
            ],
            "verifications": [
                {"action": v.action, "outcome": v.outcome.value} for v in incident.verifications
            ],
            # Phase 2 (2.9/2.11): the change-lifecycle durable record.
            "incident_kind": incident.kind.value if incident.kind else None,
            "owner": incident.owner,
            "triage": (
                {
                    "affected_service": triage.affected_service,
                    "owner": triage.owner,
                    "owner_source": triage.owner_source,
                    "duplicate_of_id": triage.duplicate_of_id,
                }
                if triage
                else None
            ),
            "reviews": [
                {
                    "reviewer": r.reviewer,
                    "status": r.status.value,
                    "decided_by": r.decided_by,
                    "attempt": r.attempt,
                }
                for r in incident.review_requests
            ],
            "work_items": [
                {
                    "provider": w.provider,
                    "external_id": w.external_id,
                    "status": w.status,
                    "assignee": w.assignee,
                }
                for w in incident.work_items
            ],
            "knowledge_page": (
                {
                    "external_id": incident.knowledge_page.external_id,
                    "external_url": incident.knowledge_page.external_url,
                    "title": incident.knowledge_page.title,
                }
                if incident.knowledge_page
                else None
            ),
            "outbox": {
                status: sum(1 for o in incident.outbox_entries if o.status.value == status)
                for status in {o.status.value for o in incident.outbox_entries}
            },
            "outbox_ops": [
                {"integration": o.integration, "op": o.op, "status": o.status.value}
                for o in incident.outbox_entries
            ],
            "transitions": [
                {
                    "from": t.from_status.value if t.from_status else None,
                    "to": t.to_status.value,
                }
                for t in transitions
            ],
            "duplicate_of": (
                incident.duplicate_of.incident_id if incident.duplicate_of_id else None
            ),
            "decision_evidence": {
                "candidates_with_refs": sum(1 for c in candidates if c.evidence_refs),
                "candidates_total": len(candidates),
                "hypotheses_with_refs": sum(1 for h in hypotheses if h.evidence_refs),
                "hypotheses_total": len(hypotheses),
            },
        }


def _execution_detail(hit: dict[str, Any] | None) -> str:
    """Evidence line for the executed-action check.

    `hit` is an executions row as `_collect` builds it, and that row has
    no `detail` key - the Execution model has no such column. Reading one
    off it meant the not-found string was printed for every outcome,
    passing or failing, so each `ok: true` in the committed report read
    as though the check had passed on no evidence at all. Describe the
    row from the columns it does carry instead.
    """
    if hit is None:
        return "no successful executions row"
    detail = (
        f"{hit['action']} result={hit['result']} origin={hit['origin']} "
        f"policy={hit['policy_decision']}"
    )
    if hit.get("approval_id") is not None:
        detail += f" approval_id={hit['approval_id']}"
    return detail


def _measure(cfg: HarnessConfig, scenario: dict[str, Any], record: dict[str, Any]) -> list[dict]:
    """The harness's independent expected-final-state measurement:
    recorded-write assertions for fixture scenarios, kubectl probes for
    live ones."""
    checks: list[dict] = []
    expected = scenario.get("expected_final_state") or {}
    want = expected.get("executed_action")
    if want:
        hit = next(
            (
                e
                for e in record.get("executions", [])
                if e["action"] == want and e["result"] == "success" and e["compensates_id"] is None
            ),
            None,
        )
        checks.append(
            {
                "check": f"executed_action={want}",
                "ok": hit is not None,
                "detail": _execution_detail(hit),
            }
        )
        if hit is not None:
            # The recorded intended write is what fixture mode scores
            # (D10). The concatenated `detail` this used to carry was the
            # same phantom key `_execution_detail` documents, so it only
            # ever appended an empty string.
            blob = json.dumps(hit.get("output") or {})
            for needle in expected.get("output_contains") or []:
                checks.append(
                    {
                        "check": f"output_contains {needle!r}",
                        "ok": needle in blob,
                        "detail": blob[:160],
                    }
                )
    if scenario.get("inject") == "live":
        for probe in expected.get("measured") or []:
            checks.append(_kubectl_probe(probe))
    checks.extend(_measure_change(scenario, record))
    return checks


def _measure_change(scenario: dict[str, Any], record: dict[str, Any]) -> list[dict]:
    """The change-flow assertions: the durable record the 2.x lifecycle
    left behind. Each declared expectation becomes a measured check the
    same way `executed_action` does for Phase 1 - a scenario that does not
    declare one simply is not measured on it."""
    checks: list[dict] = []
    if "expected_duplicate" in scenario:
        want = bool(scenario["expected_duplicate"])
        got = record.get("duplicate_of") is not None
        checks.append(
            {
                "check": f"duplicate_of set == {want}",
                "ok": got == want,
                "detail": f"duplicate_of={record.get('duplicate_of')!r}",
            }
        )
    if "expected_work_items" in scenario:
        want = int(scenario["expected_work_items"])
        got = len(record.get("work_items") or [])
        items = [
            (w["provider"], w["external_id"], w["status"]) for w in record.get("work_items") or []
        ]
        checks.append(
            {
                "check": f"work_items == {want}",
                "ok": got == want,
                "detail": f"{got} work_items row(s): {items}",
            }
        )
    if "expected_work_item_status" in scenario:
        want = scenario["expected_work_item_status"]
        statuses = [w["status"] for w in record.get("work_items") or []]
        checks.append(
            {
                "check": f"work_item_status == {want!r}",
                "ok": bool(statuses) and all(s == want for s in statuses),
                "detail": f"statuses={statuses}",
            }
        )
    if "expected_knowledge_page" in scenario:
        want = bool(scenario["expected_knowledge_page"])
        # The deterministic half of the projection is the committed
        # outbox op: whether Notion delivered it depends on credentials
        # the benchmark env may not have, and `outbox` in the record
        # reports that truth separately. A mirror row or a sync_page op
        # both count as "the lifecycle projected a page".
        got = record.get("knowledge_page") is not None or any(
            o.get("integration") == "notion" and o.get("op") == "sync_page"
            for o in record.get("outbox_ops") or []
        )
        checks.append(
            {
                "check": f"knowledge_page projected == {want}",
                "ok": got == want,
                "detail": (
                    f"page={record.get('knowledge_page')!r} outbox={record.get('outbox')!r}"
                ),
            }
        )
    return checks


def _kubectl_probe(probe: dict[str, Any]) -> dict[str, Any]:
    """One live measured check: deployment_ready runs `rollout status`,
    jsonpath_equals reads the field and compares."""
    ns = probe.get("namespace", "payment-api")
    ctx = get_settings().kube_context
    kind = probe.get("kind")
    if kind == "deployment_ready":
        proc = subprocess.run(
            [
                "kubectl",
                "--context",
                ctx,
                "-n",
                ns,
                "rollout",
                "status",
                f"deployment/{probe['deployment']}",
                "--timeout=15s",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return {
            "check": f"deployment_ready {ns}/{probe['deployment']}",
            "ok": proc.returncode == 0,
            "detail": (proc.stdout + proc.stderr).strip()[:200],
        }
    if kind == "jsonpath_equals":
        proc = subprocess.run(
            [
                "kubectl",
                "--context",
                ctx,
                "-n",
                ns,
                "get",
                probe["resource"],
                "-o",
                f"jsonpath={probe['jsonpath']}",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        got = proc.stdout.strip()
        return {
            "check": f"{probe['resource']} {probe['jsonpath']} == {probe['equals']}",
            "ok": proc.returncode == 0 and got == str(probe["equals"]),
            "detail": got[:200] or proc.stderr.strip()[:200],
        }
    return {"check": f"unknown probe {kind!r}", "ok": False, "detail": ""}


# --- Scenario runner ------------------------------------------------------------


def run_scenario(
    scenario_name: str, scenario: dict[str, Any], arm: str, cfg: HarnessConfig
) -> OutcomeRecord:
    """One (scenario, arm) attempt end to end, or an honestly-recorded
    skip/error record."""
    record = OutcomeRecord(scenario=scenario_name, arm=arm)

    catalog_errors = catalog.validate_scenario(scenario_name, scenario)
    if catalog_errors:
        record.skipped = "catalog invalid"
        record.errors = catalog_errors
        return record

    # Arm-level requirement: B, C, D and E reason through the LLM -
    # without a key they are honestly unrunnable, not failed runs. Arm
    # A's deterministic tables need no key.
    #
    # Read through Settings, not os.environ. The key's documented home is
    # .env (.env.example, docs/bring-up.md), which only pydantic-settings
    # loads - os.environ never sees it. Gating on os.environ meant putting
    # the key exactly where every document says to put it left all three
    # LLM arms skipping with a message naming the variable that was, as
    # far as the user could tell, already set.
    if arm in ("B", "C", "D", "E") and not get_settings().llm_api_key:
        record.skipped = f"arm {arm} requires HELIX_LLM_API_KEY (set it in .env or the environment)"
        return record

    flow = scenario.get("flow") or "signal"
    if flow == "change":
        # Change scenarios ingest canonical events; `inject` is a Phase 1
        # concern. Fixture mode is still selectable via evidence_fixture -
        # a failed deploy hands off to the recovery loop, which needs an
        # evidence substrate.
        mode = (
            cfg.mode
            if cfg.mode != "auto"
            else ("fixture" if scenario.get("evidence_fixture") else "live")
        )
    else:
        mode = scenario["inject"] if cfg.mode == "auto" else cfg.mode

    # `--mode fixture` forces every scenario onto recorded evidence, but
    # `evidence_fixture` is not a required catalogue field and the five
    # live Kubernetes scenarios do not declare one - they inject a real
    # fault instead. Reading it unconditionally crashed the whole run with
    # a KeyError, and only when the cluster happened to be up: with kind
    # down these scenarios skip on unmet requirements first and never
    # reach it. Checked before requirements so the outcome does not depend
    # on what infrastructure is running.
    if mode == "fixture" and not scenario.get("evidence_fixture"):
        record.skipped = "no evidence_fixture declared, cannot run in fixture mode"
        return record

    database_url = cfg.database_url or get_settings().database_url
    unmet = check_requirements(scenario.get("requires") or [], database_url)
    if unmet:
        record.skipped = f"unmet requirements: {', '.join(unmet)}"
        return record

    env = dict(os.environ)
    env["HELIX_ARM"] = arm
    env["HELIX_DATABASE_URL"] = database_url
    if mode == "fixture":
        env.update(FIXTURE_RUN_ENV)
        env["HELIX_FIXTURE_PATH"] = scenario["evidence_fixture"]  # guarded above
    else:
        env["HELIX_EVIDENCE_SOURCE"] = "live"
        if flow != "change":
            fail = _inject_live(scenario)
            if fail:
                record.skipped = f"injection failed: {fail}"
                return record

    if flow == "change":
        results, error = _ingest_declared_events(scenario, database_url)
        if error:
            record.skipped = error
            return record
        # The incident under test is the LAST declared event's - earlier
        # events may have opened other incidents (the duplicate scenario)
        # that stay open on purpose.
        incident_id = results[-1]["incident_id"]
        record.incident_id = incident_id
        record.extra_incident_ids = [
            r["incident_id"] for r in results[:-1] if r["incident_id"] != incident_id
        ]
        record.errors.extend(_drive_change(incident_id, env, cfg, scenario))
        record.errors.extend(_settle_integrations(database_url))
    elif cfg.path == "event":
        incident_id, skip = _ingest_via_events(scenario, database_url)
        if skip:
            record.skipped = skip
            return record
        record.incident_id = incident_id
        record.errors.extend(_drive(incident_id, env, cfg, scenario))
    else:
        # `helix intake` reads stdin when --file is omitted - the operator path.
        intake = _helix(["intake"], env, stdin_text=json.dumps(scenario["signal"]))
        if intake.returncode != 0:
            record.skipped = f"intake failed: {intake.stderr.strip()[:300]}"
            return record
        incident_id = intake.stdout.strip().splitlines()[-1]
        record.incident_id = incident_id
        record.errors.extend(_drive(incident_id, env, cfg, scenario))

    collected = _collect(database_url, incident_id)
    record.final_status = collected.get("final_status")
    record.hypothesis_claim = collected.get("hypothesis_claim")
    record.selected_action = collected.get("selected_action")
    record.executions = collected.get("executions", [])
    record.approvals = collected.get("approvals", [])
    record.verifications = collected.get("verifications", [])
    record.signal_at = collected.get("signal_at")
    record.recovered_at = collected.get("recovered_at")
    record.last_event_at = collected.get("last_event_at")
    # Phase 2 (2.9/2.11): the change-lifecycle record.
    record.incident_kind = collected.get("incident_kind")
    record.owner = collected.get("owner")
    record.triage = collected.get("triage")
    record.reviews = collected.get("reviews", [])
    record.work_items = collected.get("work_items", [])
    record.knowledge_page = collected.get("knowledge_page")
    record.outbox = collected.get("outbox", {})
    record.outbox_ops = collected.get("outbox_ops", [])
    record.transitions = collected.get("transitions", [])
    record.duplicate_of = collected.get("duplicate_of")
    record.decision_evidence = collected.get("decision_evidence", {})
    record.measured = _measure(cfg, scenario, collected)
    return record


def run_benchmark(cfg: HarnessConfig) -> dict[str, Any]:
    """All scenarios x all arms -> scored report dict."""
    scenarios = catalog.load_scenarios()
    if cfg.scenarios:
        scenarios = {k: v for k, v in scenarios.items() if k in set(cfg.scenarios)}
    database_url = cfg.database_url or get_settings().database_url
    # Before anything runs: close incidents a previous invocation left
    # open that these scenarios would dedupe onto. See
    # _close_preexisting_incidents for the contamination this prevents.
    stale = _close_preexisting_incidents(database_url, scenarios)
    if stale:
        print(f"closed {stale} incident(s) left open by an earlier run", file=sys.stderr)
    outcomes: list[Any] = []
    for arm_index, arm in enumerate(cfg.arms):
        touched: set[str] = set()
        for name, scenario in scenarios.items():
            record = run_scenario(name, scenario, arm, cfg)
            if record.incident_id:
                touched.add(record.incident_id)
            # Change flows can open incidents besides the driven one (the
            # duplicate scenario's original) - they must close too or the
            # next arm's identical events correlate onto them.
            touched.update(record.extra_incident_ids)
            scored = score_outcome(record, scenario)
            outcomes.append(scored)
            tag = record.skipped or record.final_status or "?"
            print(f"[{arm}] {name}: {tag}", file=sys.stderr)
        # Cross-arm isolation (see module docstring + _close_arm_incidents):
        # only runs when another arm is still queued behind this one, so a
        # single-arm invocation never calls this and behaves exactly as
        # before.
        if arm_index < len(cfg.arms) - 1 and touched:
            _close_arm_incidents(database_url, touched, arm)
    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "arms": cfg.arms,
        "mode": cfg.mode,
        "path": cfg.path,
        "outcomes": [o.to_dict() for o in outcomes],
        "summary": summarize(outcomes),
    }
    return report


def write_report(report: dict[str, Any], out_dir: Path) -> tuple[Path, Path]:
    """Timestamped artifacts + the `latest.*` pointers (committed - the
    current benchmark evidence lives in-tree; intermediates are ignored)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    arms = "-".join(report["arms"]).lower()
    json_path = out_dir / f"benchmark-{stamp}-{arms}.json"
    md_path = out_dir / f"benchmark-{stamp}-{arms}.md"
    json_path.write_text(dumps_report(report), encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    (out_dir / "latest.json").write_text(dumps_report(report), encoding="utf-8")
    (out_dir / "latest.md").write_text(render_markdown(report), encoding="utf-8")
    return json_path, md_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="helix-benchmark",
        description="Controlled benchmark harness (subphase 1.11): run scenarios "
        "through the real CLI and score the durable record.",
    )
    parser.add_argument(
        "--arm",
        action="append",
        choices=["A", "B", "C", "D", "E"],
        help="ablation arm(s) to run; repeatable (default: A)",
    )
    parser.add_argument(
        "--scenario",
        action="append",
        help="scenario name(s) to run; repeatable (default: all)",
    )
    parser.add_argument(
        "--mode",
        choices=["auto", "fixture", "live"],
        default="auto",
        help="auto honors each scenario's inject field (default)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=REPORTS_DIR,
        help="report output directory (default benchmark/reports/)",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="state DB URL (default HELIX_DATABASE_URL/settings)",
    )
    parser.add_argument("--max-hops", type=int, default=14)
    parser.add_argument(
        "--path",
        choices=["direct", "event"],
        default="direct",
        help="intake path: direct shells out to `helix intake` (default, "
        "unchanged Phase 1 behaviour); event ingests through "
        "events.ingest.ingest_event (subphase E7)",
    )
    args = parser.parse_args(argv)

    cfg = HarnessConfig(
        arms=args.arm or ["A"],
        scenarios=args.scenario,
        mode=args.mode,
        out_dir=args.out,
        max_hops=args.max_hops,
        database_url=args.database_url,
        path=args.path,
    )
    report = run_benchmark(cfg)
    json_path, md_path = write_report(report, cfg.out_dir)
    print(f"report: {json_path}", file=sys.stderr)
    print(f"report: {md_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
