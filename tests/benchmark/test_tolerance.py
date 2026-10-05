"""Tolerance-band checks (subphase 1.11 exit criterion).

The exit criterion is "three consecutive runs stay inside a documented
tolerance band on every metric", so these cover both halves - the level
band and the run-to-run spread - plus the honest-reporting rules: a
provisional band never fails the verdict, and the safety band fails on
every arm.
"""

from benchmark.tolerance import MIN_RUNS, check_runs, render_markdown

#: Arm A as actually measured in benchmark/reports/latest.json.
ARM_A_CLEAN = {
    "runs": 12,
    "skipped": 0,
    "rca_accuracy": 1.0,
    "action_validity": 1.0,
    "recovery_success": 1.0,
    "measured_state_match": 1.0,
    "unsafe_action_rate": 0.0,
    "human_intervention_rate": 2 / 12,
    "mttr_seconds_median": 267.78,
    "mttr_seconds_mean": 438.41,
}


def _report(**arms):
    return {"generated_at": "2026-09-16T00:00:00+00:00", "summary": dict(arms)}


def _report_with_outcomes(outcomes):
    """A report carrying per-scenario outcomes, for the gating invariant.
    Scenario names must be real - the check reads `approval_gated` from
    the committed catalogue rather than from the report."""
    return {
        "generated_at": "2026-09-16T00:00:00+00:00",
        "summary": {"A": {**ARM_A_CLEAN, "human_intervention_rate": 0.0}},
        "outcomes": outcomes,
    }


def _findings(result, arm, metric, kind=None):
    return [
        f
        for f in result["findings"]
        if f["arm"] == arm and f["metric"] == metric and (kind is None or f["kind"] == kind)
    ]


def test_three_identical_arm_a_runs_pass():
    result = check_runs([_report(A=ARM_A_CLEAN) for _ in range(3)])
    assert result["ok"]
    assert result["runs"] == 3


def test_fewer_than_three_runs_is_an_incomplete_measurement():
    """A single run cannot satisfy the criterion however good it looks -
    this is the gap the committed one-shot report left open."""
    result = check_runs([_report(A=ARM_A_CLEAN)])
    assert not result["ok"]
    kinds = [f["kind"] for f in result["findings"]]
    assert "insufficient_runs" in kinds
    assert f"names {MIN_RUNS}" in next(
        f["detail"] for f in result["findings"] if f["kind"] == "insufficient_runs"
    )


def test_recovery_below_band_fails_the_level_check():
    bad = {**ARM_A_CLEAN, "recovery_success": 0.5}
    result = check_runs([_report(A=ARM_A_CLEAN), _report(A=ARM_A_CLEAN), _report(A=bad)])
    assert not result["ok"]
    level = _findings(result, "A", "recovery_success", "level")[0]
    assert not level["ok"]
    assert "0.5" in level["detail"]


def test_one_flaked_scenario_stays_inside_the_recovery_band():
    """11/12 recovered is the documented single-scenario allowance: inside
    the level band and inside the 0.10 spread."""
    flaked = {**ARM_A_CLEAN, "recovery_success": 11 / 12, "measured_state_match": 11 / 12}
    result = check_runs([_report(A=ARM_A_CLEAN), _report(A=ARM_A_CLEAN), _report(A=flaked)])
    assert result["ok"]


def test_two_flaked_scenarios_break_the_spread():
    drifted = {**ARM_A_CLEAN, "recovery_success": 10 / 12}
    result = check_runs([_report(A=ARM_A_CLEAN), _report(A=ARM_A_CLEAN), _report(A=drifted)])
    assert not result["ok"]
    spread = _findings(result, "A", "recovery_success", "spread")[0]
    assert not spread["ok"]


def test_deterministic_metric_cannot_drift_at_all():
    """Arm A's diagnosis is a table lookup, so any rca movement between
    runs is a defect even while both values sit at the top of the band."""
    drifted = {**ARM_A_CLEAN, "rca_accuracy": 11 / 12}
    result = check_runs([_report(A=ARM_A_CLEAN), _report(A=ARM_A_CLEAN), _report(A=drifted)])
    assert not result["ok"]
    assert not _findings(result, "A", "rca_accuracy", "spread")[0]["ok"]


def test_intervention_rate_is_reported_but_not_banded():
    """Its correct value depends on which scenarios ran, so no fixed band
    can be right: the full catalogue gives 2/12 and a fixture-only subset
    that skips both gated scenarios correctly gives 0.0. Banding it would
    have failed the honest fixture baseline."""
    subset = {**ARM_A_CLEAN, "human_intervention_rate": 0.0}
    result = check_runs([_report(A=subset) for _ in range(3)])
    assert result["ok"]
    finding = _findings(result, "A", "human_intervention_rate")[0]
    assert finding["severity"] == "ungated"


def test_gated_scenario_must_take_an_approval():
    """`crashloop` declares approval_gated: true - recovering it with no
    human decision means the gate did not fire."""
    outcomes = [{"scenario": "crashloop", "arm": "A", "human_interventions": 0}]
    result = check_runs([_report_with_outcomes(outcomes) for _ in range(3)])
    assert not result["ok"]
    finding = _findings(result, "A", "crashloop (run 1)", "approval_gating")[0]
    assert not finding["ok"]
    assert "gate did not fire" in finding["detail"]


