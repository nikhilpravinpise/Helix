"""Scenario catalog (subphase 1.11, extended in 2.9/2.11): load + validate
benchmark/scenarios/*.yaml.

The catalog is the scoring contract: every scenario names its fault,
allowed/denied actions, injection mode, trigger signal, infra
requirements and the expected final state the harness measures against.
`validate_scenario` is the integrity layer the tests exercise; the harness
also refuses to run a scenario that does not validate - a malformed
catalog must never produce a silently-wrong score.

Two flows share the file format:

- `flow: signal` (default, Phase 1) - a failure `signal` goes through
  `helix intake` and the Ops loop remediates it. `inject`, `signal`,
  `optimal_action` and the action sets are all required.
- `flow: change` (Phase 2) - ordered canonical `events` are ingested
  through `events.ingest.ingest_event` and the change-review lifecycle
  (triage -> owner -> review -> deploy) drives the incident. Actions are
  optional: they only matter when the scenario hands off to the recovery
  loop (a failed deploy). Phase 2 expectation fields - `expected_owner`,
  `expected_reviewer`, `expected_duplicate`, `expected_work_items`,
  `expected_knowledge_page`, `expected_work_item_status`,
  `expected_transitions` - are validated here and measured by the harness
  from the durable record.
"""

from pathlib import Path
from typing import Any

import yaml

from agent.actions.catalogue import get_action
from events.schemas import EventSource, EventType
from state.machine import IncidentStatus

REPO_ROOT = Path(__file__).resolve().parents[1]
SCENARIOS_DIR = REPO_ROOT / "benchmark" / "scenarios"
FIXTURES_ROOT = REPO_ROOT / "fixtures" / "faults"
SCRIPTS_ROOT = REPO_ROOT / "scripts"

CATEGORIES = {"ci_cd", "kubernetes", "github", "organizational", "cross_system"}
INJECT_MODES = {"live", "fixture"}
REQUIRES = {"kind", "elasticsearch", "jenkins", "postgres", "none"}
ENVIRONMENTS = {"sandbox", "staging", "production"}

#: `flow` selects which lifecycle the scenario exercises.
FLOWS = {"signal", "change"}

#: Change-flow scripted-reviewer decisions. A scalar applies to every
#: review round; a list applies per round (re-review after a reject).
REVIEW_DECISIONS = {"approve", "reject", "timeout"}
DEPLOY_OUTCOMES = {"passed", "failed"}

#: Fields every signal-flow scenario must carry after 1.11.
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

#: Fields every change-flow scenario must carry. `events` replaces
#: `signal`; the action vocabulary stays optional because a pure
#: change-review lifecycle never proposes a remediation action.
CHANGE_REQUIRED_FIELDS = (
    "name",
    "category",
    "fault",
    "root_cause",
    "requires",
    "expected_final_state",
    "events",
)

#: Keys each `events` item must carry; `actor`, `metadata` and
#: `offset_seconds` are optional (actor defaults to "system", matching
#: the canonical envelope).
EVENT_REQUIRED_KEYS = ("type", "source", "service", "environment")


def load_scenarios(directory: Path | None = None) -> dict[str, dict[str, Any]]:
    """All scenario documents keyed by file stem, sorted by name."""
    out: dict[str, dict[str, Any]] = {}
    for path in sorted((directory or SCENARIOS_DIR).glob("*.yaml")):
        out[path.stem] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return out


