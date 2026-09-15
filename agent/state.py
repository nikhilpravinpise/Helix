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

    Fields populated by later subphases (candidates, selected_action,
    validation, execution, verification) are declared now so the typed
    contract is stable - the nodes that write them land in 1.6-1.9.
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
    candidates: list[dict[str, Any]]  # 1.6
    selected_action: dict[str, Any]  # 1.6/1.7
    validation: dict[str, Any]  # 1.7
    execution: dict[str, Any]  # 1.8
    verification: dict[str, Any]  # 1.9
    attempts: int  # incident.attempt_count mirrored for hypothesis rows
    errors: list[str]  # node-level failure notes; coordinator consumes them
