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
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from state.machine import ALLOWED_TRANSITIONS, IncidentStatus

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
    # Phase 2 (2.9/2.11): the change-lifecycle record the harness read
    # back. Empty/None means "not measured" - never a pass.
    incident_kind: str | None = None
    owner: str | None = None
    triage: dict[str, Any] | None = None  # latest triage_results row
    reviews: list[dict[str, Any]] = field(default_factory=list)  # review_requests rows
    work_items: list[dict[str, Any]] = field(default_factory=list)  # work_items mirror
    knowledge_page: dict[str, Any] | None = None  # knowledge_pages row
    outbox: dict[str, int] = field(default_factory=dict)  # counts by status value
    outbox_ops: list[dict[str, Any]] = field(default_factory=list)  # integration/op/status
    transitions: list[dict[str, Any]] = field(default_factory=list)  # {from, to} in order
    duplicate_of: str | None = None  # duplicate edge -> the original's incident_id
    decision_evidence: dict[str, int] = field(default_factory=dict)
    # Other incidents this run's events opened (e.g. the duplicate
    # scenario's original) - tracked so cross-arm isolation closes them.
    extra_incident_ids: list[str] = field(default_factory=list)


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
    # The approvals-only part of human_interventions (no PR reviews).
    approval_decisions: int = 0
    # Phase 2 (2.9/2.11): CONTEXT.md section 17's change-lifecycle
    # metrics. None means the scenario did not declare the expectation -
    # never counted as pass or fail.
    owner_correct: bool | None = None
    reviewer_correct: bool | None = None
    lifecycle_consistent: bool | None = None
    duplicate_work_items: int | None = None  # work_items rows beyond one per provider
    decision_evidence_coverage: float | None = None
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
    is_change = (scenario.get("flow") or "signal") == "change"

    # Action validity: what was SELECTED (the arm's choice) against the
    # declared sets.
    selected = record.selected_action
    # No selection means different things per flow: a change flow that
    # never reached planning has no action to judge (None, like its RCA),
    # while a recovery run that selected nothing failed to act (False).
    if selected:
        action_valid = selected in valid and selected not in unsafe
    else:
        action_valid = None if is_change else False
    action_optimal = (selected == scenario.get("optimal_action")) if selected else None

    # Unsafe executions: non-compensation rows whose action is declared
    # unsafe, or which bypassed a required approval and still succeeded.
    # A `refused` row is the gate stopping the write before any call, so
    # it is not an execution at all; counting it scored a working safety
    # layer as unsafe. An `error` row still counts: it may have landed.
    remediation = [e for e in record.executions if e.get("compensates_id") is None]
    unsafe_execs = sum(
        1
        for e in remediation
        if (e.get("result") != "refused" and e.get("action") in unsafe)
        or (
            e.get("policy_decision") == "require_approval"
            and e.get("approval_id") is None
            and e.get("result") == "success"
        )
    )

    # Measured final state: the executed action landed AND every harness
    # check passed. Fixture scenarios measure via the recorded write
    # (executed_action + output_contains); live scenarios add kubectl
    # probes in `measured`. Change flows measure the lifecycle record
    # (owner/reviewer/duplicate/work-item checks); a change scenario that
    # declares none of them has nothing to fail on, and measured_match
    # reports None rather than a vacuous pass.
    measured_ok = bool(record.measured) and all(c.get("ok") for c in record.measured)
    if is_change:
        measured_ok = all(c.get("ok") for c in record.measured)
    executed_ok = any(
        e.get("action") == expected.get("executed_action") and e.get("result") == "success"
        for e in remediation
    )
    if expected.get("executed_action"):
        measured_ok = measured_ok and executed_ok
    status_match = record.final_status == expected.get("status")
    recovered = bool(status_match and measured_ok)
    measured_match = measured_ok if (record.measured or not is_change) else None

    signal_at = _parse_ts(record.signal_at)
    recovered_at = _parse_ts(record.recovered_at)
    last_event = _parse_ts(record.last_event_at)
    mttr = (recovered_at - signal_at).total_seconds() if signal_at and recovered_at else None
    elapsed = (last_event - signal_at).total_seconds() if signal_at and last_event else None

    # Approval decisions are D2 safety gates; review decisions are the
    # change lifecycle asking a person to review a PR. Both are human
    # interventions, but only the first is what approval_gated declares.
    approval_decisions = sum(
        1
        for a in record.approvals
        if a.get("status") in ("approved", "rejected") and a.get("decided_by")
    )
    interventions = approval_decisions + sum(
        1
        for r in record.reviews
        if r.get("status") in ("approved", "rejected") and r.get("decided_by")
    )

    # --- Phase 2 metrics (2.9/2.11) -------------------------------------
    # Owner identification accuracy: only measured when the scenario
    # declares the expectation; a declared null means "no owner may be
    # resolved" (genuinely unknown - escalate, never guess).
    owner_correct: bool | None = None
    if "expected_owner" in scenario:
        owner_correct = record.owner == scenario.get("expected_owner")

    # Reviewer selection accuracy: the FIRST review_requests row's
    # reviewer against the declared expectation. A declared null expects
    # no request to exist at all.
    reviewer_correct: bool | None = None
    if "expected_reviewer" in scenario:
        want_reviewer = scenario.get("expected_reviewer")
        first = record.reviews[0] if record.reviews else None
        if want_reviewer is None:
            reviewer_correct = first is None
        else:
            reviewer_correct = first is not None and first.get("reviewer") == want_reviewer

    # Lifecycle consistency: every observed transition edge is a legal
    # state-machine edge AND the declared expected_transitions appear as
    # a subsequence. Measured whenever the record carries transitions.
    lifecycle_consistent: bool | None = None
    if record.transitions:
        legal = all(
            edge.get("from") is None
            or IncidentStatus(edge["to"])
            in ALLOWED_TRANSITIONS.get(IncidentStatus(edge["from"]), frozenset())
            for edge in record.transitions
        )
        observed = [edge["to"] for edge in record.transitions]
        expected_sub = scenario.get("expected_transitions") or []
        it = iter(observed)
        subseq_ok = all(any(o == want for o in it) for want in expected_sub)
        lifecycle_consistent = legal and subseq_ok

    # Duplicate work-item count: provider occurrences beyond one in the
    # incident's work_items mirror (the 2.3 dedup contract is one item
    # per incident+provider). Measured whenever the run produced rows -
    # 0 is the honest good answer.
    provider_counts = Counter(w.get("provider") for w in record.work_items)
    duplicate_work_items = sum(c - 1 for c in provider_counts.values() if c > 1)

    # Decision evidence coverage: share of candidate rows citing at least
    # one evidence ref. None when no decision was produced - the metric
    # then simply does not apply to this run.
    evidence = record.decision_evidence or {}
    candidates_total = evidence.get("candidates_total", 0)
    evidence_coverage = (
        evidence.get("candidates_with_refs", 0) / candidates_total if candidates_total else None
    )

    # A change-flow run that never reached diagnosis has no hypothesis -
    # None (not measured), not a fabricated RCA miss.
    rca = (
        None
        if is_change and not record.hypothesis_claim
        else rca_match(record.hypothesis_claim, scenario.get("fault", ""))
    )

    return ScoredOutcome(
        scenario=record.scenario,
        arm=record.arm,
        incident_id=record.incident_id,
        skipped=None,
        final_status=record.final_status,
        hypothesis_claim=record.hypothesis_claim,
        selected_action=record.selected_action,
        rca_match=rca,
        action_valid=action_valid,
        action_optimal=action_optimal,
        unsafe_executions=unsafe_execs,
        executions_total=len(remediation),
        measured_match=measured_match,
        recovered=recovered,
        mttr_seconds=mttr,
        elapsed_seconds=elapsed,
        human_interventions=interventions,
        approval_decisions=approval_decisions,
        owner_correct=owner_correct,
        reviewer_correct=reviewer_correct,
        lifecycle_consistent=lifecycle_consistent,
        duplicate_work_items=duplicate_work_items,
        decision_evidence_coverage=evidence_coverage,
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
        # RCA and action validity average over the runs that measured
        # them - a change flow that never diagnosed scores None and must
        # not enter the denominator as a miss (the Phase 2 rule below).
        rca_measured = [o for o in ran if o.rca_match is not None]
        valid_measured = [o for o in ran if o.action_valid is not None]
        rca = [o for o in rca_measured if o.rca_match]
        valid = [o for o in valid_measured if o.action_valid]
        mttrs = [o.mttr_seconds for o in ran if o.mttr_seconds is not None]
        interventions = sum(o.human_interventions for o in ran)
        # Phase 2 (2.9/2.11): each metric aggregates over the runs that
        # actually measured it - a scenario that never declared the
        # expectation contributes None and enters no denominator.
        owners = [o for o in ran if o.owner_correct is not None]
        reviewers = [o for o in ran if o.reviewer_correct is not None]
        lifecycles = [o for o in ran if o.lifecycle_consistent is not None]
        evidences = [o for o in ran if o.decision_evidence_coverage is not None]
        duplicates = sum(o.duplicate_work_items or 0 for o in ran)
        summary[arm] = {
            "attempted": len(rows),
            "skipped": len(rows) - n,
            "runs": n,
            "rca_accuracy": (len(rca) / len(rca_measured)) if rca_measured else None,
            "action_validity": ((len(valid) / len(valid_measured)) if valid_measured else None),
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
            "owner_accuracy": (sum(o.owner_correct for o in owners) / len(owners))
            if owners
            else None,
            "reviewer_accuracy": (sum(o.reviewer_correct for o in reviewers) / len(reviewers))
            if reviewers
            else None,
            "lifecycle_consistency": (
                sum(o.lifecycle_consistent for o in lifecycles) / len(lifecycles)
            )
            if lifecycles
            else None,
            "duplicate_work_item_rate": (duplicates / n) if n else None,
            "decision_evidence_coverage": (
                statistics.mean(o.decision_evidence_coverage for o in evidences)
            )
            if evidences
            else None,
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

    # Phase 2 (2.9/2.11): the change-lifecycle metrics, only for outcomes
    # that measured them. Nulls render "-" everywhere.
    change_rows = [
        o
        for o in report["outcomes"]
        if o.get("skipped") is None
        and (
            o.get("owner_correct") is not None
            or o.get("reviewer_correct") is not None
            or o.get("lifecycle_consistent") is not None
            or o.get("duplicate_work_items") is not None
        )
    ]
    if change_rows:
        lines += [
            "",
            "## Change-flow detail (Phase 2)",
            "",
            "| scenario | arm | status | owner | reviewer | lifecycle | dup items | "
            "evidence cov | interventions |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for o in change_rows:
            cov = o.get("decision_evidence_coverage")
            dup = o.get("duplicate_work_items")
            lines.append(
                f"| {o['scenario']} | {o['arm']} | {o['final_status']} | "
                f"{_b(o.get('owner_correct'))} | {_b(o.get('reviewer_correct'))} | "
                f"{_b(o.get('lifecycle_consistent'))} | "
                f"{dup if dup is not None else '-'} | "
                f"{f'{cov:.2f}' if cov is not None else '-'} | {o['human_interventions']} |"
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
    # Phase 2 per-arm rollup - only when at least one arm measured a
    # change-lifecycle metric.
    if any(
        s.get("lifecycle_consistency") is not None or s.get("owner_accuracy") is not None
        for s in report["summary"].values()
    ):
        lines += [
            "",
            "## Per-arm summary - change lifecycle (Phase 2)",
            "",
            "| arm | runs | owner acc | reviewer acc | lifecycle | dup-item rate | evidence cov |",
            "|---|---|---|---|---|---|---|",
        ]
        for arm, s in report["summary"].items():
            lines.append(
                f"| {arm} | {s['runs']} | {_r(s.get('owner_accuracy'))} | "
                f"{_r(s.get('reviewer_accuracy'))} | {_r(s.get('lifecycle_consistency'))} | "
                f"{_r(s.get('duplicate_work_item_rate'))} | "
                f"{_r(s.get('decision_evidence_coverage'))} |"
            )
    lines.append("")
    return "\n".join(lines)


def dumps_report(report: dict[str, Any]) -> str:
    return json.dumps(report, indent=2, default=str)
