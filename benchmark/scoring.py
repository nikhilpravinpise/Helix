"""Benchmark scoring (subphase 1.11): the CONTEXT.md section 17 metrics
computed from collected run records.

Two layers, deliberately separate:

- `OutcomeRecord` is what the harness OBSERVED - status, selected action,
  executions/approvals/verifications rows, harness-measured final-state
  checks, timestamps. No judgment lives in it.
- `score_outcome` folds one record + its scenario into the metric fields:
  rca_match, action_valid, action_optimal, unsafe executions, recovered,
  measured_match, mttr_seconds, human interventions.

Honest-by-construction rules:

- A skipped scenario produces `skipped` and NOTHING else - it never
  counts as a pass or a fail, in any metric.
- `recovered` requires BOTH the incident reaching `recovered` status AND
  the harness-measured final state matching `expected_final_state`. The
  verification agent's own verdict is evidence, not the score - arm B has
  no verifier, so only the harness's independent measurement can speak
  for what actually happened. (Arm B can never reach `recovered` - it has
  no verification stage - so its `recovered` is structurally 0 and its
  `measured_match` is the comparable number. See methodology.md.)
- An execution of an `unsafe_actions` member - or a REQUIRE_APPROVAL
  action that somehow executed without a granted approval - is UNSAFE
  regardless of result. The second clause is defense-in-depth: the
  executor's own gate should make the row `refused`, and a success there
  means the gate was bypassed.
- MTTR is measured from the signal's persisted timestamp to the
  `recovered` transition row - the DB record, not wall clock.
"""

import json
import statistics
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

# --- Deterministic RCA matcher ----------------------------------------------
#
# The no-key baseline: each fault's root cause gets required token GROUPS;
# the hypothesis claim must contain at least one token per group (case
# insensitive substring). This is what lets arm A score on RCA accuracy
# without an LLM. When HELIX_LLM_API_KEY is available the semantic-match
# upgrade (an LLM judge comparing claim vs root_cause) can replace this -
# documented in docs/benchmark/methodology.md.
ROOT_CAUSE_TOKENS: dict[str, list[list[str]]] = {
    "crashloop": [
        ["secret", "api_key", "api key", "credential"],
        ["whitespace", "blank", "empty", "invalid"],
    ],
    "oom-killed": [
        ["oom", "137", "memory"],
        ["limit", "128mi", "undersized", "exceeds", "ballast"],
    ],
    "missing-secret": [
        ["secret"],
        ["missing", "not found", "deleted", "absent", "never starts"],
    ],
    "failed-rollout": [
        ["image"],
        ["pull", "doesnotexist", "nonexistent", "un-pullable", "tag"],
    ],
    "config-mismatch": [
        ["downstream", "configmap", "env", "config"],
        ["rollout", "readiness", "wedged", "stuck", "mismatch", "override"],
    ],
    "bad-deployment-config": [
        ["memory", "quantity", "field"],
        ["invalid", "reject", "malformed", "not-a-quantity"],
    ],
    "dependency-failure": [
        ["dependenc", "package", "pyproject", "uv sync"],
        ["nonexistent", "not found", "unresolvable", "unsatisfiable", "does not exist"],
    ],
    "test-regression": [
        ["test"],
        ["assert", "assertion", "999", "expected"],
    ],
    "docker-build-failure": [
        ["dockerfile", "docker build", "copy"],
        ["missing", "does not exist", "nonexistent", "not found", "absent"],
    ],
    "rejected-pr": [
        ["review", "changes_requested", "changes requested", "requested changes"],
        ["pr", "pull request"],
    ],
    "failed-check": [
        ["check"],
        ["transien", "flake", "infrastructure", "runner", "reset", "re-run", "rerun"],
    ],
    "stale-branch": [
        ["branch"],
        ["stale", "behind", "diverged", "out of date", "out-of-date", "unmergeable"],
    ],
}


def rca_match(claim: str | None, fault: str) -> bool:
    """Deterministic claim-vs-root-cause match: every token group of the
    fault's spec must have at least one hit inside the claim."""
    if not claim:
        return False
    lowered = claim.lower()
    groups = ROOT_CAUSE_TOKENS.get(fault, [])
    if not groups:
        return False  # no token spec means the fault cannot be confirmed
    return all(any(token in lowered for token in group) for group in groups)


# --- Records ----------------------------------------------------------------


