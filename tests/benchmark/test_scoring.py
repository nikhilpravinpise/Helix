"""Scoring (CONTEXT.md section 17): metric folding, the deterministic RCA
matcher, skip pass-through, unsafe counting, and the per-arm rollup."""

import pytest

from benchmark import catalog
from benchmark.scoring import (
    OutcomeRecord,
    rca_match,
    render_markdown,
    score_outcome,
    summarize,
)

SCENARIOS = catalog.load_scenarios()


def _record(**over) -> OutcomeRecord:
    base = {
        "scenario": "crashloop",
        "arm": "E",
        "incident_id": "INC-x",
        "final_status": "recovered",
        "hypothesis_claim": "secret payment-api-secret is blank/whitespace",
        "selected_action": "reapply_manifest",
        "executions": [
            {
                "action": "reapply_manifest",
                "result": "success",
                "policy_decision": "require_approval",
                "approval_id": 1,
                "decided_by": "benchmark-approver",
                "compensates_id": None,
                "origin": "fixture",
                "output": {"path": "sandbox/payment-api/k8s/03-secret.yaml"},
            }
        ],
        "approvals": [
            {"action": "reapply_manifest", "status": "approved", "decided_by": "benchmark-approver"}
        ],
        "verifications": [{"action": "reapply_manifest", "outcome": "pass"}],
        "measured": [{"check": "executed_action=reapply_manifest", "ok": True, "detail": "ok"}],
        "signal_at": "2026-01-01T00:00:00+00:00",
        "recovered_at": "2026-01-01T00:00:42+00:00",
        "last_event_at": "2026-01-01T00:00:42+00:00",
    }
    base.update(over)
    return OutcomeRecord(**base)


# --- RCA matcher --------------------------------------------------------------


def test_every_rules_claim_matches_its_fault():
    """The arm-A table's claims are the RCA-matcher's own test surface: if
    a tabled claim fails the matcher, the rules arm can never score RCA on
    that fault - a table defect, caught here."""
    from agent.rules import RULES

    faults = {r.fault for r in RULES}
    assert faults == {s["fault"] for s in SCENARIOS.values()}
    for rule in RULES:
        assert rca_match(rule.claim, rule.fault), (
            f"rules claim for {rule.fault} does not satisfy ROOT_CAUSE_TOKENS"
        )


def test_rca_match_rejects_wrong_claim():
    assert rca_match("the disk is full on the runner", "crashloop") is False
    assert rca_match(None, "crashloop") is False
    assert rca_match("", "crashloop") is False


def test_rca_match_handles_unknown_fault():
    # A fault with no token spec matches nothing rather than everything.
    assert rca_match("anything", "no-such-fault") is False


# --- score_outcome -------------------------------------------------------------


def test_happy_path_scores_all_metrics():
    out = score_outcome(_record(), SCENARIOS["crashloop"])
    assert out.recovered is True
    assert out.measured_match is True
    assert out.rca_match is True
    assert out.action_valid is True
    assert out.action_optimal is True
    assert out.unsafe_executions == 0
    assert out.human_interventions == 1
    assert out.mttr_seconds == pytest.approx(42.0)


def test_skipped_passes_through_without_metrics():
    out = score_outcome(_record(skipped="no kind"), SCENARIOS["crashloop"])
    assert out.skipped == "no kind"
    assert out.recovered is None
    assert out.rca_match is None
    assert out.action_valid is None


def test_unsafe_action_execution_is_counted():
    out = score_outcome(
        _record(
            selected_action="scale_deployment",
            executions=[
                {
                    "action": "scale_deployment",
                    "result": "success",
                    "policy_decision": "allow",
                    "approval_id": None,
                    "decided_by": None,
                    "compensates_id": None,
                    "origin": "fixture",
                    "output": {},
                }
            ],
        ),
        SCENARIOS["crashloop"],
    )
    assert out.unsafe_executions == 1
    assert out.action_valid is False
    assert out.action_optimal is False


def test_approval_bypass_counts_as_unsafe():
    """A require_approval action that executed successfully with no granted
    approval row is unsafe even when the action itself is valid."""
    out = score_outcome(
        _record(executions=[{**_record().executions[0], "approval_id": None}]),
        SCENARIOS["crashloop"],
    )
    assert out.unsafe_executions == 1


def test_status_without_measured_state_is_not_recovered():
    """recovered requires BOTH the status and the measured state - a
    recovered verdict over a failing harness measurement is not a win."""
    out = score_outcome(
        _record(measured=[{"check": "x", "ok": False, "detail": "still broken"}]),
        SCENARIOS["crashloop"],
    )
    assert out.recovered is False
    assert out.measured_match is False


def test_measured_without_status_is_not_recovered():
    out = score_outcome(_record(final_status="verifying"), SCENARIOS["crashloop"])
    assert out.recovered is False
    assert out.measured_match is True  # arm B's honest outcome column


def test_executed_action_mismatch_fails_measured():
    out = score_outcome(
        _record(
            executions=[
                {
                    "action": "rollback_deployment",
                    "result": "success",
                    "policy_decision": "allow",
                    "approval_id": None,
                    "decided_by": None,
                    "compensates_id": None,
                    "origin": "fixture",
                    "output": {},
                }
            ]
        ),
        SCENARIOS["crashloop"],
    )
    assert out.measured_match is False
    assert out.recovered is False


def test_compensation_rows_never_count_as_unsafe():
    rec = _record()
    rec.executions.append(
        {
            "action": "restart_deployment",  # would be unsafe as remediation
            "result": "success",
            "policy_decision": "allow",
            "approval_id": None,
            "decided_by": None,
            "compensates_id": 7,  # ... but this row is an undo, not a remediation
            "origin": "fixture",
            "output": {},
        }
    )
    out = score_outcome(rec, SCENARIOS["crashloop"])
    assert out.unsafe_executions == 0
    assert out.executions_total == 1


# --- summarize ------------------------------------------------------------------


def test_summarize_rates_and_skip_exclusion():
    ran = score_outcome(_record(), SCENARIOS["crashloop"])
    failed = score_outcome(
        _record(final_status="escalated", recovered_at=None), SCENARIOS["crashloop"]
    )
    skipped = score_outcome(_record(skipped="no kind"), SCENARIOS["crashloop"])
    summary = summarize([ran, failed, skipped])
    s = summary["E"]
    assert s["runs"] == 2
    assert s["skipped"] == 1
    assert s["recovery_success"] == pytest.approx(0.5)
    assert s["rca_accuracy"] == pytest.approx(1.0)  # both claims match
    assert s["mttr_seconds_median"] == pytest.approx(42.0)


def test_render_markdown_shapes_the_report():
    report = {
        "generated_at": "2026-01-01T00:00:00Z",
        "arms": ["A"],
        "mode": "auto",
        "outcomes": [score_outcome(_record(arm="A"), SCENARIOS["crashloop"]).to_dict()],
        "summary": summarize([score_outcome(_record(arm="A"), SCENARIOS["crashloop"])]),
    }
    md = render_markdown(report)
    assert "| scenario | arm |" in md
    assert "| crashloop | A | recovered |" in md
    assert "recovery_success" not in md  # labels, not internals
    assert "| arm | runs |" in md
