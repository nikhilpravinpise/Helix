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
