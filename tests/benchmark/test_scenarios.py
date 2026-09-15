"""Scenario-file consistency tests (subphase 1.6).

benchmark/scenarios/*.yaml is the contract 1.11 scores against: every
action name in optimal/valid/unsafe must be a real catalogue action,
optimal must sit inside valid, and the sets must not overlap. Also pins
the catalogue's secret-manifest set against the real files' `kind:`
fields, so the param-dependent high tier cannot silently drift.
"""

from pathlib import Path

import pytest
import yaml

from agent.actions.catalogue import (
    MANIFEST_ALLOWLIST,
    SECRET_MANIFEST_PATHS,
    get_action,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SCENARIOS_DIR = REPO_ROOT / "benchmark" / "scenarios"
CATEGORIES = {"ci_cd", "kubernetes", "github"}


def _load_all() -> dict[str, dict]:
    out = {}
    for path in sorted(SCENARIOS_DIR.glob("*.yaml")):
        out[path.stem] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return out


SCENARIOS = _load_all()


def test_twelve_scenarios_covering_the_phase1_categories():
    assert set(SCENARIOS) == {
        "ci-dependency-failure",
        "ci-test-regression",
        "ci-docker-build-failure",
        "config-mismatch",
        "crashloop",
        "oom-killed",
        "failed-rollout",
        "missing-secret",
        "bad-deployment-config",
        "rejected-pr",
        "failed-check",
        "stale-branch",
    }
    # 4 per category, matching CONTEXT.md section 18's Phase 1 spread.
    by_cat: dict[str, int] = {}
    for s in SCENARIOS.values():
        by_cat[s["category"]] = by_cat.get(s["category"], 0) + 1
    assert by_cat == {"ci_cd": 4, "kubernetes": 4, "github": 4}


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_action_sets_reference_only_catalogue_actions(name):
    s = SCENARIOS[name]
    assert s["category"] in CATEGORIES
    assert isinstance(s["approval_gated"], bool)
    for key in ("optimal_action", "valid_actions", "unsafe_actions"):
        assert key in s, f"{name}: missing {key}"
    for action in [s["optimal_action"], *s["valid_actions"], *s["unsafe_actions"]]:
        assert get_action(action) is not None, (
            f"{name}: {action} is not a registered catalogue action"
        )
    assert s["optimal_action"] in s["valid_actions"], (
        f"{name}: optimal {s['optimal_action']} must sit inside valid_actions"
    )
    overlap = set(s["valid_actions"]) & set(s["unsafe_actions"])
    assert not overlap, f"{name}: actions in both valid and unsafe: {overlap}"


def test_approval_gated_scenarios_have_high_tier_optimal():
    """Gating must fall out of the access table, not the scenario's say-so:
    an approval_gated scenario's optimal action is one whose params can
    reach `high` (today: the secret manifest)."""
    for name, s in SCENARIOS.items():
        spec = get_action(s["optimal_action"])
        assert spec is not None
        if s["approval_gated"]:
            assert s["optimal_action"] == "reapply_manifest", (
                f"{name}: gated scenario's optimal should be the "
                "secret-manifest apply (the only high-tier path)"
            )
        else:
            assert s["optimal_action"] != "reapply_manifest" or name in (
                "crashloop",
                "missing-secret",
            ), f"{name}: secret-manifest apply should be approval_gated"


def test_secret_manifest_set_matches_real_file_kinds():
    """`SECRET_MANIFEST_PATHS` must contain exactly the allowlisted files
    whose kind is Secret - the param-dependent high tier reads it."""
    kinds: dict[str, set[str]] = {}
    for rel_path in MANIFEST_ALLOWLIST:
        docs = yaml.safe_load_all((REPO_ROOT / rel_path).read_text(encoding="utf-8"))
        kinds[rel_path] = {d["kind"] for d in docs if d}
    secrets = {p for p, ks in kinds.items() if "Secret" in ks}
    assert secrets == SECRET_MANIFEST_PATHS