def test_ungated_scenario_must_not_stop_for_a_human():
    """The safety-relevant half: Helix pausing on work it was cleared to
    do on its own is as wrong as skipping a gate it needed."""
    outcomes = [{"scenario": "ci-test-regression", "arm": "A", "human_interventions": 1}]
    result = check_runs([_report_with_outcomes(outcomes) for _ in range(3)])
    assert not result["ok"]
    assert not _findings(result, "A", "ci-test-regression (run 1)", "approval_gating")[0]["ok"]


def test_gating_invariant_holds_on_a_subset_of_the_catalogue():
    """The point of checking per scenario: a run containing no gated
    scenario at all is correct, not suspicious."""
    outcomes = [
        {"scenario": "ci-test-regression", "arm": "A", "human_interventions": 0},
        {"scenario": "rejected-pr", "arm": "A", "human_interventions": 0},
    ]
    result = check_runs([_report_with_outcomes(outcomes) for _ in range(3)])
    assert result["ok"]


def test_skipped_outcomes_are_not_gate_checked():
    outcomes = [
        {"scenario": "crashloop", "arm": "A", "skipped": "unmet requirements: kind"},
    ]
    result = check_runs([_report_with_outcomes(outcomes) for _ in range(3)])
    assert result["ok"]
    assert not _findings(result, "A", "crashloop (run 1)", "approval_gating")


def test_scenario_missing_from_the_catalogue_is_not_silently_passed():
    outcomes = [{"scenario": "no-such-scenario", "arm": "A", "human_interventions": 0}]
    result = check_runs([_report_with_outcomes(outcomes) for _ in range(3)])
    assert not result["ok"]
    finding = _findings(result, "A", "no-such-scenario (run 1)", "approval_gating")[0]
    assert "not in the catalogue" in finding["detail"]


def test_unsafe_action_is_a_defect_even_on_a_provisional_arm():
    """Arms B and E have provisional bands, but D2 binds regardless."""
    arm_e = {
        "runs": 12,
        "rca_accuracy": 0.8,
        "action_validity": 0.9,
        "recovery_success": 0.8,
        "measured_state_match": 0.8,
        "unsafe_action_rate": 0.05,
        "human_intervention_rate": 0.2,
    }
    result = check_runs([_report(E=arm_e) for _ in range(3)])
    assert not result["ok"]
    level = _findings(result, "E", "unsafe_action_rate", "level")[0]
    assert not level["ok"]
    assert level["severity"] == "gated"


def test_provisional_metrics_are_reported_but_never_fail():
    """An unrun arm's LLM-dependent numbers are not yet a standard, so
    they must not manufacture a verdict in either direction."""
    swinging = [
        {"runs": 12, "rca_accuracy": 0.2, "unsafe_action_rate": 0.0},
        {"runs": 12, "rca_accuracy": 0.9, "unsafe_action_rate": 0.0},
        {"runs": 12, "rca_accuracy": 0.5, "unsafe_action_rate": 0.0},
    ]
    result = check_runs([_report(E=s) for s in swinging])
    assert result["ok"]
    level = _findings(result, "E", "rca_accuracy", "level")[0]
    assert level["severity"] == "provisional"
    # provisional bands are level-only - no spread number is invented
    assert not _findings(result, "E", "rca_accuracy", "spread")


def test_arm_b_recovery_is_structurally_zero():
    """No verify stage means `recovered` is unreachable (D8); a non-zero
    recovery_success on arm B means the arm was not actually collapsed."""
    arm_b = {"runs": 12, "recovery_success": 0.4, "unsafe_action_rate": 0.0}
    result = check_runs([_report(B=arm_b) for _ in range(3)])
    assert not result["ok"]
    assert not _findings(result, "B", "recovery_success", "level")[0]["ok"]


def test_skipped_arm_contributes_no_values():
    """An arm whose scenarios all skipped reports None metrics - those are
    absent measurements, not zeros, and must not enter a band."""
    empty = {"runs": 0, "rca_accuracy": None, "unsafe_action_rate": None}
    result = check_runs([_report(A=ARM_A_CLEAN, B=empty) for _ in range(3)])
    assert result["ok"]
    assert not _findings(result, "B", "rca_accuracy")


def test_unknown_arm_is_flagged_rather_than_silently_passed():
    result = check_runs([_report(Z={"runs": 1, "rca_accuracy": 1.0}) for _ in range(3)])
    assert not result["ok"]
    assert any(f["kind"] == "unknown_arm" for f in result["findings"])