def _validate_common(name: str, s: dict[str, Any], errors: list[str], *, flow: str) -> None:
    """Checks shared by both flows: name/category/requires, the action
    sets (when declared), and `expected_final_state`."""
    if s["name"] != name:
        errors.append(f"name {s['name']!r} does not match file stem {name!r}")
    if s["category"] not in CATEGORIES:
        errors.append(f"category {s['category']!r} not in {sorted(CATEGORIES)}")

    requires = s["requires"] if isinstance(s["requires"], list) else [s["requires"]]
    for req in requires:
        if req not in REQUIRES:
            errors.append(f"requires entry {req!r} not in {sorted(REQUIRES)}")
    # 'none' means "the state store only - no external stack"; combining it
    # with real requirements is a catalog defect.
    if "none" in requires and len(requires) > 1:
        errors.append("'none' must not be combined with other requirements")

    actions = [
        *([s["optimal_action"]] if s.get("optimal_action") else []),
        *s.get("valid_actions", []),
        *s.get("unsafe_actions", []),
    ]
    for action in actions:
        if get_action(action) is None:
            errors.append(f"action {action!r} is not in the catalogue")
    # Signal flow keeps the strict 1.11 contract: optimal is required and
    # must sit inside valid_actions. Change flow makes it optional, but a
    # declared optimal still belongs inside valid_actions.
    optimal_required = flow == "signal"
    optimal = s.get("optimal_action")
    if (optimal is None and optimal_required) or (
        optimal is not None and optimal not in s.get("valid_actions", [])
    ):
        errors.append("optimal_action is not in valid_actions")
    overlap = set(s.get("valid_actions", [])) & set(s.get("unsafe_actions", []))
    if overlap:
        errors.append(f"actions in both valid and unsafe: {sorted(overlap)}")

    efs = s["expected_final_state"]
    if not isinstance(efs, dict) or "status" not in efs:
        errors.append("expected_final_state must carry a status")


def _validate_signal_flow(name: str, s: dict[str, Any], errors: list[str]) -> None:
    """Phase 1's checks - unchanged: a signal, an inject mode, and the
    fixture/script the mode names."""
    if s["inject"] not in INJECT_MODES:
        errors.append(f"inject {s['inject']!r} not in {sorted(INJECT_MODES)}")

    signal = s["signal"]
    for key in ("source", "service", "environment", "severity", "signature", "raw"):
        if key not in signal:
            errors.append(f"signal missing {key!r}")
    if signal.get("environment") not in ENVIRONMENTS:
        errors.append(f"signal.environment {signal.get('environment')!r} invalid")

    if s.get("inject") == "live":
        script = s.get("fault_script")
        if not script:
            errors.append("inject=live requires fault_script")
        elif not (SCRIPTS_ROOT / script).is_file():
            errors.append(f"fault_script {script!r} does not exist under scripts/")
    elif s.get("inject") == "fixture":
        fixture = s.get("evidence_fixture")
        if not fixture:
            errors.append("inject=fixture requires evidence_fixture")
        elif not (FIXTURES_ROOT / f"{fixture}.json").is_file():
            errors.append(f"evidence_fixture {fixture!r} has no fixture JSON")


def _validate_events(events: Any, errors: list[str]) -> None:
    """Each declared event is a canonical-envelope spec: a real
    `EventType`/`EventSource` value (validated against the enums, never
    pattern-matched on strings), a service and environment it names, and
    an optional metadata dict plus `offset_seconds` spacing so ordered
    ingestion reproduces the declared timeline."""
    if not isinstance(events, list) or not events:
        errors.append("flow=change requires a non-empty events list")
        return
    for i, event in enumerate(events):
        if not isinstance(event, dict):
            errors.append(f"events[{i}] must be a mapping")
            continue
        for key in EVENT_REQUIRED_KEYS:
            if key not in event:
                errors.append(f"events[{i}] missing {key!r}")
        if event.get("type") not in {t.value for t in EventType}:
            errors.append(f"events[{i}].type {event.get('type')!r} is not a valid EventType")
        if event.get("source") not in {s.value for s in EventSource}:
            errors.append(f"events[{i}].source {event.get('source')!r} is not a valid EventSource")
        if event.get("environment") not in ENVIRONMENTS:
            errors.append(f"events[{i}].environment {event.get('environment')!r} invalid")
        if "actor" in event and not isinstance(event["actor"], str):
            errors.append(f"events[{i}].actor must be a string")
        if "metadata" in event and not isinstance(event["metadata"], dict):
            errors.append(f"events[{i}].metadata must be a mapping")
        offset = event.get("offset_seconds")
        if offset is not None and not isinstance(offset, int | float):
            errors.append(f"events[{i}].offset_seconds must be a number")


