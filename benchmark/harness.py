"""Benchmark harness (subphase 1.11): drive scenarios through the real
CLI code paths and collect what actually happened.

Flow per (scenario, arm):

1. Validate the scenario document (`catalog.validate_scenario`) - a
   malformed catalog is a defect, not a run.
2. Check `requires` against live availability - unmet requirements SKIP
   the scenario honestly (no partial credit, no fabricated result).
3. Inject: `inject: live` runs `scripts/<fault_script>`; `inject:
   fixture` points `HELIX_FIXTURE_PATH` at the declared evidence fixture.
4. `helix intake` records the signal through the real intake path
   (dedup, transitions row) - the subprocess is exactly what a CLI
   operator would run.
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

The harness never short-circuits the graph: everything it scores was
produced by real node executions against real durable state.
"""

import argparse
import json
import os
import subprocess
import sys
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from benchmark import catalog
from benchmark.approver import service_suspended_run
from benchmark.scoring import OutcomeRecord, dumps_report, render_markdown, score_outcome, summarize
from helix.config import get_settings
from state import models
from state.db import session_scope
from state.machine import IncidentStatus

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
    max_hops: int = 10  # `helix run` invocations before declaring a stall
    database_url: str | None = None  # None = settings.database_url


# --- Requirement checks ------------------------------------------------------


def _postgres_ok(url: str) -> bool:
    try:
        engine = create_engine(url)
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
        timeout=300,
    )


def _inject_live(scenario: dict[str, Any]) -> str | None:
    """Run the declared fault script; None on success, else the failure
    detail the record carries."""
    script = REPO_ROOT / "scripts" / scenario["fault_script"]
    try:
        proc = subprocess.run(
            ["bash", str(script)], cwd=REPO_ROOT, capture_output=True, text=True, timeout=300
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"fault script could not run: {exc}"
    if proc.returncode != 0:
        return f"fault script exited {proc.returncode}: {proc.stderr.strip()[:300]}"
    return None


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


def _collect(database_url: str, incident_id: str) -> dict[str, Any]:
    """Read back the durable record the run produced."""
    engine = create_engine(database_url)
    with Session(engine) as session:
        incident = session.scalar(
            select(models.Incident).where(models.Incident.incident_id == incident_id)
        )
        if incident is None:
            return {}
        transitions = list(incident.transitions)
        recovered = next((t for t in transitions if t.to_status == IncidentStatus.RECOVERED), None)
        return {
            "incident_id": incident.incident_id,
            "final_status": IncidentStatus(incident.status).value,
            "signal_at": incident.signals[-1].created_at.isoformat() if incident.signals else None,
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
        }


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
                "detail": (hit or {}).get("detail") or "no successful executions row",
            }
        )
        if hit is not None:
            blob = json.dumps(hit.get("output") or {}) + str(hit.get("detail") or "")
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

    # Arm-level requirement: B and E reason through the LLM - without
    # HELIX_LLM_API_KEY they are honestly unrunnable, not failed runs.
    # Arm A's deterministic tables need no key.
    if arm in ("B", "E") and not os.environ.get("HELIX_LLM_API_KEY"):
        record.skipped = f"arm {arm} requires HELIX_LLM_API_KEY"
        return record

    mode = scenario["inject"] if cfg.mode == "auto" else cfg.mode
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
        env["HELIX_FIXTURE_PATH"] = scenario["evidence_fixture"]
    else:
        env["HELIX_EVIDENCE_SOURCE"] = "live"
        fail = _inject_live(scenario)
        if fail:
            record.skipped = f"injection failed: {fail}"
            return record

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
    record.measured = _measure(cfg, scenario, collected)
    return record


def run_benchmark(cfg: HarnessConfig) -> dict[str, Any]:
    """All scenarios x all arms -> scored report dict."""
    scenarios = catalog.load_scenarios()
    if cfg.scenarios:
        scenarios = {k: v for k, v in scenarios.items() if k in set(cfg.scenarios)}
    outcomes: list[Any] = []
    for arm in cfg.arms:
        for name, scenario in scenarios.items():
            record = run_scenario(name, scenario, arm, cfg)
            scored = score_outcome(record, scenario)
            outcomes.append(scored)
            tag = record.skipped or record.final_status or "?"
            print(f"[{arm}] {name}: {tag}", file=sys.stderr)
    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "arms": cfg.arms,
        "mode": cfg.mode,
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
        choices=["A", "B", "E"],
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
    parser.add_argument("--max-hops", type=int, default=10)
    args = parser.parse_args(argv)

    cfg = HarnessConfig(
        arms=args.arm or ["A"],
        scenarios=args.scenario,
        mode=args.mode,
        out_dir=args.out,
        max_hops=args.max_hops,
        database_url=args.database_url,
    )
    report = run_benchmark(cfg)
    json_path, md_path = write_report(report, cfg.out_dir)
    print(f"report: {json_path}", file=sys.stderr)
    print(f"report: {md_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