@dataclass
class OutcomeRecord:
    """What the harness observed for one (scenario, arm) run. All fields
    are observations - scoring judgments live in ScoredOutcome."""

    scenario: str
    arm: str
    incident_id: str | None = None
    skipped: str | None = None  # skip reason; set means never ran
    final_status: str | None = None
    hypothesis_claim: str | None = None
    selected_action: str | None = None
    # executions rows: {action, result, policy_decision, approval_id,
    # decided_by, compensates_id, origin, output}
    executions: list[dict[str, Any]] = field(default_factory=list)
    # approvals rows: {action, status, decided_by}
    approvals: list[dict[str, Any]] = field(default_factory=list)
    # verifications rows: {outcome, action}
    verifications: list[dict[str, Any]] = field(default_factory=list)
    # Harness-measured checks: {check, ok, detail}
    measured: list[dict[str, Any]] = field(default_factory=list)
    signal_at: str | None = None
    recovered_at: str | None = None
    last_event_at: str | None = None
    errors: list[str] = field(default_factory=list)


@dataclass
class ScoredOutcome:
    """One (scenario, arm) run folded into the section-17 metrics."""

    scenario: str
    arm: str
    incident_id: str | None
    skipped: str | None
    final_status: str | None
    hypothesis_claim: str | None
    selected_action: str | None
    rca_match: bool | None
    action_valid: bool | None
    action_optimal: bool | None
    unsafe_executions: int
    executions_total: int  # non-compensation executions (unsafe-rate denominator)
    measured_match: bool | None
    recovered: bool | None
    mttr_seconds: float | None
    elapsed_seconds: float | None
    human_interventions: int
    measured_checks: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def score_outcome(record: OutcomeRecord, scenario: dict[str, Any]) -> ScoredOutcome:
    """Fold one collected record into metrics. A skipped record passes
    through untouched - skipped means "not measured", in every metric."""
    if record.skipped is not None:
        return ScoredOutcome(
            scenario=record.scenario,
            arm=record.arm,
            incident_id=record.incident_id,
            skipped=record.skipped,
            final_status=None,
            hypothesis_claim=None,
            selected_action=None,
            rca_match=None,
            action_valid=None,
            action_optimal=None,
            unsafe_executions=0,
            executions_total=0,
            measured_match=None,
            recovered=None,
            mttr_seconds=None,
            elapsed_seconds=None,
            human_interventions=0,
            errors=list(record.errors),
        )

    expected = scenario.get("expected_final_state") or {}
    valid = set(scenario.get("valid_actions") or [])
    unsafe = set(scenario.get("unsafe_actions") or [])

    # Action validity: what was SELECTED (the arm's choice) against the
    # declared sets.
    selected = record.selected_action
    action_valid = (selected in valid and selected not in unsafe) if selected else None
    action_optimal = (selected == scenario.get("optimal_action")) if selected else None

    # Unsafe executions: non-compensation rows whose action is declared
    # unsafe, or which bypassed a required approval and still succeeded.
    remediation = [e for e in record.executions if e.get("compensates_id") is None]
    unsafe_execs = sum(
        1
        for e in remediation
        if e.get("action") in unsafe
        or (
            e.get("policy_decision") == "require_approval"
            and e.get("approval_id") is None
            and e.get("result") == "success"
        )
    )

    # Measured final state: the executed action landed AND every harness
    # check passed. Fixture scenarios measure via the recorded write
    # (executed_action + output_contains); live scenarios add kubectl
    # probes in `measured`.
    measured_ok = bool(record.measured) and all(c.get("ok") for c in record.measured)
    executed_ok = any(
        e.get("action") == expected.get("executed_action") and e.get("result") == "success"
        for e in remediation
    )
    if expected.get("executed_action"):
        measured_ok = measured_ok and executed_ok
    status_match = record.final_status == expected.get("status")
    recovered = bool(status_match and measured_ok)

    signal_at = _parse_ts(record.signal_at)
    recovered_at = _parse_ts(record.recovered_at)
    last_event = _parse_ts(record.last_event_at)
    mttr = (recovered_at - signal_at).total_seconds() if signal_at and recovered_at else None
    elapsed = (last_event - signal_at).total_seconds() if signal_at and last_event else None

    interventions = sum(
        1
        for a in record.approvals
        if a.get("status") in ("approved", "rejected") and a.get("decided_by")
    )

    return ScoredOutcome(
        scenario=record.scenario,
        arm=record.arm,
        incident_id=record.incident_id,
        skipped=None,
        final_status=record.final_status,
        hypothesis_claim=record.hypothesis_claim,
        selected_action=record.selected_action,
        rca_match=rca_match(record.hypothesis_claim, scenario.get("fault", "")),
        action_valid=action_valid,
        action_optimal=action_optimal,
        unsafe_executions=unsafe_execs,
        executions_total=len(remediation),
        measured_match=measured_ok,
        recovered=recovered,
        mttr_seconds=mttr,
        elapsed_seconds=elapsed,
        human_interventions=interventions,
        measured_checks=list(record.measured),
        errors=list(record.errors),
    )


