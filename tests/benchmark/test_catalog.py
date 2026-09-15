"""Scenario catalog integrity (subphase 1.11): the 1.11 field set the
harness drives on - inject mode, signal shape, requires, expected final
state - plus the validation layer's unhappy paths."""

import pytest

from benchmark import catalog

SCENARIOS = catalog.load_scenarios()


def test_all_twelve_scenarios_validate_clean():
    failures = {name: catalog.validate_scenario(name, s) for name, s in SCENARIOS.items()}
    assert {k: v for k, v in failures.items() if v} == {}


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_1_11_fields_present(name):
    s = SCENARIOS[name]
    for field in catalog.REQUIRED_FIELDS:
        assert field in s, f"{name}: missing 1.11 field {field!r}"
    # Signal carries the intake payload the harness posts verbatim.
    for key in ("source", "service", "environment", "severity", "signature", "raw"):
        assert key in s["signal"], f"{name}: signal missing {key!r}"


def test_inject_mode_split_is_six_live_six_fixture():
    live = [n for n, s in SCENARIOS.items() if s["inject"] == "live"]
    fixture = [n for n, s in SCENARIOS.items() if s["inject"] == "fixture"]
    assert sorted(live) == [
        "bad-deployment-config",
        "config-mismatch",
        "crashloop",
        "failed-rollout",
        "missing-secret",
        "oom-killed",
    ]
    assert sorted(fixture) == [
        "ci-dependency-failure",
        "ci-docker-build-failure",
        "ci-test-regression",
        "failed-check",
        "rejected-pr",
        "stale-branch",
    ]


def test_fixture_scenarios_point_at_real_fixtures():
    for name, s in SCENARIOS.items():
        if s["inject"] == "fixture":
            path = catalog.FIXTURES_ROOT / f"{s['evidence_fixture']}.json"
            assert path.is_file(), f"{name}: {path} missing"


def test_live_scenarios_point_at_real_fault_scripts():
    for name, s in SCENARIOS.items():
        if s["inject"] == "live":
            path = catalog.SCRIPTS_ROOT / s["fault_script"]
            assert path.is_file(), f"{name}: {path} missing"


# --- validate_scenario unhappy paths -----------------------------------------


def _base() -> dict:
    return {
        "name": "x",
        "category": "ci_cd",
        "fault": "x",
        "optimal_action": "open_fix_pr",
        "valid_actions": ["open_fix_pr"],
        "unsafe_actions": ["restart_deployment"],
        "approval_gated": False,
        "root_cause": "x",
        "inject": "fixture",
        "evidence_fixture": "ci/test-regression",
        "signal": {
            "source": "jenkins",
            "service": "payment-api",
            "environment": "sandbox",
            "severity": "high",
            "signature": {"job": "payment-api"},
            "raw": {},
        },
        "requires": ["postgres"],
        "expected_final_state": {"status": "recovered"},
    }


def test_validate_catches_missing_fields():
    doc = _base()
    del doc["signal"], doc["expected_final_state"]
    errors = catalog.validate_scenario("x", doc)
    assert any("signal" in e for e in errors)
    assert any("expected_final_state" in e for e in errors)


def test_validate_catches_non_catalogue_action():
    doc = _base()
    doc["valid_actions"].append("rm_rf_production")
    assert any("rm_rf_production" in e for e in catalog.validate_scenario("x", doc))


def test_validate_catches_valid_unsafe_overlap():
    doc = _base()
    doc["unsafe_actions"].append("open_fix_pr")
    assert any("both" in e for e in catalog.validate_scenario("x", doc))


def test_validate_catches_missing_fixture_file():
    doc = _base()
    doc["evidence_fixture"] = "ci/does-not-exist"
    assert any("fixture" in e for e in catalog.validate_scenario("x", doc))


def test_validate_catches_missing_fault_script():
    doc = _base()
    doc["inject"] = "live"
    doc["fault_script"] = "faults/does-not-exist.sh"
    del doc["evidence_fixture"]
    assert any("fault_script" in e for e in catalog.validate_scenario("x", doc))


def test_validate_catches_optimal_outside_valid():
    doc = _base()
    doc["optimal_action"] = "rerun_pipeline"
    assert any("optimal" in e for e in catalog.validate_scenario("x", doc))
