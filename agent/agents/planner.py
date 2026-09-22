"""Recovery Planner: the LLM proposes, a deterministic Python ranker orders.

Flow per run of the node:

1. Gate on the hypothesis: absent or below `MIN_HYPOTHESIS_CONFIDENCE`
   (the diagnosis agent's clamped-confidence ceiling is 0.2) -> escalate
   without spending an LLM call.
2. `complete_structured` proposes a `CandidateList` - each candidate names
   a catalogue action, params, a rationale, and the evidence ids it leans
   on. The prompt's action menu is `catalogue_view()` output, so it cannot
   drift from the registry.
3. Each proposal is constrained BEFORE ranking: unknown action names and
   params failing the ActionSpec's `params_model` are dropped (recorded in
   `state.errors`) - the catalogue's bounds are the first safety gate.
4. `rank_candidates` - pure Python, no LLM - scores survivors on evidence
   support, expected-effect match, risk tier, and blast radius, and
   returns the full ranked list.
5. The ranked list is persisted to `candidates` (every candidate, its
   rank, rationale, refs, score breakdown) - the 1.9 provenance timeline's
   "candidates" stage is a DB query, not a memory artifact.

Outcomes the coordinator turns into transitions:

- `selected_action` set -> VALIDATING (1.7 fills the node behind it).
- `plan_complete` with `escalation_cause` -> ESCALATED. Escalation is a
  real terminal outcome - empty proposals, proposals that all fail
  catalogue constraints, low confidence, or nothing clearing the score
  floor all land here, never as an exception.
- `plan_failed` (the proposal call itself errored) -> FAILED, matching
  diagnosis's treatment of provider failures as retriable machinery
  faults rather than domain escalations.
"""

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field, ValidationError

from agent.actions.catalogue import (
    ActionSpec,
    Reversibility,
    RiskTier,
    catalogue_view,
    get_action,
)
from agent.prompts.planning import planning_prompt
from agent.state import Hypothesis, WorkflowState
from agent.validation import candidate_fingerprint
from helix.config import get_settings
from helix.llm import LLMError
from helix.observability import trace_workflow_step
from integrations.evidence import Evidence
from state import models

if TYPE_CHECKING:
    from agent.graph import GraphDeps

#: Below this confidence the planner escalates instead of proposing - a
#: hypothesis the diagnosis agent itself flagged unreliable is not a
#: planning substrate. (Its clamped-confidence ceiling is LOW_CONFIDENCE=0.2.)
MIN_HYPOTHESIS_CONFIDENCE = 0.3

#: A ranked candidate must clear this score to be selected; below it the
#: honest outcome is escalation ("no candidate cleared the safe bar").
MIN_SELECT_SCORE = 1.0

#: Ranker weights - see `rank_candidates` for the scoring breakdown.
W_SUPPORT = 2.0
W_EFFECT = 3.0
_RISK_PENALTY = {RiskTier.LOW: 0.0, RiskTier.MEDIUM: 0.5, RiskTier.HIGH: 1.0}


class CandidateProposal(BaseModel):
    """One proposed recovery action. `action` is a free string at this
    layer (the catalogue lookup drops unknown names); `params` is an
    unvalidated dict until the ActionSpec's params_model checks it - a
    proposal whose params fail validation is dropped, not trusted."""

    action: str
    params: dict[str, Any] = Field(default_factory=dict)
    rationale: str = ""
    evidence_refs: list[str] = Field(default_factory=list)


class CandidateList(BaseModel):
    """The model's proposal set. An empty list is the honest "nothing
    safe to propose" answer - the planner turns it into an escalation."""

    candidates: list[CandidateProposal] = Field(default_factory=list)


@dataclass(frozen=True)
class ScoredCandidate:
    """A proposal that survived catalogue constraints, with its rank and
    the score breakdown the `candidates` table persists."""

    rank: int
    action: str
    params: dict[str, Any]
    rationale: str
    evidence_refs: list[str]
    risk_tier: str
    score: float
    components: dict[str, float] = field(default_factory=dict)

    def to_state(self) -> dict[str, Any]:
        return {
            "rank": self.rank,
            "action": self.action,
            "params": self.params,
            "rationale": self.rationale,
            "evidence_refs": self.evidence_refs,
            "risk_tier": self.risk_tier,
            "score": self.score,
            "components": self.components,
        }


