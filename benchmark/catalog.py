"""Scenario catalog (subphase 1.11): load + validate benchmark/scenarios/*.yaml.

The catalog is the scoring contract: every scenario names its fault,
allowed/denied actions, injection mode, trigger signal, infra
requirements and the expected final state the harness measures against.
`validate_scenario` is the integrity layer the tests exercise; the harness
also refuses to run a scenario that does not validate - a malformed
catalog must never produce a silently-wrong score.
"""

from pathlib import Path
from typing import Any

import yaml

from agent.actions.catalogue import get_action

REPO_ROOT = Path(__file__).resolve().parents[1]
SCENARIOS_DIR = REPO_ROOT / "benchmark" / "scenarios"
FIXTURES_ROOT = REPO_ROOT / "fixtures" / "faults"
SCRIPTS_ROOT = REPO_ROOT / "scripts"

CATEGORIES = {"ci_cd", "kubernetes", "github"}
INJECT_MODES = {"live", "fixture"}
REQUIRES = {"kind", "elasticsearch", "jenkins", "postgres", "none"}

#: Fields every scenario must carry after 1.11.
REQUIRED_FIELDS = (
    "name",
    "category",
    "fault",
    "optimal_action",
    "valid_actions",
    "unsafe_actions",
    "approval_gated",
    "root_cause",
    "inject",
    "signal",
    "requires",
    "expected_final_state",
)


def load_scenarios(directory: Path | None = None) -> dict[str, dict[str, Any]]:
    """All scenario documents keyed by file stem, sorted by name."""
    out: dict[str, dict[str, Any]] = {}
    for path in sorted((directory or SCENARIOS_DIR).glob("*.yaml")):
        out[path.stem] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return out


def validate_scenario(name: str, s: dict[str, Any]) -> list[str]:
    """The integrity checks on one scenario document - every violation is
    returned; the caller decides whether to refuse or record."""
    errors: list[str] = []
    for key in REQUIRED_FIELDS:
        if key not in s:
            errors.append(f"missing field {key!r}")
    if errors:
        return errors  # nothing else is trustworthy without the full field set

    if s["name"] != name:
        errors.append(f"name {s['name']!r} does not match file stem {name!r}")
    if s["category"] not in CATEGORIES:
        errors.append(f"category {s['category']!r} not in {sorted(CATEGORIES)}")
    if s["inject"] not in INJECT_MODES:
        errors.append(f"inject {s['inject']!r} not in {sorted(INJECT_MODES)}")

    requires = s["requires"] if isinstance(s["requires"], list) else [s["requires"]]
    for req in requires:
        if req not in REQUIRES:
            errors.append(f"requires entry {req!r} not in {sorted(REQUIRES)}")
    # 'none' means "the state store only - no external stack"; combining it
    # with real requirements is a catalog defect.
    if "none" in requires and len(requires) > 1:
        errors.append("'none' must not be combined with other requirements")

    signal = s["signal"]
    for key in ("source", "service", "environment", "severity", "signature", "raw"):
        if key not in signal:
            errors.append(f"signal missing {key!r}")
    if signal.get("environment") not in ("sandbox", "staging", "production"):
        errors.append(f"signal.environment {signal.get('environment')!r} invalid")

    actions = [s["optimal_action"], *s.get("valid_actions", []), *s.get("unsafe_actions", [])]
    for action in actions:
        if get_action(action) is None:
            errors.append(f"action {action!r} is not in the catalogue")
    if s["optimal_action"] not in s.get("valid_actions", []):
        errors.append("optimal_action is not in valid_actions")
    overlap = set(s.get("valid_actions", [])) & set(s.get("unsafe_actions", []))
    if overlap:
        errors.append(f"actions in both valid and unsafe: {sorted(overlap)}")

    if s["inject"] == "live":
        script = s.get("fault_script")
        if not script:
            errors.append("inject=live requires fault_script")
        elif not (SCRIPTS_ROOT / script).is_file():
            errors.append(f"fault_script {script!r} does not exist under scripts/")
    else:
        fixture = s.get("evidence_fixture")
        if not fixture:
            errors.append("inject=fixture requires evidence_fixture")
        elif not (FIXTURES_ROOT / f"{fixture}.json").is_file():
            errors.append(f"evidence_fixture {fixture!r} has no fixture JSON")

    efs = s["expected_final_state"]
    if not isinstance(efs, dict) or "status" not in efs:
        errors.append("expected_final_state must carry a status")
    return errors
