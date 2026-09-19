"""Harness orchestration tests (subphase 1.11).

Unit tier: requirement checks, measurement helpers, and the honest skip
paths - no subprocess runs. The real end-to-end (`helix intake` + `helix
run` subprocesses against Postgres) is the integration tier.
"""

import pytest

from benchmark import catalog
from benchmark.harness import (
    HarnessConfig,
    _collect,
    _measure,
    check_requirements,
    run_scenario,
)

SCENARIOS = catalog.load_scenarios()
BAD_URL = "postgresql+psycopg://helix:helix@127.0.0.1:59999/helix"


def test_check_requirements_flags_missing_postgres():
    unmet = check_requirements([], BAD_URL)
    assert "postgres" in unmet


def test_run_scenario_skips_on_unmet_requirements():
    """No Postgres -> an honest skip record, never a fabricated run."""
    cfg = HarnessConfig(arms=["A"], database_url=BAD_URL)
    record = run_scenario("ci-test-regression", SCENARIOS["ci-test-regression"], "A", cfg)
    assert record.skipped is not None
    assert "postgres" in record.skipped
    assert record.final_status is None


def test_arm_b_and_e_skip_without_llm_key(monkeypatch):
    monkeypatch.delenv("HELIX_LLM_API_KEY", raising=False)
    cfg = HarnessConfig(arms=["B"], database_url=BAD_URL)
    record = run_scenario("ci-test-regression", SCENARIOS["ci-test-regression"], "B", cfg)
    assert record.skipped == "arm B requires HELIX_LLM_API_KEY"


def test_malformed_scenario_is_a_catalog_defect_not_a_run():
    cfg = HarnessConfig(arms=["A"], database_url=BAD_URL)
    record = run_scenario("bogus", {"name": "bogus"}, "A", cfg)
    assert record.skipped == "catalog invalid"
    assert record.errors


def _execution_row(**overrides):
    """An executions row shaped exactly as `_collect` builds one.

    Deliberately no `detail` key: the Execution model has no such column,
    so a fabricated one here hid the reporting bug that printed "no
    successful executions row" underneath every passing check.
    """
    row = {
        "action": "open_fix_pr",
        "result": "success",
        "policy_decision": "allow_with_checks",
        "approval_id": None,
        "decided_by": None,
        "compensates_id": None,
        "origin": "fixture",
        "output": {
            "branch": "helix/INC-x",
            "files": ["sandbox/payment-api/tests/test_main.py"],
        },
    }
    return {**row, **overrides}


def test_measure_fixture_mode_checks_the_recorded_write():
    """Fixture measurement reads the recorded intended write: the right
    action succeeded and its output carries the declared content."""
    record = {"executions": [_execution_row()]}
    scenario = SCENARIOS["ci-test-regression"]
    checks = _measure(HarnessConfig(), scenario, record)
    assert all(c["ok"] for c in checks)
    assert any("executed_action" in c["check"] for c in checks)
    assert any("output_contains" in c["check"] for c in checks)


def test_measure_flags_a_wrong_write():
    record = {"executions": []}
    checks = _measure(HarnessConfig(), SCENARIOS["ci-test-regression"], record)
    assert any(not c["ok"] for c in checks)


def test_passing_executed_action_check_reports_the_row_it_matched():
    """A passing check must not carry the not-found string as evidence -
    the committed arm-A report did exactly that for all 12 scenarios."""
    record = {"executions": [_execution_row()]}
    checks = _measure(HarnessConfig(), SCENARIOS["ci-test-regression"], record)
    executed = next(c for c in checks if c["check"].startswith("executed_action"))
    assert executed["ok"]
    assert "no successful executions row" not in executed["detail"]
    assert "open_fix_pr" in executed["detail"]
    assert "result=success" in executed["detail"]


def test_failing_executed_action_check_says_the_row_is_missing():
    checks = _measure(HarnessConfig(), SCENARIOS["ci-test-regression"], {"executions": []})
    executed = next(c for c in checks if c["check"].startswith("executed_action"))
    assert not executed["ok"]
    assert executed["detail"] == "no successful executions row"


def test_executed_action_ignores_failed_and_compensating_rows():
    """Only a successful, non-compensating row counts - and when none
    qualifies the detail must say so rather than describe a rejected row."""
    record = {
        "executions": [
            _execution_row(result="failure"),
            _execution_row(compensates_id=7),
        ]
    }
    checks = _measure(HarnessConfig(), SCENARIOS["ci-test-regression"], record)
    executed = next(c for c in checks if c["check"].startswith("executed_action"))
    assert not executed["ok"]
    assert executed["detail"] == "no successful executions row"


@pytest.mark.integration
class TestHarnessEndToEnd:
    """Real `helix intake`/`helix run` subprocesses against the compose
    Postgres (D9 integration tier): the fixture-mode arm-A path through
    the actual CLI, not an in-process shortcut."""

    def test_fixture_arm_a_recovers_through_the_real_cli(self):
        cfg = HarnessConfig(arms=["A"], scenarios=["ci-test-regression"])
        record = run_scenario("ci-test-regression", SCENARIOS["ci-test-regression"], "A", cfg)
        assert record.skipped is None
        assert record.final_status == "recovered"
        assert record.selected_action == "open_fix_pr"
        # The durable record exists: a successful fixture write row and a
        # passing verification row, both readable back from Postgres.
        assert any(
            e["action"] == "open_fix_pr" and e["result"] == "success" and e["origin"] == "fixture"
            for e in record.executions
        )
        assert any(v["outcome"] == "pass" for v in record.verifications)
        assert record.recovered_at is not None

    def test_collect_reads_the_durable_record(self):
        from helix.config import get_settings

        cfg = HarnessConfig(arms=["A"], scenarios=["ci-test-regression"])
        record = run_scenario("ci-test-regression", SCENARIOS["ci-test-regression"], "A", cfg)
        assert record.incident_id is not None
        collected = _collect(get_settings().database_url, record.incident_id)
        assert collected["final_status"] == "recovered"