# --- Deterministic scoring -------------------------------------------------

#: Signal-source priors: which action family plausibly remediates a failure
#: surfaced from that domain. A CI-side fault is not fixed by a runtime
#: write and vice versa - this is the coarse cross-domain check.
_DOMAIN_FIT: dict[str, dict[str, float]] = {
    "kubernetes": {
        "restart_deployment": 1.0,
        "rollback_deployment": 1.0,
        "scale_deployment": 1.0,
        "reapply_manifest": 1.0,
        "open_fix_pr": 0.7,
        "rerun_pipeline": 0.1,
    },
    "jenkins": {
        "rerun_pipeline": 1.0,
        "open_fix_pr": 0.9,
        "reapply_manifest": 0.3,
        "restart_deployment": 0.1,
        "rollback_deployment": 0.1,
        "scale_deployment": 0.1,
    },
    "github": {
        "open_fix_pr": 1.0,
        "rerun_pipeline": 0.6,
        "reapply_manifest": 0.2,
        "restart_deployment": 0.1,
        "rollback_deployment": 0.1,
        "scale_deployment": 0.1,
    },
}

#: Claim/signal text -> the fault surface it implicates. The ranker cannot
#: parse semantics; these keyword surfaces are the honest deterministic
#: proxy for "does this action act where the hypothesis says the fault is".
_CLAIM_SURFACES: dict[str, tuple[str, ...]] = {
    "secret": ("secret", "api key", "api_key", "credential", "token"),
    "envconfig": (
        "configmap",
        "env var",
        "env override",
        "environment variable",
        "downstream_url",
        "downstream url",
        "config",
    ),
    "resources": ("memory", "oom", "cpu", "limit", "ballast", "resource"),
    "image": (
        "image",
        "imagepull",
        "errimagepull",
        "pullbackoff",
        "cannot be pulled",
        "tag",
        "registry",
    ),
    "capacity": ("scaled", "scale to", "replica count", "no pods", "zero pods"),
    "workload": (
        "deployment",
        "pod",
        "rollout",
        "crash",
        "restart",
        "revision",
        "replicaset",
        "container",
    ),
    "tests": ("test_", "tests/", "test ", "assert", "pytest"),
    "dockerfile": ("dockerfile", "docker build", "copy "),
    "deps": (
        "pyproject",
        "uv.lock",
        "uv sync",
        "package",
        "dependency",
        "resolve",
        "requirements",
        "pip install",
        "unsatisfiable",
    ),
    "manifest": ("manifest", ".yaml", "apply"),
    "pipeline": ("pipeline", "job", "build", "stage", "jenkins", "console", "check"),
    "pr": ("pull request", "pr ", "pr#", "review", "branch", "merge", "stale"),
}

#: Surfaces each action can actually remediate. reapply_manifest derives
#: its surface from WHICH manifest is named - the ConfigMap file remediates
#: config drift, the Secret file secret drift, the Deployment file image /
#: resources / template drift. rollback remediates anything a recent
#: rollout revision delivered (image, env overrides, resources).
_ACTION_SURFACES: dict[str, frozenset[str]] = {
    "restart_deployment": frozenset({"workload"}),
    "rollback_deployment": frozenset({"workload", "image", "resources", "envconfig"}),
    "scale_deployment": frozenset({"capacity"}),
    "rerun_pipeline": frozenset({"pipeline"}),
}

#: Manifest basename -> the surfaces applying that file can remediate.
_MANIFEST_SURFACES: dict[str, frozenset[str]] = {
    "00-namespace.yaml": frozenset({"workload"}),
    "01-downstream-stub.yaml": frozenset({"workload", "capacity"}),
    "02-configmap.yaml": frozenset({"envconfig"}),
    "03-secret.yaml": frozenset({"secret"}),
    "04-deployment.yaml": frozenset({"workload", "image", "resources", "envconfig"}),
    "05-service.yaml": frozenset({"workload"}),
    "06-ingress.yaml": frozenset({"workload"}),
}

