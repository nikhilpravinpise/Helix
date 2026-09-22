"""Direct-vs-event benchmark comparison (subphase E7).

Mirrors the honest-reporting conventions `tests/benchmark/test_tolerance.py`
already covers for the tolerance checker: a missing metric is never
counted as a pass, and only a comparable metric can move the verdict.
"""

import json

import pytest

from benchmark.compare import compare_reports, main, render_markdown

ARM_A_DIRECT = {
    "rca_accuracy": 1.0,
    "action_validity": 1.0,
    "recovery_success": 1.0,
    "measured_state_match": 1.0,
    "unsafe_action_rate": 0.0,
    # Phase 2 (2.9/2.11): change-lifecycle metrics - a real report's
    # summary emits every one of these keys, so the fixture must too.
    "owner_accuracy": 1.0,
    "reviewer_accuracy": 1.0,
    "lifecycle_consistency": 1.0,
    "duplicate_work_item_rate": 0.0,
    "decision_evidence_coverage": 1.0,
}


def _report(generated_at="2026-09-19T00:00:00+00:00", **arms):
    return {"generated_at": generated_at, "summary": dict(arms)}


def test_identical_summaries_are_no_regression():
    direct = _report(A=ARM_A_DIRECT)
    event = _report(A=dict(ARM_A_DIRECT))
    result = compare_reports(direct, event)
    assert not result.regressed
    assert result.incomparable_count == 0
    assert all(c.comparable for c in result.comparisons)
    assert all(not c.regressed for c in result.comparisons)


def test_lower_rca_accuracy_on_event_path_is_a_regression():
    direct = _report(A=ARM_A_DIRECT)
    event = _report(A={**ARM_A_DIRECT, "rca_accuracy": 11 / 12})
    result = compare_reports(direct, event)
    assert result.regressed
    hit = next(c for c in result.comparisons if c.arm == "A" and c.metric == "rca_accuracy")
    assert hit.regressed
    assert hit.delta == pytest.approx(11 / 12 - 1.0)


def test_higher_unsafe_action_rate_on_event_path_is_a_regression():
    """unsafe_action_rate is lower-is-better - direction is inverted from
    the other four metrics."""
    direct = _report(A=ARM_A_DIRECT)
    event = _report(A={**ARM_A_DIRECT, "unsafe_action_rate": 0.05})
    result = compare_reports(direct, event)
    assert result.regressed
    hit = next(c for c in result.comparisons if c.metric == "unsafe_action_rate")
    assert hit.regressed
    assert hit.delta == pytest.approx(0.05)


def test_higher_recovery_success_on_event_path_is_not_a_regression():
    """Movement in the GOOD direction must never trip the verdict."""
    direct = _report(A={**ARM_A_DIRECT, "recovery_success": 0.8})
    event = _report(A={**ARM_A_DIRECT, "recovery_success": 1.0})
    result = compare_reports(direct, event)
    assert not result.regressed
    hit = next(c for c in result.comparisons if c.metric == "recovery_success")
    assert not hit.regressed
    assert hit.delta == pytest.approx(0.2)


def test_metric_missing_on_one_side_is_incomparable_not_a_pass():
    """A None on either side must never silently count as a match - it is
    its own, separately counted, outcome."""
    direct = _report(A=ARM_A_DIRECT)
    event = _report(A={**ARM_A_DIRECT, "rca_accuracy": None})
    result = compare_reports(direct, event)
    assert not result.regressed  # an incomparable metric cannot regress
    assert result.incomparable_count == 1
    hit = next(c for c in result.comparisons if c.metric == "rca_accuracy")
    assert not hit.comparable
    assert hit.delta is None
    assert "direct path" not in hit.detail  # missing on the EVENT side
    assert "not measured on the event path" in hit.detail


def test_metric_missing_on_both_sides_is_incomparable():
    direct = _report(A={**ARM_A_DIRECT, "rca_accuracy": None})
    event = _report(A={**ARM_A_DIRECT, "rca_accuracy": None})
    result = compare_reports(direct, event)
    assert result.incomparable_count == 1
    hit = next(c for c in result.comparisons if c.metric == "rca_accuracy")
    assert "not measured on either path" in hit.detail


def test_arm_present_on_only_one_side_is_reported_incomparable():
    """An arm that only ran on one path (e.g. arm E with a key on one
    machine, not the other) still appears - every one of its metrics is
    incomparable rather than dropped from the report entirely."""
    direct = _report(A=ARM_A_DIRECT)
    event = _report(A=dict(ARM_A_DIRECT), E={**ARM_A_DIRECT, "rca_accuracy": 0.5})
    result = compare_reports(direct, event)
    assert "E" in result.arms
    e_comparisons = [c for c in result.comparisons if c.arm == "E"]
    assert e_comparisons
    assert all(not c.comparable for c in e_comparisons)
    assert not result.regressed


def test_empty_summaries_produce_no_arms_and_no_regression():
    result = compare_reports(_report(), _report())
    assert result.arms == []
    assert result.comparisons == []
    assert not result.regressed


def test_render_markdown_states_the_verdict():
    direct = _report(A=ARM_A_DIRECT)
    event = _report(A={**ARM_A_DIRECT, "rca_accuracy": 0.5})
    result = compare_reports(direct, event)
    out = render_markdown(direct, event, result)
    assert "REGRESSED" in out
    assert "rca_accuracy" in out
    assert "A" in out


def test_render_markdown_states_no_regression():
    direct = _report(A=ARM_A_DIRECT)
    event = _report(A=dict(ARM_A_DIRECT))
    result = compare_reports(direct, event)
    out = render_markdown(direct, event, result)
    assert "NO REGRESSION" in out


def test_main_exits_zero_on_no_regression(tmp_path, capsys):
    direct_path = tmp_path / "direct.json"
    event_path = tmp_path / "event.json"
    direct_path.write_text(json.dumps(_report(A=ARM_A_DIRECT)))
    event_path.write_text(json.dumps(_report(A=dict(ARM_A_DIRECT))))

    code = main([str(direct_path), str(event_path)])
    assert code == 0
    out = capsys.readouterr().out
    assert "NO REGRESSION" in out


def test_main_exits_nonzero_on_regression(tmp_path, capsys):
    direct_path = tmp_path / "direct.json"
    event_path = tmp_path / "event.json"
    direct_path.write_text(json.dumps(_report(A=ARM_A_DIRECT)))
    event_path.write_text(json.dumps(_report(A={**ARM_A_DIRECT, "recovery_success": 0.0})))

    code = main([str(direct_path), str(event_path)])
    assert code == 1
    out = capsys.readouterr().out
    assert "REGRESSED" in out


def test_main_json_output_is_parseable(tmp_path, capsys):
    direct_path = tmp_path / "direct.json"
    event_path = tmp_path / "event.json"
    direct_path.write_text(json.dumps(_report(A=ARM_A_DIRECT)))
    event_path.write_text(json.dumps(_report(A=dict(ARM_A_DIRECT))))

    code = main([str(direct_path), str(event_path), "--json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["regressed"] is False
    assert payload["arms"] == ["A"]
