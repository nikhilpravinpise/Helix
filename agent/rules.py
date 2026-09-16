"""Arm A (decision D8): deterministic rules instead of LLM reasoning.

The ablation's honest no-key baseline: a hand-authored table mapping
fault signatures to a canned hypothesis claim plus an ORDERED action
list. `rules_diagnosis` collects evidence through the same deterministic
pre-selection as every arm (`agent.agents.diagnosis._preselect`) so all
arms reason over the same evidence substrate; the difference is only
that interpretation and selection come from the table, not a model.

Deliberate properties:

- Zero LLM calls. The `llm` dep is never touched - arm A runs with no
  HELIX_LLM_API_KEY.
- The table names the same catalogue actions the planner can propose.
  Entries are ordered by declared preference; the shared exclusion list
  (validation/verification failures) removes a tabled action before the
  next is tried, so the bounded replan path is exercised for real.
- Everything downstream of selection is shared with arm E: validate,
  policy gate, execute, verify, compensate. Only the two reasoning nodes
  differ.
- A signal no rule matches produces an honest low-confidence hypothesis
  and an empty plan - the coordinator escalates; the rules never invent
  a diagnosis.
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from agent.actions.catalogue import get_action
from agent.agents.diagnosis import _persist, _preselect, _signal_context
from agent.state import Hypothesis, WorkflowState
from agent.validation import candidate_fingerprint
from helix.observability import trace_workflow_step
from state import models

if TYPE_CHECKING:
    from agent.graph import GraphDeps

REPO_ROOT = Path(__file__).resolve().parents[1]

_K8S = {"deployment": "payment-api", "namespace": "payment-api"}
_SECRET = "sandbox/payment-api/k8s/03-secret.yaml"
_CONFIGMAP = "sandbox/payment-api/k8s/02-configmap.yaml"
_DEPLOYMENT = "sandbox/payment-api/k8s/04-deployment.yaml"


def _file_change(path: str, replacements: tuple[tuple[str, str], ...] = ()) -> dict | None:
    """Read the committed repo file and apply declared substitutions - the
    corrected content an open_fix_pr carries. Returns None when the file
    is unreadable or a declared `old` string is absent: a rule that cannot
    express its fix produces no candidate, never a guessed one."""
    try:
        content = (REPO_ROOT / path).read_text(encoding="utf-8")
    except OSError:
        return None
    for old, new in replacements:
        if old not in content:
            return None
        content = content.replace(old, new)
    return {"path": path, "content": content}


def _fix_pr(state: WorkflowState, *files: dict | None) -> dict | None:
    """Build open_fix_pr params for the incident's helix/INC-* branch.
    None when any file's content could not be produced."""
    file_list = [f for f in files if f is not None]
    if len(file_list) != len(files) or not file_list:
        return None
    return {
        "title": f"Helix remediation for {state['incident_id']}",
        "body": "Deterministic arm-A remediation carrying the corrected file contents.",
        "branch": f"helix/{state['incident_id']}",
        "files": file_list,
    }


@dataclass(frozen=True)
class Rule:
    """One signature -> diagnosis + ordered remediation mapping.

    `match` keys must all hold on the signal's signature (a tuple value
    means "any of"); `actions` are tried in order, skipping whatever the
    persisted exclusion list already ruled out."""

    fault: str
    source: str
    match: dict[str, Any]
    claim: str
    evidence_kinds: tuple[str, ...] = ()
    actions: tuple[tuple[str, Callable[[WorkflowState], dict | None]], ...] = field(
        default_factory=tuple
    )


def _reapply(path: str) -> Callable[[WorkflowState], dict | None]:
    return lambda _state: {"path": path}


def _k8s_target(_state: WorkflowState) -> dict | None:
    return dict(_K8S)


def _rerun(_state: WorkflowState) -> dict | None:
    return {"job": "payment-api"}


def _fix(*files: dict | None) -> Callable[[WorkflowState], dict | None]:
    return lambda state: _fix_pr(state, *files)