#: Terms marking a persistent content/config defect - the failure recurs
#: until the defect itself changes, so a STATELESS action (restart,
#: rerun) that changes nothing persistent can never remediate it. This is
#: the catalogue's own reversibility class doing real work.
_PERSISTENT_TERMS = (
    "assert",
    "expected",
    "does not exist",
    "doesnotexist",
    "not found",
    "unresolvable",
    "unsatisfiable",
    "no solution",
    "invalid",
    "reject",
    "missing",
    "absent",
    "blank",
    "whitespace",
    "secret",
    "credential",
    "oom",
    "out of memory",
    "memory limit",
    "imagepull",
    "errimagepull",
    "pullbackoff",
    "cannot be pulled",
    "nonexist",
    "misconfigur",
    "typo",
    "syntax",
    "dockerfile",
)


def _corpus(hypothesis: Hypothesis, evidence: list[Evidence], signal: dict[str, Any]) -> str:
    """The text the surface/keyword checks run over: the claim, the signal,
    and SUMMARIES of the cited evidence (payloads stay out - bounded and
    consistent with the prompt's own summary-only view)."""
    cited = {e.summary for e in evidence if e.id in set(hypothesis.evidence_refs)}
    return " ".join(
        [
            hypothesis.claim,
            json.dumps(signal.get("signature") or {}, default=str),
            json.dumps(signal.get("raw") or {}, default=str),
            *sorted(cited),
        ]
    ).lower()


def _claim_surfaces(corpus: str) -> frozenset[str]:
    return frozenset(
        surface
        for surface, terms in _CLAIM_SURFACES.items()
        if any(term in corpus for term in terms)
    )


def _persistent_defect(corpus: str) -> bool:
    return any(term in corpus for term in _PERSISTENT_TERMS)


def _action_surfaces(spec: ActionSpec, params: BaseModel) -> frozenset[str]:
    """The surfaces `spec` under these validated params can remediate."""
    if spec.name == "reapply_manifest":
        basename = getattr(params, "path", "").rsplit("/", 1)[-1]
        return _MANIFEST_SURFACES.get(basename, frozenset({"manifest"}))
    if spec.name == "open_fix_pr":
        surfaces: set[str] = {"pr"}
        for f in getattr(params, "files", []):
            name = f.path.rsplit("/", 1)[-1].lower()
            if f.path.startswith("sandbox/payment-api/k8s/"):
                surfaces |= _MANIFEST_SURFACES.get(name, {"manifest"})
            elif name == "dockerfile" or name.endswith("dockerfile"):
                surfaces.add("dockerfile")
            elif name in ("pyproject.toml", "uv.lock"):
                surfaces.add("deps")
            elif name.startswith("test_") or "/tests/" in f"/{f.path}":
                surfaces.add("tests")
            else:
                surfaces.add("repo")
        return frozenset(surfaces)
    return _ACTION_SURFACES.get(spec.name, frozenset())


def _targets_seen(spec: ActionSpec, params: BaseModel, corpus: str) -> bool:
    """Entity params (a deployment or job name) must appear somewhere in
    the claim/signal/cited evidence - a name the evidence never mentions
    is a hallucinated target."""
    for key in ("deployment", "job"):
        value = getattr(params, key, None)
        if value and str(value).lower() not in corpus:
            return False
    return True


def _evidence_support(refs: list[str], known_ids: set[str], hypothesis_refs: set[str]) -> float:
    """Fraction of cited refs that resolve to collected evidence, plus a
    relevance bonus when the candidate cites evidence the hypothesis
    itself leaned on. Citing nothing scores zero."""
    if not refs:
        return 0.0
    resolved = sum(1 for ref in refs if ref in known_ids)
    support = resolved / len(refs)
    if set(refs) & hypothesis_refs:
        support = min(1.0, support + 0.25)
    return support