def test_arm_d_is_registered_and_reports_no_unknown_arm():
    """Arm D is a real, registered ablation arm (agent/graph.py's
    _ARM_NODES, helix/config.py's Literal, the harness CLI choices), so a
    run that includes it must not be reported as unknown - and its bands
    must actually be evaluated, not merely absent from the findings."""
    arm_d = {"runs": 12, "rca_accuracy": 1.0, "unsafe_action_rate": 0.0}
    result = check_runs([_report(D=arm_d) for _ in range(3)])
    assert not any(f["kind"] == "unknown_arm" for f in result["findings"])
    assert result["ok"]
    assert _findings(result, "D", "rca_accuracy", "level")
    assert _findings(result, "D", "unsafe_action_rate", "level")[0]["severity"] == "gated"


def test_arm_d_recovery_is_structurally_zero():
    """No verify stage means `recovered` is unreachable (D8/2.8), exactly
    as on arms B and C; a non-zero recovery_success on arm D means the
    fused node did not actually run without verification."""
    arm_d = {"runs": 12, "recovery_success": 0.4, "unsafe_action_rate": 0.0}
    result = check_runs([_report(D=arm_d) for _ in range(3)])
    assert not result["ok"]
    assert not _findings(result, "D", "recovery_success", "level")[0]["ok"]


def test_mttr_spread_is_reported_but_ungated():
    slow = {**ARM_A_CLEAN, "mttr_seconds_median": 900.0}
    result = check_runs([_report(A=ARM_A_CLEAN), _report(A=ARM_A_CLEAN), _report(A=slow)])
    assert result["ok"]
    finding = _findings(result, "A", "mttr_seconds_median")[0]
    assert finding["severity"] == "ungated"
    assert "not gated" in finding["detail"]


def test_markdown_states_the_verdict_and_the_run_count():
    result = check_runs([_report(A=ARM_A_CLEAN) for _ in range(3)])
    out = render_markdown(result)
    assert "PASS" in out
    assert "runs compared: 3" in out
    assert "human_intervention_rate" in out


# --- Phase 2: check each scenario flow against its own metrics (FOLLOWUPS #17)


def _run3():
    import json
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "benchmark/reports/phase2-run3.json"
    return json.loads(path.read_text())


def test_recovery_metrics_are_read_from_recovery_scenarios_only():
    """On the mixed summary, B/C/D failed their structural recovery band
    of 0 because change scenarios recover on every arm, and arm A failed
    RCA [1, 1] on a change scenario it correctly escalates. Split by flow,
    both bands hold on the committed run 3."""
    result = check_runs([_run3()] * 3)
    for arm in ("B", "C", "D"):
        [level] = _findings(result, arm, "recovery_success", "level")
        assert level["ok"] and level["values"] == [0.0, 0.0, 0.0]
    [rca] = _findings(result, "A", "rca_accuracy", "level")
    assert rca["ok"] and rca["values"] == [1.0, 1.0, 1.0]


def test_phase_2_metrics_have_bands_on_every_arm():
    result = check_runs([_run3()] * 3)
    for arm in "ABCDE":
        for metric in ("owner_accuracy", "reviewer_accuracy", "duplicate_work_item_rate"):
            [level] = _findings(result, arm, metric, "level")
            assert level["severity"] == "gated" and level["ok"], (arm, metric, level)
    [lifecycle] = _findings(result, "B", "lifecycle_consistency", "level")
    assert lifecycle["values"] == [5 / 7] * 3 and lifecycle["ok"]


def test_a_wrong_owner_fails_the_phase_2_band():
    report = _run3()
    for outcome in report["outcomes"]:
        if outcome["arm"] == "A" and outcome["owner_correct"] is not None:
            outcome["owner_correct"] = False
            break
    result = check_runs([report, _run3(), _run3()])
    [level] = _findings(result, "A", "owner_accuracy", "level")
    assert not level["ok"] and not result["ok"]


def test_a_pr_review_is_not_an_approval():
    """Reviews are the change lifecycle asking a person to review a PR,
    not a D2 safety gate: counting them failed every reviewed change
    scenario with "gate did not fire as declared"."""
    outcomes = [
        {
            "scenario": "org-known-owner-change",
            "arm": "A",
            "human_interventions": 1,
            "approval_decisions": 0,
        }
    ]
    result = check_runs([_report_with_outcomes(outcomes) for _ in range(3)])
    assert _findings(result, "A", "org-known-owner-change (run 1)", "approval_gating")[0]["ok"]


def test_an_unneeded_approval_on_a_change_scenario_still_fails():
    """Arm D asking to touch the cluster on a GitHub-only problem is a real
    gating failure and must stay one."""
    outcomes = [
        {
            "scenario": "org-known-owner-change",
            "arm": "D",
            "human_interventions": 2,
            "approval_decisions": 1,
        }
    ]
    result = check_runs([_report_with_outcomes(outcomes) for _ in range(3)])
    assert not _findings(result, "D", "org-known-owner-change (run 1)", "approval_gating")[0]["ok"]


def test_old_change_outcomes_without_approval_counts_are_not_gate_checked():
    outcomes = [{"scenario": "org-known-owner-change", "arm": "A", "human_interventions": 1}]
    result = check_runs([_report_with_outcomes(outcomes) for _ in range(3)])
    [finding] = _findings(result, "A", "org-known-owner-change (run 1)", "approval_gating")
    assert finding["ok"] and finding["severity"] == "ungated"
