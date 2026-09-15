"""Arm-B fused node (subphase 1.11, decision D8): `diagnose_and_plan`.

One `complete_structured` call over deterministically pre-collected
evidence produces the hypothesis AND the selected action directly - no
evidence-request loop, no ranker, and (by graph composition) no
validate/verify/replan nodes downstream. This is the deliberately weaker
contrast arm: the policy/approval gate and the executor are shared safety
substrate, but everything interpretive lives in the single call.

The candidate that survives lands in `candidates` with selected=True so
the provenance "candidates"/"decision" stages exist for arm B exactly as
for A/E. A proposal naming a non-catalogue action or failing param
validation is an honest escalation - arm B has no replan to catch it.
"""

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field, ValidationError

from agent.actions.catalogue import catalogue_view, get_action
from agent.agents.diagnosis import _persist, _preselect, _signal_context
from agent.prompts.fused import fused_prompt
from agent.state import Hypothesis, WorkflowState
from helix.llm import LLMError
from helix.observability import trace_workflow_step
from state import models

if TYPE_CHECKING:
    from agent.graph import GraphDeps


class FusedDiagnosisPlan(BaseModel):
    """The single-call answer: hypothesis and selected action together."""

    claim: str = ""
    evidence_refs: list[str] = Field(default_factory=list)
    confidence: float = 0.5
    action: str = ""
    params: dict[str, Any] = Field(default_factory=dict)
    rationale: str = ""
    action_evidence_refs: list[str] = Field(default_factory=list)


def _incident_view(state: WorkflowState) -> dict[str, Any]:
    return {
        "incident_id": state.get("incident_id"),
        "service": state.get("service"),
        "environment": state.get("environment"),
        "severity": state.get("severity"),
        "signal": state.get("signal") or {},
    }


def _persist_selection(
    deps: "GraphDeps",
    state: WorkflowState,
    answer: FusedDiagnosisPlan,
    params: dict[str, Any],
    risk_tier: str,
) -> int:
    """The fused arm's one candidate row: selected=True, rank=1, score 0.0
    - there is no ranker behind arm B, so the score column records that
    honestly rather than borrowing arm E's machinery."""
    with deps.session_scope() as session:
        row = models.Candidate(
            incident_id=state["incident_pk"],
            attempt=state.get("attempts", 0),
            rank=1,
            action=answer.action,
            params=params,
            rationale=answer.rationale,
            evidence_refs=list(answer.action_evidence_refs),
            score=0.0,
            risk_tier=risk_tier,
            selected=True,
            components={"arm": "B", "selection": "fused single-call selection - no ranker"},
        )
        session.add(row)
        session.flush()
        return row.id


def diagnose_and_plan_node(deps: "GraphDeps"):
    """The fused node: preselect evidence -> one structured call -> persist
    hypothesis + selected candidate. An LLM/provider failure is a run
    failure (plan_failed), matching diagnosis/planner's treatment."""

    def _diagnose_and_plan(state: WorkflowState) -> dict:
        with trace_workflow_step("diagnose_and_plan"):
            errors: list[str] = list(state.get("errors", []))
            ctx = _signal_context(state)
            collected = _preselect(ctx, deps, errors)
            evidence_view = [e.model_dump(mode="json") for e in collected]

            outcome: dict[str, Any] = {
                "evidence": evidence_view,
                "diagnosis_complete": True,
                "plan_complete": True,
                "plan_failed": None,
                "escalation_cause": None,
                "errors": errors,
            }
            try:
                answer = deps.llm.complete_structured(
                    fused_prompt(_incident_view(state), evidence_view, catalogue_view()),
                    FusedDiagnosisPlan,
                )
            except LLMError as exc:
                errors.append(f"fused diagnose/plan call failed: {exc}")
                outcome["plan_complete"] = False
                outcome["plan_failed"] = str(exc)
                # Evidence still lands - a failed reasoning step does not
                # erase what was collected.
                _persist(deps, state, collected, None)
                return outcome

            hypothesis = Hypothesis(
                claim=answer.claim or "no claim produced",
                evidence_refs=list(answer.evidence_refs),
                confidence=answer.confidence,
            )
            _persist(deps, state, collected, hypothesis)
            outcome["hypothesis"] = hypothesis.model_dump(mode="json")

            spec = get_action(answer.action) if answer.action else None
            if spec is None:
                outcome["escalation_cause"] = (
                    "fused node proposed no action"
                    if not answer.action
                    else f"fused node proposed non-catalogue action {answer.action!r}"
                )
                return outcome
            try:
                validated = spec.params_model.model_validate(answer.params)
            except ValidationError as exc:
                outcome["escalation_cause"] = (
                    f"fused node proposed {spec.name} with invalid params - "
                    f"{exc.errors()[0]['msg']}"
                )
                return outcome
            normalized = validated.model_dump(mode="json")
            tier = spec.risk_tier(validated).value

            candidate_id = _persist_selection(deps, state, answer, normalized, tier)
            selected = {
                "rank": 1,
                "action": spec.name,
                "params": normalized,
                "rationale": answer.rationale,
                "evidence_refs": list(answer.action_evidence_refs),
                "risk_tier": tier,
                "score": 0.0,
                "components": {"arm": "B", "selection": "fused single-call"},
            }
            outcome["candidates"] = [selected]
            outcome["selected_action"] = {**selected, "candidate_id": candidate_id}
            return outcome

    return _diagnose_and_plan