def _expected_effect(spec: ActionSpec, params: BaseModel, corpus: str, signal_source: str) -> float:
    """domain fit + fault-surface match, damped for stateless actions on
    persistent defects and for targets the evidence never named."""
    domain = _DOMAIN_FIT.get(signal_source, {}).get(spec.name, 0.2)
    surfaces = _action_surfaces(spec, params)
    surface = 1.0 if surfaces & _claim_surfaces(corpus) else 0.15
    effect = 0.4 * domain + 0.6 * surface
    if spec.reversibility == Reversibility.STATELESS and _persistent_defect(corpus):
        # A restart or rerun on a content/config defect re-fails
        # deterministically - near-zero expected effect, not zero, because
        # the surface match may still reflect the right neighborhood.
        effect *= 0.15
    if not _targets_seen(spec, params, corpus):
        effect *= 0.5
    return effect


def rank_candidates(
    proposal: CandidateList,
    *,
    hypothesis: Hypothesis,
    evidence: list[Evidence],
    signal: dict[str, Any],
    excluded: frozenset[str] = frozenset(),
) -> tuple[list[ScoredCandidate], list[str]]:
    """Score and order the proposed candidates. Pure Python - no LLM call,
    no I/O - so ranking is fully deterministic and unit-testable.

    Returns (ranked candidates, drop reasons). Proposals are dropped when
    they name a non-catalogue action, fail their params_model, or carry a
    fingerprint in `excluded` - the validation-failure exclusion list
    (1.7): a candidate that already failed validation is never
    re-selected. The catalogue bounds are enforced here, before any
    score exists.
    """
    known_ids = {e.id for e in evidence}
    corpus = _corpus(hypothesis, evidence, signal)
    signal_source = str(signal.get("source") or "")
    drops: list[str] = []
    scored: list[ScoredCandidate] = []

    for cand in proposal.candidates:
        spec = get_action(cand.action)
        if spec is None:
            drops.append(f"candidate {cand.action!r} dropped: not in the action catalogue")
            continue
        try:
            params = spec.params_model.model_validate(cand.params)
        except ValidationError as exc:
            drops.append(
                f"candidate {cand.action!r} dropped: params failed validation - "
                f"{exc.errors()[0]['msg']}"
            )
            continue
        if candidate_fingerprint(spec.name, params.model_dump(mode="json")) in excluded:
            drops.append(
                f"candidate {cand.action!r} dropped: identical params already "
                f"failed validation for this incident"
            )
            continue
        tier = spec.risk_tier(params)
        support = _evidence_support(cand.evidence_refs, known_ids, set(hypothesis.evidence_refs))
        effect = _expected_effect(spec, params, corpus, signal_source)
        score = W_SUPPORT * support + W_EFFECT * effect - _RISK_PENALTY[tier] - spec.blast_radius
        scored.append(
            ScoredCandidate(
                rank=0,  # assigned after sorting
                action=spec.name,
                params=params.model_dump(mode="json"),
                rationale=cand.rationale,
                evidence_refs=list(cand.evidence_refs),
                risk_tier=tier.value,
                score=round(score, 4),
                components={
                    "evidence_support": round(support, 4),
                    "expected_effect": round(effect, 4),
                    "risk_penalty": _RISK_PENALTY[tier],
                    "blast_radius": spec.blast_radius,
                },
            )
        )

    # Deterministic order: score desc, then expected effect, then name -
    # identical inputs always produce an identical ranking.
    scored.sort(key=lambda c: (-c.score, -c.components["expected_effect"], c.action))
    return (
        [ScoredCandidate(**{**c.__dict__, "rank": i}) for i, c in enumerate(scored, start=1)],
        drops,
    )


# --- The graph node --------------------------------------------------------


def _incident_view(state: WorkflowState) -> dict[str, Any]:
    return {
        "incident_id": state.get("incident_id"),
        "service": state.get("service"),
        "environment": state.get("environment"),
        "severity": state.get("severity"),
        "signal": state.get("signal") or {},
    }


def _evidence_view(evidence: list[Evidence]) -> list[dict[str, Any]]:
    return [e.model_dump(mode="json") for e in evidence]