#: The arm-A rule table, one entry per benchmark fault signature. Claim
#: text states what the signature actually asserts - it is the arm's
#: canned diagnosis, matched against scenario root_cause by the
#: deterministic keyword matcher in benchmark/scoring.py.
RULES: tuple[Rule, ...] = (
    Rule(
        fault="crashloop",
        source="kubernetes",
        match={"waiting_reason": "CrashLoopBackOff", "terminated_reason": "Error"},
        claim=(
            "Secret payment-api-secret holds a blank/whitespace PAYMENT_API_API_KEY - "
            "the container fails startup config validation, exits 1, and the pod is "
            "in CrashLoopBackOff."
        ),
        evidence_kinds=("pod_list", "pod_status", "secret_info", "log_lines", "event_list"),
        actions=(
            ("reapply_manifest", _reapply(_SECRET)),
            # Order 2: after the secret is fixed, a restart escapes the
            # accumulated kubelet backoff immediately instead of waiting
            # out CrashLoopBackOff's retry schedule inside the verify window.
            ("restart_deployment", _k8s_target),
            ("open_fix_pr", _fix(_file_change(_SECRET))),
        ),
    ),
    Rule(
        fault="oom-killed",
        source="kubernetes",
        match={"waiting_reason": "CrashLoopBackOff", "terminated_reason": "OOMKilled"},
        claim=(
            "The payment-api container exceeds its 128Mi memory limit "
            "(PAYMENT_API_BALLAST_MB override) and is OOMKilled with exit code 137, "
            "leaving the pod in CrashLoopBackOff."
        ),
        evidence_kinds=("pod_list", "pod_status", "log_lines", "event_list"),
        actions=(
            ("rollback_deployment", _k8s_target),
            ("open_fix_pr", _fix(_file_change(_DEPLOYMENT, (("memory: 128Mi", "memory: 256Mi"),)))),
        ),
    ),
    Rule(
        fault="missing-secret",
        source="kubernetes",
        match={"waiting_reason": "CreateContainerConfigError"},
        claim=(
            "Secret payment-api-secret is missing/deleted, so new pods fail at "
            "CreateContainerConfigError and the container never starts."
        ),
        evidence_kinds=("pod_list", "pod_status", "secret_info", "event_list"),
        actions=(
            ("reapply_manifest", _reapply(_SECRET)),
            # Same reasoning as crashloop: CreateContainerConfigError pods
            # retry on backoff; a restart after the secret exists again
            # recreates the container immediately.
            ("restart_deployment", _k8s_target),
            ("open_fix_pr", _fix(_file_change(_SECRET))),
        ),
    ),
    Rule(
        fault="failed-rollout",
        source="kubernetes",
        match={"waiting_reason": ("ImagePullBackOff", "ErrImagePull")},
        claim=(
            "deployment/payment-api was pointed at an un-pullable image tag "
            "(payment-api:doesnotexist); new pods sit in ImagePullBackOff and the "
            "rollout is wedged."
        ),
        evidence_kinds=("pod_list", "pod_status", "event_list"),
        actions=(
            ("rollback_deployment", _k8s_target),
            ("reapply_manifest", _reapply(_DEPLOYMENT)),
        ),
    ),
    Rule(
        fault="config-mismatch",
        source="kubernetes",
        match={"symptom": "rollout_wedged"},
        claim=(
            "An out-of-band PAYMENT_API_DOWNSTREAM_URL env override points at a "
            "nonexistent host; new pods fail readiness probes and the rollout is "
            "wedged while the old ReplicaSet keeps serving."
        ),
        evidence_kinds=("pod_list", "pod_status", "log_lines", "event_list"),
        actions=(("rollback_deployment", _k8s_target),),
    ),
    Rule(
        fault="bad-deployment-config",
        source="kubernetes",
        match={"reason": "ApplyRejected"},
        claim=(
            "A deployment patch was rejected by API-server schema validation: "
            "resources.limits.memory 'not-a-quantity' is an invalid quantity - the "
            "intended change was never expressed as a valid manifest."
        ),
        evidence_kinds=("pod_list", "pod_status", "event_list"),
        actions=(
            ("open_fix_pr", _fix(_file_change(_DEPLOYMENT, (("memory: 128Mi", "memory: 256Mi"),)))),
            ("reapply_manifest", _reapply(_DEPLOYMENT)),
        ),
    ),
    Rule(
        fault="dependency-failure",
        source="jenkins",
        match={"job": "payment-api", "stage": "Install dependencies"},
        claim=(
            "pyproject.toml declares the unresolvable dependency "
            "this-package-definitely-does-not-exist-helix-fault - uv sync fails "
            "dependency resolution in the Install dependencies stage."
        ),
        evidence_kinds=("build_console", "test_results", "job_info"),
        actions=(
            ("open_fix_pr", _fix(_file_change("sandbox/payment-api/pyproject.toml"))),
            ("rerun_pipeline", _rerun),
        ),
    ),
    Rule(
        fault="test-regression",
        source="jenkins",
        match={"job": "payment-api", "stage": "Test"},
        claim=(
            "tests/test_main.py::test_health_returns_200 asserts status_code == 999 "
            "instead of 200 - a broken test assertion fails the Test stage "
            "deterministically."
        ),
        evidence_kinds=("build_console", "test_results", "job_info"),
        actions=(
            ("open_fix_pr", _fix(_file_change("sandbox/payment-api/tests/test_main.py"))),
            ("rerun_pipeline", _rerun),
        ),
    ),
    Rule(
        fault="docker-build-failure",
        source="jenkins",
        match={"job": "payment-api", "stage": "Build image"},
        claim=(
            "The Dockerfile COPYs this-file-does-not-exist-helix-fault.txt, which is "
            "absent from the build context - the image build cannot succeed until "
            "the Dockerfile is corrected."
        ),
        evidence_kinds=("build_console", "job_info"),
        actions=(
            ("open_fix_pr", _fix(_file_change("sandbox/payment-api/Dockerfile"))),
            ("rerun_pipeline", _rerun),
        ),
    ),
    Rule(
        fault="rejected-pr",
        source="github",
        match={"type": "pull_request_review", "review_state": "CHANGES_REQUESTED"},
        claim=(
            "The pull request received a changes-requested review: its configmap "
            "edit points PAYMENT_API_DOWNSTREAM_URL at a nonexistent host, so the "
            "change must be corrected and re-authored."
        ),
        evidence_kinds=("pr_info", "pr_review", "commit", "diff"),
        actions=(
            ("open_fix_pr", _fix(_file_change(_CONFIGMAP))),
            ("rerun_pipeline", _rerun),
        ),
    ),
    Rule(
        fault="failed-check",
        source="github",
        match={"type": "check_run", "conclusion": "failure"},
        claim=(
            "The payment-api-ci check run failed on a transient runner network "
            "reset rather than a code defect - the check needs a re-run."
        ),
        evidence_kinds=("check_run", "commit", "diff"),
        actions=(
            ("rerun_pipeline", _rerun),
            ("open_fix_pr", _fix(_file_change("sandbox/payment-api/payment_api/config.py"))),
        ),
    ),
    Rule(
        fault="stale-branch",
        source="github",
        match={"type": "branch_state", "comparison": "diverged"},
        claim=(
            "The work branch is stale - diverged far behind main and unmergeable - "
            "so the change it carries must be re-authored on a fresh branch cut "
            "from current main."
        ),
        evidence_kinds=("branch_state", "commit", "diff"),
        actions=(
            (
                "open_fix_pr",
                _fix(
                    _file_change(
                        _CONFIGMAP,
                        (
                            (
                                "  PAYMENT_API_DOWNSTREAM_URL: "
                                '"http://downstream-stub.payment-api.svc.cluster.local"',
                                "  PAYMENT_API_DOWNSTREAM_URL: "
                                '"http://downstream-stub.payment-api.svc.cluster.local"\n'
                                '  PAYMENT_API_TIMEOUT_S: "8"',
                            ),
                        ),
                    )
                ),
            ),
            ("rerun_pipeline", _rerun),
        ),
    ),
)