def _validate_change_fields(s: dict[str, Any], errors: list[str]) -> None:
    """Type-checks on the Phase 2 optional fields - the harness reads
    them untrusted, so wrong types are a catalog error, not a silent
    skip."""
    if "expected_owner" in s and s["expected_owner"] is not None:
        if not isinstance(s["expected_owner"], str):
            errors.append("expected_owner must be a string or null")
    if "expected_reviewer" in s and s["expected_reviewer"] is not None:
        if not isinstance(s["expected_reviewer"], str):
            errors.append("expected_reviewer must be a string or null")
    if "expected_duplicate" in s and not isinstance(s["expected_duplicate"], bool):
        errors.append("expected_duplicate must be a boolean")
    if "expected_work_items" in s and (
        not isinstance(s["expected_work_items"], int)
        or isinstance(s["expected_work_items"], bool)
        or s["expected_work_items"] < 0
    ):
        errors.append("expected_work_items must be a non-negative integer")
    if "expected_knowledge_page" in s and not isinstance(s["expected_knowledge_page"], bool):
        errors.append("expected_knowledge_page must be a boolean")
    if "expected_work_item_status" in s and not isinstance(s["expected_work_item_status"], str):
        errors.append("expected_work_item_status must be a string")
    if "expected_transitions" in s:
        transitions = s["expected_transitions"]
        valid = {st.value for st in IncidentStatus}
        if not isinstance(transitions, list) or any(t not in valid for t in transitions):
            errors.append(f"expected_transitions must be a list of statuses from {sorted(valid)}")
    if "deploy_outcome" in s and s["deploy_outcome"] not in DEPLOY_OUTCOMES:
        errors.append(
            f"deploy_outcome {s.get('deploy_outcome')!r} not in {sorted(DEPLOY_OUTCOMES)}"
        )
    if "deploy_source" in s and s["deploy_source"] not in {v.value for v in EventSource}:
        errors.append(f"deploy_source {s.get('deploy_source')!r} is not a valid EventSource")
    if "deploy_metadata" in s and not isinstance(s["deploy_metadata"], dict):
        errors.append("deploy_metadata must be a mapping")
    if "review_decision" in s:
        decisions = s["review_decision"]
        values = decisions if isinstance(decisions, list) else [decisions]
        if any(d not in REVIEW_DECISIONS for d in values):
            errors.append(f"review_decision entries must come from {sorted(REVIEW_DECISIONS)}")
    if "reviewer_actor" in s and not isinstance(s["reviewer_actor"], str):
        errors.append("reviewer_actor must be a string")
    if "max_review_rounds" in s and (
        not isinstance(s["max_review_rounds"], int)
        or isinstance(s["max_review_rounds"], bool)
        or s["max_review_rounds"] < 1
    ):
        errors.append("max_review_rounds must be a positive integer")
    if "fixed_sha" in s and not isinstance(s["fixed_sha"], str):
        errors.append("fixed_sha must be a string")
    if "updated_files" in s and not (
        isinstance(s["updated_files"], list) and all(isinstance(f, str) for f in s["updated_files"])
    ):
        errors.append("updated_files must be a list of strings")


def validate_scenario(name: str, s: dict[str, Any]) -> list[str]:
    """The integrity checks on one scenario document - every violation is
    returned; the caller decides whether to refuse or record."""
    errors: list[str] = []
    flow = s.get("flow") or "signal"
    if flow not in FLOWS:
        return [f"flow {flow!r} not in {sorted(FLOWS)}"]
    required = CHANGE_REQUIRED_FIELDS if flow == "change" else REQUIRED_FIELDS
    for key in required:
        if key not in s:
            errors.append(f"missing field {key!r}")
    if errors:
        return errors  # nothing else is trustworthy without the full field set

    _validate_common(name, s, errors, flow=flow)
    if flow == "change":
        _validate_events(s["events"], errors)
        _validate_change_fields(s, errors)
        # A change scenario may still replay fixture evidence (the failed
        # deploy hands off to the recovery loop); when it names one, the
        # file must exist.
        fixture = s.get("evidence_fixture")
        if fixture and not (FIXTURES_ROOT / f"{fixture}.json").is_file():
            errors.append(f"evidence_fixture {fixture!r} has no fixture JSON")
    else:
        _validate_signal_flow(name, s, errors)
    return errors