def _persist_candidates(
    deps: "GraphDeps",
    state: WorkflowState,
    ranked: list[ScoredCandidate],
    *,
    selected: bool,
) -> None:
    """Write the FULL ranked list - every candidate, its rank, rationale,
    refs and score breakdown - tagged with the incident's attempt count so
    replan attempts (1.10) coexist as separate rows. `selected` marks the
    rank-1 row only when a selection was actually made (a below-floor
    rank-1 row is not a selection)."""
    with deps.session_scope() as session:
        for cand in ranked:
            session.add(
                models.Candidate(
                    incident_id=state["incident_pk"],
                    attempt=state.get("attempts", 0),
                    rank=cand.rank,
                    action=cand.action,
                    params=cand.params,
                    rationale=cand.rationale,
                    evidence_refs=cand.evidence_refs,
                    score=cand.score,
                    risk_tier=cand.risk_tier,
                    selected=selected and cand.rank == 1,
                    components=cand.components,
                )
            )


def planner_node(deps: "GraphDeps"):
    """The plan graph node: propose -> constrain -> rank -> select or
    escalate. Writes candidates/selected_action/plan_* into state; the
    coordinator turns them into transitions."""

    def _plan(state: WorkflowState) -> dict:
        with trace_workflow_step("plan"):
            errors: list[str] = list(state.get("errors", []))
            outcome: dict[str, Any] = {
                "candidates": [],
                "selected_action": None,
                "plan_complete": True,
                "plan_failed": None,
                "escalation_cause": None,
                "errors": errors,
            }

            hyp_dict = state.get("hypothesis")
            if not hyp_dict:
                outcome["escalation_cause"] = "planner: no hypothesis to plan from"
                return outcome
            hypothesis = Hypothesis.model_validate(hyp_dict)
            if hypothesis.confidence < MIN_HYPOTHESIS_CONFIDENCE:
                outcome["escalation_cause"] = (
                    f"planner: hypothesis confidence {hypothesis.confidence} "
                    f"below {MIN_HYPOTHESIS_CONFIDENCE}"
                )
                return outcome

            evidence = [Evidence.model_validate(e) for e in state.get("evidence", [])]
            # 2.8: the Knowledge Agent's organizational context (event
            # timeline + graph neighborhood) is advisory context for the
            # proposal call when the graph layer is on; off, the prompt is
            # byte-identical to the pre-2.8 baseline.
            knowledge = ""
            if get_settings().graph_enabled:
                from agent.knowledge_context import knowledge_context

                incident_view = _incident_view(state)
                knowledge = knowledge_context(
                    deps,
                    incident_view,
                    (state.get("signal") or {}).get("signature"),
                    include_retrieval=False,
                )
            prompt = planning_prompt(
                _incident_view(state),
                hypothesis.model_dump(mode="json"),
                _evidence_view(evidence),
                catalogue_view(),
                prior_context=f"{knowledge}\n\n" if knowledge else "",
            )
            try:
                proposal = deps.llm.complete_structured(prompt, CandidateList)
            except LLMError as exc:
                errors.append(f"planning proposal failed: {exc}")
                outcome["plan_complete"] = False
                outcome["plan_failed"] = str(exc)
                outcome["escalation_cause"] = None
                return outcome

            ranked, drops = rank_candidates(
                proposal,
                hypothesis=hypothesis,
                evidence=evidence,
                signal=state.get("signal") or {},
                # 1.7: candidates that already failed validation can never
                # be re-proposed - the planner consults the exclusion list.
                excluded=frozenset(state.get("excluded_candidates") or []),
            )
            errors.extend(drops)
            outcome["candidates"] = [c.to_state() for c in ranked]

            if not ranked:
                outcome["escalation_cause"] = (
                    "planner: no proposed action survived catalogue constraints"
                    if proposal.candidates
                    else "planner: no candidate actions proposed"
                )
                _persist_candidates(deps, state, ranked, selected=False)
                return outcome
            top = ranked[0]
            if top.score < MIN_SELECT_SCORE:
                outcome["escalation_cause"] = (
                    f"planner: top candidate {top.action} scored "
                    f"{top.score:.2f} below the safe bar {MIN_SELECT_SCORE}"
                )
                _persist_candidates(deps, state, ranked, selected=False)
                return outcome
            outcome["selected_action"] = top.to_state()
            _persist_candidates(deps, state, ranked, selected=True)
            return outcome

    return _plan