def _sig_values(value: Any) -> frozenset[str]:
    values: Iterable[Any] = value if isinstance(value, tuple | frozenset | set) else (value,)
    return frozenset(str(v).lower() for v in values)


def match_rule(signal: dict[str, Any]) -> Rule | None:
    """The first rule whose source and every `match` key hold on the
    signal's signature, else None. Case-insensitive on strings; tuple
    values are any-of."""
    signature = signal.get("signature") or {}
    for rule in RULES:
        if signal.get("source") != rule.source:
            continue
        if all(
            str(signature.get(key, "")).lower() in _sig_values(want)
            for key, want in rule.match.items()
        ):
            return rule
    return None


def rules_diagnosis_node(deps: "GraphDeps"):
    """Arm-A diagnosis: deterministic evidence collection, then the rule
    table's canned claim. No evidence-request loop (that round is the
    LLM's); no model call at all."""

    def _diagnose(state: WorkflowState) -> dict:
        with trace_workflow_step("rules_diagnosis"):
            errors: list[str] = list(state.get("errors", []))
            ctx = _signal_context(state)
            collected = _preselect(ctx, deps, errors)

            rule = match_rule(state["signal"])
            if rule is not None:
                cited = [e.id for e in collected if e.kind.value in rule.evidence_kinds]
                hypothesis = Hypothesis(
                    claim=rule.claim,
                    evidence_refs=cited or [e.id for e in collected],
                    confidence=0.9,
                )
            else:
                hypothesis = Hypothesis(
                    claim="no deterministic rule matched this signal signature",
                    evidence_refs=[],
                    confidence=0.15,
                )
            _persist(deps, state, collected, hypothesis)
            return {
                "evidence": [e.model_dump(mode="json") for e in collected],
                "hypothesis": hypothesis.model_dump(mode="json"),
                "diagnosis_complete": True,
                "errors": errors,
            }

    return _diagnose


