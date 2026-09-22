"""Typed workflow state flowing through the agent graph (subphase 1.5).

`WorkflowState` is deliberately flat and JSON-serializable: it is BOTH the
LangGraph checkpointer payload (D13 - a suspended run resumes from it in
any process) and the provenance substrate 1.9 assembles. Evidence is
stored as `Evidence.model_dump(mode="json")` dicts and the hypothesis as
a `Hypothesis.model_dump()` dict - never vendor types (C8).

`Hypothesis` is the Diagnosis Agent's output contract: claim, the
evidence ids it cites, a 0..1 confidence, and the signals that contradict
it. `evidence_refs` must resolve to collected evidence ids - a citation
of never-collected evidence is a structured-validation failure
(agents/diagnosis.py re-prompts once, then records with clamped
confidence; full unhappy-path handling is 1.10).
"""

from typing import Any, TypedDict

from pydantic import BaseModel, Field


class Hypothesis(BaseModel):
    """A root-cause claim constrained to cite collected evidence."""

    claim: str
    evidence_refs: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    contradicting_signals: list[str] = Field(default_factory=list)


class WorkflowState(TypedDict, total=False):
    """The graph payload. Every key is optional so nodes can read only the
    slice they need and partial updates merge cleanly under the
    checkpointer.

    Fields populated by later subphases (execution, verification) are
    declared now so the typed contract is stable - the nodes that write
    them land in 1.8-1.9.
    """

    incident_id: str  # INC-* external id; also the checkpointer thread_id
    incident_pk: int  # incidents.id - FK target for evidence/hypothesis rows
    service: str
    environment: str
    severity: str
    status: str  # last-known incident status (machine.IncidentStatus values)
    signal: dict[str, Any]  # latest signal: {source, signature, raw}
    evidence: list[dict[str, Any]]  # Evidence dicts in collection order
    evidence_rounds: int  # LLM request-more-evidence rounds used this diagnosis
    diagnosis_complete: bool  # diagnosis node ran (drives coordinator routing)
    hypothesis: dict[str, Any]  # Hypothesis dict
    candidates: list[dict[str, Any]]  # 1.6 - the full ranked candidate list
    selected_action: dict[str, Any]  # 1.6/1.7 - the rank-1 candidate dict, if selected
    plan_complete: bool  # 1.6 - planner produced an outcome (drives coordinator routing)
    plan_failed: str | None  # 1.6 - proposal call itself errored (-> FAILED)
    escalation_cause: str | None  # 1.6 - why the planner escalated (-> ESCALATED)
    validation: dict[str, Any]  # 1.7 - latest ValidationResult dict (+ action/params)
    validation_attempts: int  # 1.7 - validate passes this run; the single-retry bound
    excluded_candidates: list[str]  # 1.7+ - fingerprints never re-proposed (fail/deny/reject)
    policy: dict[str, Any]  # 1.8 - latest PolicyVerdict snapshot (agent/policy.py)
    approval: dict[str, Any]  # 1.8 - latest approvals-row snapshot for the selection
    execution: dict[str, Any]  # 1.8 - executions-row summary the execute node wrote
    verification: dict[str, Any]  # 1.9 - latest VerificationReport dict
    needs_compensation: bool  # 1.9/1.10 - a failed verification left an
    # un-undone compensable change; the compensate node pays the debt.
    compensation: dict[str, Any]  # 1.10 - the compensation executions-row
    # summary the compensate node wrote (result routes escalate/replan)
    node_failed: dict[str, Any]  # 1.10 - {"node", "cause"} a timed-out or
    # crashed node set; the coordinator turns it into a FAILED transition
    attempts: int  # incident.attempt_count mirrored for hypothesis rows
    errors: list[str]  # node-level failure notes; coordinator consumes them

    # Phase 2 (2.1/2.5/2.6) - the change-review lifecycle. `kind` mirrors
    # Incident.kind ("recovery" | "change") so the coordinator can branch
    # on it without a fresh query. The rest are the Triage/Review agents'
    # reports plus the coordinator's own reads of newer-event facts -
    # see agent/agents/coordinator.py's `_decide` docstring.
    kind: str  # "recovery" | "change"
    triage: dict[str, Any]  # 2.5 - TriageResult report
    triage_complete: bool  # triage node ran this pass (drives routing)
    review: dict[str, Any]  # 2.6 - reviewer resolution report
    review_complete: bool  # review node ran this pass (drives routing)
    fix_pushed: bool  # 2.6 - a newer pr.updated event exists at fix_required