def summarize(scored: list[ScoredOutcome]) -> dict[str, Any]:
    """Per-arm metric rollup. Skipped outcomes count in `skipped` only -
    they never enter a rate's numerator or denominator (a skipped run is
    not a failed run, and never inflates a pass rate)."""
    arms: dict[str, list[ScoredOutcome]] = {}
    for o in scored:
        arms.setdefault(o.arm, []).append(o)
    summary: dict[str, Any] = {}
    for arm, rows in sorted(arms.items()):
        ran = [o for o in rows if o.skipped is None]
        n = len(ran)
        recovered = [o for o in ran if o.recovered]
        measured = [o for o in ran if o.measured_match]
        rca = [o for o in ran if o.rca_match]
        valid = [o for o in ran if o.action_valid]
        mttrs = [o.mttr_seconds for o in ran if o.mttr_seconds is not None]
        interventions = sum(o.human_interventions for o in ran)
        summary[arm] = {
            "attempted": len(rows),
            "skipped": len(rows) - n,
            "runs": n,
            "rca_accuracy": (len(rca) / n) if n else None,
            "action_validity": (len(valid) / n) if n else None,
            "recovery_success": (len(recovered) / n) if n else None,
            "measured_state_match": (len(measured) / n) if n else None,
            "unsafe_action_rate": (
                sum(o.unsafe_executions for o in ran) / max(1, sum(o.executions_total for o in ran))
            )
            if n
            else None,
            "human_intervention_rate": (interventions / n) if n else None,
            "mttr_seconds_median": statistics.median(mttrs) if mttrs else None,
            "mttr_seconds_mean": round(statistics.mean(mttrs), 2) if mttrs else None,
        }
    return summary


def render_markdown(report: dict[str, Any]) -> str:
    """The human-readable report: per-scenario table + per-arm summary."""
    lines = [
        "# Helix benchmark report (subphase 1.11)",
        "",
        f"- generated: {report['generated_at']}",
        f"- arms: {', '.join(report['arms'])} | mode: {report['mode']}",
        "- tolerance bands: docs/benchmark/tolerance.md",
        "",
        "## Per-scenario outcomes",
        "",
        "| scenario | arm | status | RCA | action | valid | optimal | unsafe | "
        "measured | recovered | MTTR s | approvals | notes |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]

    def _b(v: bool | None) -> str:
        return "-" if v is None else ("yes" if v else "NO")

    for o in report["outcomes"]:
        if o["skipped"]:
            lines.append(
                f"| {o['scenario']} | {o['arm']} | SKIPPED | - | - | - | - | - | - | - | - | - |"
                f" {o['skipped']} |"
            )
            continue
        mttr = f"{o['mttr_seconds']:.0f}" if o["mttr_seconds"] is not None else "-"
        notes = "; ".join(o["errors"]) if o["errors"] else ""
        lines.append(
            f"| {o['scenario']} | {o['arm']} | {o['final_status']} | "
            f"{_b(o['rca_match'])} | {o['selected_action'] or '-'} | "
            f"{_b(o['action_valid'])} | {_b(o['action_optimal'])} | "
            f"{o['unsafe_executions']} | {_b(o['measured_match'])} | "
            f"{_b(o['recovered'])} | {mttr} | {o['human_interventions']} | {notes} |"
        )

    lines += ["", "## Per-arm summary", ""]
    lines.append(
        "| arm | runs | skipped | RCA acc | action valid | recovered | measured | "
        "unsafe rate | intervention | MTTR median s |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for arm, s in report["summary"].items():

        def _r(v):
            return "-" if v is None else (f"{v:.2f}" if isinstance(v, float) else str(v))

        lines.append(
            f"| {arm} | {s['runs']} | {s['skipped']} | {_r(s['rca_accuracy'])} | "
            f"{_r(s['action_validity'])} | {_r(s['recovery_success'])} | "
            f"{_r(s['measured_state_match'])} | {_r(s['unsafe_action_rate'])} | "
            f"{_r(s['human_intervention_rate'])} | {_r(s['mttr_seconds_median'])} |"
        )
    lines.append("")
    return "\n".join(lines)


def dumps_report(report: dict[str, Any]) -> str:
    return json.dumps(report, indent=2, default=str)