def rules_plan_node(deps: "GraphDeps"):
    """Arm-A plan: walk the matched rule's ordered action table, skip
    whatever the exclusion list already killed, select the first survivor.
    Every surviving action lands as a candidates row in table order - the
    provenance 'candidates' stage exists for arm A exactly like arm E."""

    def _plan(state: WorkflowState) -> dict:
        with trace_workflow_step("rules_plan"):
            errors: list[str] = list(state.get("errors", []))
            outcome: dict[str, Any] = {
                "candidates": [],
                "selected_action": None,
                "plan_complete": True,
                "plan_failed": None,
                "escalation_cause": None,
                "errors": errors,
            }
            rule = match_rule(state.get("signal") or {})
            if rule is None:
                outcome["escalation_cause"] = (
                    "rules: no rule matches this signal signature - "
                    "the deterministic arm cannot diagnose it"
                )
                return outcome

            excluded = frozenset(state.get("excluded_candidates") or [])
            known_ids = {e.get("id") for e in state.get("evidence") or []}
            proposals: list[dict[str, Any]] = []
            for action_name, builder in rule.actions:
                spec = get_action(action_name)
                if spec is None:
                    errors.append(f"rules: action {action_name!r} is not in the catalogue")
                    continue
                try:
                    raw = builder(state)
                except Exception as exc:
                    errors.append(f"rules: params for {action_name} could not be built: {exc}")
                    continue
                if raw is None:
                    continue
                try:
                    params = spec.params_model.model_validate(raw)
                except ValidationError as exc:
                    errors.append(
                        f"rules: tabled params for {action_name} failed validation - "
                        f"{exc.errors()[0]['msg']}"
                    )
                    continue
                normalized = params.model_dump(mode="json")
                fingerprint = candidate_fingerprint(spec.name, normalized)
                if fingerprint in excluded:
                    continue
                proposals.append(
                    {
                        "action": spec.name,
                        "params": normalized,
                        "fingerprint": fingerprint,
                        "risk_tier": spec.risk_tier(params).value,
                    }
                )

            if not proposals:
                outcome["escalation_cause"] = (
                    f"rules: every tabled action for {rule.fault} is already "
                    "excluded or could not be expressed"
                )
                return outcome

            cited = sorted(
                set((state.get("hypothesis") or {}).get("evidence_refs") or []) & known_ids
            )
            with deps.session_scope() as session:
                for rank, proposal in enumerate(proposals, start=1):
                    row = models.Candidate(
                        incident_id=state["incident_pk"],
                        attempt=state.get("attempts", 0),
                        rank=rank,
                        action=proposal["action"],
                        params=proposal["params"],
                        rationale=f"rules table entry for {rule.fault} (order {rank})",
                        evidence_refs=cited,
                        score=round(1.0 - 0.1 * (rank - 1), 4),
                        risk_tier=proposal["risk_tier"],
                        selected=rank == 1,
                        components={
                            "arm": "A",
                            "table_order": rank,
                            "selection": "first non-excluded tabled action - no ranker",
                        },
                    )
                    session.add(row)
                    session.flush()
                    if rank == 1:
                        proposals[0]["candidate_id"] = row.id

            top = proposals[0]
            outcome["candidates"] = [
                {
                    "rank": i,
                    "action": p["action"],
                    "params": p["params"],
                    "rationale": f"rules table entry for {rule.fault} (order {i})",
                    "evidence_refs": cited,
                    "risk_tier": p["risk_tier"],
                    "score": round(1.0 - 0.1 * (i - 1), 4),
                    "components": {"arm": "A", "table_order": i},
                }
                for i, p in enumerate(proposals, start=1)
            ]
            outcome["selected_action"] = {
                "rank": 1,
                "action": top["action"],
                "params": top["params"],
                "rationale": f"rules table entry for {rule.fault} (order 1)",
                "evidence_refs": cited,
                "risk_tier": top["risk_tier"],
                "score": 1.0,
                "candidate_id": top["candidate_id"],
            }
            return outcome

    return _plan
