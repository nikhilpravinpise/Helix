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
    that fault - a table defect, caught here. Change-flow scenarios carry
    lifecycle faults (owner, reviewer, duplicate), not rule faults, so the
    1:1 mapping holds over the signal-flow catalog."""
    from agent.rules import RULES

    faults = {r.fault for r in RULES}
    signal_faults = {
        s["fault"] for s in SCENARIOS.values() if (s.get("flow") or "signal") == "signal"
    }
    assert faults == signal_faults
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


# --- Phase 2 metrics (subphase 2.9/2.11) --------------------------------------


def _change_record(**over) -> OutcomeRecord:
    """A collected change-flow record: the full lifecycle reached
    recovered with an owner, a decided review, one work item, and the
    transition chain the happy path produces."""
    base = {
        "scenario": "org-known-owner-change",
        "arm": "A",
        "incident_id": "INC-change",
        "final_status": "recovered",
        "incident_kind": "change",
        "owner": "alice",
        "triage": {
            "affected_service": "payment-api",
            "owner": "alice",
            "owner_source": "codeowners",
            "duplicate_of_id": None,
        },
        "reviews": [
            {
                "reviewer": "bob",
                "status": "approved",
                "decided_by": "benchmark-reviewer",
                "attempt": 0,
            }
        ],
        "work_items": [
            {"provider": "memory", "external_id": "MEM-1", "status": "done", "assignee": "alice"}
        ],
        "transitions": [
            {"from": a, "to": b}
            for a, b in zip(
                [
                    None,
                    "received",
                    "triaged",
                    "owner_assigned",
                    "review_pending",
                    "approved",
                    "deploying",
                ],
                [
                    "received",
                    "triaged",
                    "owner_assigned",
                    "review_pending",
                    "approved",
                    "deploying",
                    "recovered",
                ],
                strict=True,
            )
        ],
        "signal_at": "2026-01-01T00:00:00+00:00",
        "recovered_at": "2026-01-01T00:00:42+00:00",
        "last_event_at": "2026-01-01T00:00:42+00:00",
    }
    base.update(over)
    return OutcomeRecord(**base)


CHANGE_SCENARIO = {
    "flow": "change",
    "expected_final_state": {"status": "recovered"},
    "expected_owner": "alice",
    "expected_reviewer": "bob",
    "expected_transitions": [
        "received",
        "triaged",
        "owner_assigned",
        "review_pending",
        "approved",
        "deploying",
        "recovered",
    ],
}


def test_change_happy_path_scores_phase2_metrics():
    out = score_outcome(_change_record(), CHANGE_SCENARIO)
    assert out.recovered is True
    assert out.owner_correct is True
    assert out.reviewer_correct is True
    assert out.lifecycle_consistent is True
    assert out.duplicate_work_items == 0
    assert out.human_interventions == 1  # the decided review row
    # No diagnosis ever ran - RCA is honestly unmeasured, not a miss.
    assert out.rca_match is None


def test_owner_null_expectation_measures_no_fabricated_owner():
    """`expected_owner: null` passes only when no owner was resolved - a
    guessed owner fails the metric outright."""
    scenario = {**CHANGE_SCENARIO, "expected_owner": None}
    out = score_outcome(_change_record(owner=None), scenario)
    assert out.owner_correct is True
    out = score_outcome(_change_record(owner="alice"), scenario)
    assert out.owner_correct is False


def test_wrong_owner_and_reviewer_fail_their_metrics():
    out = score_outcome(
        _change_record(
            owner="nobody",
            reviews=[
                {
                    "reviewer": "dana",
                    "status": "approved",
                    "decided_by": "benchmark-reviewer",
                    "attempt": 0,
                }
            ],
        ),
        CHANGE_SCENARIO,
    )
    assert out.owner_correct is False
    assert out.reviewer_correct is False


def test_reviewer_null_expectation_requires_no_request():
    scenario = {**CHANGE_SCENARIO, "expected_reviewer": None}
    out = score_outcome(_change_record(reviews=[]), scenario)
    assert out.reviewer_correct is True
    out = score_outcome(_change_record(), scenario)
    assert out.reviewer_correct is False


def test_lifecycle_consistency_rejects_illegal_edges_and_missing_steps():
    """Every observed edge must be a legal state-machine edge, and the
    declared chain must appear in order."""
    out = score_outcome(_change_record(), CHANGE_SCENARIO)
    assert out.lifecycle_consistent is True

    illegal = _change_record(
        transitions=[
            {"from": "received", "to": "recovered"},  # no such edge
        ]
    )
    assert score_outcome(illegal, CHANGE_SCENARIO).lifecycle_consistent is False

    missing = _change_record(
        transitions=[
            {"from": None, "to": "received"},
            {"from": "received", "to": "triaged"},
            {"from": "triaged", "to": "owner_assigned"},
        ]
    )
    assert score_outcome(missing, CHANGE_SCENARIO).lifecycle_consistent is False

    # No declared expectation: legality alone is still measured.
    no_expect = {k: v for k, v in CHANGE_SCENARIO.items() if k != "expected_transitions"}
    assert score_outcome(missing, no_expect).lifecycle_consistent is True


def test_duplicate_work_item_rate_counts_extra_provider_rows():
    dup = _change_record(
        work_items=[
            {"provider": "memory", "external_id": "MEM-1", "status": "done", "assignee": None},
            {"provider": "memory", "external_id": "MEM-9", "status": "open", "assignee": None},
        ]
    )
    assert score_outcome(dup, CHANGE_SCENARIO).duplicate_work_items == 1


def test_decision_evidence_coverage_is_measured_or_none():
    out = score_outcome(
        _change_record(decision_evidence={"candidates_with_refs": 1, "candidates_total": 2}),
        CHANGE_SCENARIO,
    )
    assert out.decision_evidence_coverage == pytest.approx(0.5)
    # No candidates at all (a pure lifecycle run): unmeasured, not 0 or 1.
    assert score_outcome(_change_record(), CHANGE_SCENARIO).decision_evidence_coverage is None


def test_undeclared_expectations_are_unmeasured_not_passed():
    """A change scenario declaring no Phase 2 expectations scores None on
    each - never a free pass."""
    out = score_outcome(_change_record(), {"flow": "change"})
    assert out.owner_correct is None
    assert out.reviewer_correct is None
    assert out.measured_match is None  # no measured checks declared
    assert out.recovered is False  # no expected status either


def test_review_decisions_count_as_human_interventions():
    out = score_outcome(_change_record(), CHANGE_SCENARIO)
    assert out.human_interventions == 1
    rejected = _change_record(
        reviews=[
            {
                "reviewer": "bob",
                "status": "rejected",
                "decided_by": "benchmark-reviewer",
                "attempt": 0,
            },
            {
                "reviewer": "carol",
                "status": "approved",
                "decided_by": "benchmark-reviewer",
                "attempt": 0,
            },
        ]
    )
    assert score_outcome(rejected, CHANGE_SCENARIO).human_interventions == 2


def test_summarize_aggregates_phase2_metrics_over_measured_runs_only():
    ran = score_outcome(_change_record(), CHANGE_SCENARIO)
    undeclared = score_outcome(
        _change_record(),
        {"flow": "change", "expected_final_state": {"status": "recovered"}},
    )
    summary = summarize([ran, undeclared])
    s = summary["A"]
    assert s["owner_accuracy"] == pytest.approx(1.0)
    assert s["reviewer_accuracy"] == pytest.approx(1.0)
    assert s["lifecycle_consistency"] == pytest.approx(1.0)
    assert s["duplicate_work_item_rate"] == pytest.approx(0.0)
    assert s["decision_evidence_coverage"] is None  # no run produced candidates


def test_render_markdown_includes_change_flow_detail():
    out = score_outcome(_change_record(), CHANGE_SCENARIO)
    report = {
        "generated_at": "2026-01-01T00:00:00Z",
        "arms": ["A"],
        "mode": "auto",
        "outcomes": [out.to_dict()],
        "summary": summarize([out]),
    }
    md = render_markdown(report)
    assert "Change-flow detail" in md
    assert "| org-known-owner-change | A | recovered |" in md
    assert "owner acc" in md


def test_change_flow_without_diagnosis_does_not_count_as_an_rca_or_action_miss():
    """A change flow that never reached diagnosis scores RCA and action
    validity as None - not measured, per score_outcome - and None must
    enter no denominator, the rule the Phase 2 metrics already follow.
    The first full 19-scenario run had arm A, perfect on every scenario
    it diagnosed, reading 0.68 on both because the five change flows
    that never diagnosed were averaged in as misses."""
    diagnosed = score_outcome(_record(arm="A"), SCENARIOS["crashloop"])
    change = score_outcome(
        _change_record(hypothesis_claim=None, selected_action=None), CHANGE_SCENARIO
    )
    assert change.rca_match is None and change.action_valid is None
    s = summarize([diagnosed, change])["A"]
    assert s["runs"] == 2
    assert s["rca_accuracy"] == pytest.approx(1.0)
    assert s["action_validity"] == pytest.approx(1.0)


def test_signal_flow_that_selects_nothing_is_still_an_action_miss():
    """The other side of the rule: on the recovery path, selecting no
    action is a failure to act, not an absent measurement. Excluding it
    would inflate exactly the LLM arms that fail to produce a plan."""
    acted = score_outcome(_record(), SCENARIOS["crashloop"])
    idle = score_outcome(_record(selected_action=None), SCENARIOS["crashloop"])
    assert idle.action_valid is False
    assert summarize([acted, idle])["E"]["action_validity"] == pytest.approx(0.5)
