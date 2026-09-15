"""The Helix workflow graph (subphase 1.5): LangGraph wiring, dependency
injection, arm composition, and the checkpointer.

Shape (arm E, the full graph):

    START -> intake -> coordinator --> diagnosis --> coordinator --> END
                          ^    |                        ^    |
                          |    v (later subphases)      |    |
                          +-- plan -> validate -> execute -> verify
                              routed by coordinator as they land

The coordinator is the hub: every worker node returns to it, and it owns
all incident transitions plus the next-hop decision
(agent/agents/coordinator.py). The `plan` slot became the real planner in
1.6, `validate` the real Validation Agent in 1.7 (dispatching to the
per-family strategies in agent/validation/), and `execute` the real
executor in 1.8 (the safety-policy gate lives in the coordinator's
routing plus the executor's own re-check); `verify` remains a registered
slot whose body lands in 1.9, so a run ends at `verifying` after a
successful write. Runs also end at `awaiting_approval` (policy requires
a human decision - the run suspends there until an approval row is
decided), `remediation_pending` -> replanned -> back through `validating`
on a failed check, `escalated` (planner escalated, or the single
validation retry was exhausted) or `failed`.

Arms (D8): HELIX_ARM selects node composition, not code forks.
- E: full graph - real diagnosis + plan + validate nodes plus
  placeholder slots for the rest.
- B: `diagnose_and_plan` (one fused LLM call) + `execute`; no
  validate/verify/replan nodes.
- A: `rules_diagnosis` + `rules_plan` deterministic variants; the
  deterministic validate node is shared with E (it is pure code - no
  LLM), execute is shared too; verify stays a placeholder.
Arm A/B rule-node variants are placeholders until 1.11; the composition
switch is what lands now.

Checkpointer (D13): `open_checkpointer()` yields a PostgresSaver bound to
HELIX_DATABASE_URL (thread_id = incident_id), which is what makes a
suspended run resumable by any process. Non-Postgres URLs yield None -
the documented fallback: state then lives only for the invoke.
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from agent.agents.coordinator import coordinator_node, make_router
from agent.agents.diagnosis import diagnosis_node
from agent.agents.execution import execution_node
from agent.agents.planner import planner_node
from agent.agents.validation import validation_node
from agent.state import WorkflowState
from agent.validation import candidate_fingerprint
from helix.config import get_settings
from helix.llm import LLMClient, OpenAICompatibleLLMClient
from helix.observability import trace_workflow_step
from integrations.capabilities import (
    DockerEvidence,
    ElkEvidence,
    GitHubEvidence,
    JenkinsEvidence,
    KubernetesEvidence,
    KubernetesPrechecks,
    get_docker_evidence,
    get_elk_evidence,
    get_github_evidence,
    get_jenkins_evidence,
    get_kubernetes_evidence,
    get_kubernetes_prechecks,
)
from integrations.evidence import Evidence
from integrations.writes import (
    GitHubWrite,
    JenkinsWrite,
    KubernetesWrite,
    get_github_write,
    get_jenkins_write,
    get_kubernetes_write,
)
from state import models
from state.db import session_scope
from state.machine import IncidentStatus


@dataclass(frozen=True)
class GraphDeps:
    """Everything the graph needs that is not part of WorkflowState:
    the session factory for persisted state, the LLM client, the five
    evidence capabilities, the three write transports (same
    HELIX_EVIDENCE_SOURCE switch - D10), and the evidence-round bound.
    Nodes see only Helix-vocabulary capability Protocols - never vendor
    SDK types (C8)."""

    session_scope: Callable[[], Any]  # context manager yielding a Session
    llm: LLMClient
    kubernetes: KubernetesEvidence
    jenkins: JenkinsEvidence
    elk: ElkEvidence
    github: GitHubEvidence
    docker: DockerEvidence
    kube_prechecks: KubernetesPrechecks  # 1.7 - can_i / dry_run_apply
    kubernetes_write: KubernetesWrite  # 1.8 - rollout/scale/apply
    github_write: GitHubWrite  # 1.8 - branch + commit + PR
    jenkins_write: JenkinsWrite  # 1.8 - trigger build
    database_url: str
    write_origin: str = "live"  # "live" | "fixture" - lands on executions.origin
    max_evidence_rounds: int = 3


def default_deps() -> GraphDeps:
    """Wire deps from settings: live or fixture capabilities per
    HELIX_EVIDENCE_SOURCE, the configured LLM client, HELIX_DATABASE_URL.
    The write transports follow the same evidence-source switch (D10)."""
    settings = get_settings()
    return GraphDeps(
        session_scope=session_scope,
        llm=OpenAICompatibleLLMClient(),
        kubernetes=get_kubernetes_evidence(),
        jenkins=get_jenkins_evidence(),
        elk=get_elk_evidence(),
        github=get_github_evidence(),
        docker=get_docker_evidence(),
        kube_prechecks=get_kubernetes_prechecks(),
        kubernetes_write=get_kubernetes_write(),
        github_write=get_github_write(),
        jenkins_write=get_jenkins_write(),
        database_url=settings.database_url,
        write_origin=("fixture" if settings.evidence_source == "fixtures" else "live"),
        max_evidence_rounds=settings.max_evidence_rounds,
    )


def _evidence_from_row(row: models.EvidenceItem) -> dict[str, Any]:
    """Rehydrate a stored evidence_items row into the Evidence dict shape
    WorkflowState carries, so a resumed run sees prior collections."""
    return Evidence(
        id=row.evidence_id,
        source=row.source,
        kind=row.kind,
        service=row.service,
        summary=row.summary,
        payload=row.payload,
        origin=row.origin,
        collected_at=row.collected_at.isoformat(),
        truncated=row.truncated,
        total=row.total,
    ).model_dump(mode="json")


def _excluded_fingerprints(session, incident_pk: int) -> list[str]:
    """Fingerprints of candidates that must never be re-proposed for this
    incident - the persisted exclusion list a fresh run rebuilds its
    in-state list from. Union of three row sources: `fail` validations,
    `rejected` approvals (an operator declined this exact action+params),
    and refused/error executions (a denied or already-failed write is not
    blindly retried)."""
    failed = session.scalars(
        select(models.Validation.fingerprint)
        .where(models.Validation.incident_id == incident_pk)
        .where(models.Validation.outcome == models.ValidationOutcome.FAIL)
    )
    rejected = session.scalars(
        select(models.Approval.fingerprint)
        .where(models.Approval.incident_id == incident_pk)
        .where(models.Approval.status == models.ApprovalStatus.REJECTED)
    )
    dead = session.scalars(
        select(models.Execution.fingerprint)
        .where(models.Execution.incident_id == incident_pk)
        .where(
            models.Execution.result.in_(
                [models.ExecutionResult.REFUSED, models.ExecutionResult.ERROR]
            )
        )
    )
    return sorted({*failed, *rejected, *dead})


def _selected_candidate_state(session, incident: models.Incident) -> dict | None:
    """On entry at `validating`/`awaiting_approval`/`executing` -
    resumable non-terminal statuses where a selection must already exist -
    reload the persisted selected candidate so the validate/execute nodes
    have something to act on: in-memory selections do not survive between
    `helix run` invocations. Any other status starts with no selection."""
    if IncidentStatus(incident.status) not in (
        IncidentStatus.VALIDATING,
        IncidentStatus.AWAITING_APPROVAL,
        IncidentStatus.EXECUTING,
    ):
        return None
    row = session.scalar(
        select(models.Candidate)
        .where(models.Candidate.incident_id == incident.id)
        .where(models.Candidate.selected.is_(True))
        .order_by(models.Candidate.id.desc())
        .limit(1)
    )
    if row is None:
        return None
    return {
        "rank": row.rank,
        "action": row.action,
        "params": row.params,
        "rationale": row.rationale,
        "risk_tier": row.risk_tier,
        "score": row.score,
    }


def _latest_hypothesis_state(session, incident: models.Incident) -> dict | None:
    """Reload the newest hypotheses row for statuses where a resumed run
    may plan again without rediagnosing - remediation_pending (a replan
    after a rejected approval or a crash), validating, awaiting_approval,
    executing, verifying. At `received`/`diagnosing`/`failed` a fresh
    diagnosis produces its own hypothesis; loading a stale one there
    would let the coordinator skip diagnosis entirely."""
    if IncidentStatus(incident.status) not in (
        IncidentStatus.REMEDIATION_PENDING,
        IncidentStatus.VALIDATING,
        IncidentStatus.AWAITING_APPROVAL,
        IncidentStatus.EXECUTING,
        IncidentStatus.VERIFYING,
    ):
        return None
    row = session.scalar(
        select(models.Hypothesis)
        .where(models.Hypothesis.incident_id == incident.id)
        .order_by(models.Hypothesis.id.desc())
        .limit(1)
    )
    if row is None:
        return None
    return {
        "claim": row.claim,
        "evidence_refs": row.evidence_refs,
        "confidence": row.confidence,
        "contradicting_signals": row.contradicting_signals,
    }


def _latest_execution_state(
    session, incident: models.Incident, selected: dict | None
) -> dict | None:
    """On entry at `executing`, reload the newest executions row matching
    the selected candidate's fingerprint: a run that resumes after a crash
    must not re-fire an action that already completed (its row exists).
    No matching row means the earlier attempt never finished - the
    executor re-runs."""
    if IncidentStatus(incident.status) != IncidentStatus.EXECUTING or not selected:
        return None
    fp = candidate_fingerprint(selected["action"], selected.get("params") or {})
    row = session.scalar(
        select(models.Execution)
        .where(models.Execution.incident_id == incident.id)
        .where(models.Execution.fingerprint == fp)
        .order_by(models.Execution.id.desc())
        .limit(1)
    )
    if row is None:
        return None
    return {
        "execution_id": row.id,
        "action": row.action,
        "params": row.params,
        "fingerprint": row.fingerprint,
        "result": row.result.value,
        "detail": row.detail,
        "policy_decision": row.policy_decision,
        "approval_id": row.approval_id,
        "decided_by": row.decided_by,
    }


def _intake_node(deps: GraphDeps):
    """Load the incident (and its latest signal + prior evidence rows)
    into WorkflowState. Resets run-scoped fields so a re-run cannot
    inherit stale node output from a checkpoint."""

    def _intake(state: WorkflowState) -> dict:
        with trace_workflow_step("intake"):
            with deps.session_scope() as session:
                incident = session.scalar(
                    select(models.Incident).where(
                        models.Incident.incident_id == state["incident_id"]
                    )
                )
                if incident is None:
                    return {
                        "errors": [
                            *state.get("errors", []),
                            f"intake: no such incident {state['incident_id']}",
                        ]
                    }
                latest = incident.signals[-1] if incident.signals else None
                selected = _selected_candidate_state(session, incident)
                return {
                    "incident_pk": incident.id,
                    "service": incident.service,
                    "environment": incident.environment,
                    "severity": incident.severity,
                    "status": IncidentStatus(incident.status).value,
                    "signal": (
                        {
                            "source": latest.source,
                            "signature": latest.signature,
                            "raw": latest.raw,
                        }
                        if latest
                        else {}
                    ),
                    "evidence": [_evidence_from_row(row) for row in incident.evidence_items],
                    "attempts": incident.attempt_count,
                    # Run-scoped fields start clean every entry.
                    "diagnosis_complete": False,
                    "evidence_rounds": 0,
                    # At plan-capable statuses the persisted hypothesis
                    # reloads so a resumed replan has something to plan
                    # from; at received/diagnosing/failed diagnosis makes
                    # its own.
                    "hypothesis": _latest_hypothesis_state(session, incident),
                    "candidates": [],
                    # At `validating`/`awaiting_approval`/`executing` the
                    # persisted selection reloads so a re-run re-validates
                    # or re-executes it; elsewhere no selection yet.
                    "selected_action": selected,
                    "plan_complete": False,
                    "plan_failed": None,
                    "escalation_cause": None,
                    "validation": None,
                    "validation_attempts": 0,
                    "excluded_candidates": _excluded_fingerprints(session, incident.id),
                    "policy": None,
                    "approval": None,
                    # At `executing` a finished executions row reloads so
                    # a crash-resumed run does not double-execute.
                    "execution": _latest_execution_state(session, incident, selected),
                }

    return _intake


def _placeholder(name: str, subphase: str):
    """A registered node slot whose implementation lands later (D8 arms
    and 1.6-1.9 fill these in). Fails loudly if ever routed to."""

    def _node(state: WorkflowState) -> dict:
        raise NotImplementedError(f"node {name!r} lands in subphase {subphase}")

    return _node


#: Node slots per ablation arm (D8). The subphase each slot landed in is
#: the comment on the right; 1.9 (verify) and 1.11 (arm A/B variants)
#: remain placeholders.
_ARM_NODES: dict[str, dict[str, str]] = {
    "E": {
        "diagnosis": "1.5",
        "plan": "1.6",
        "validate": "1.7",
        "execute": "1.8",
        "verify": "1.9",
    },
    "B": {
        "diagnose_and_plan": "1.11",
        "execute": "1.8",
    },
    "A": {
        "rules_diagnosis": "1.11",
        "rules_plan": "1.11",
        "validate": "1.7",
        "execute": "1.8",
        "verify": "1.9",
    },
}


def build_graph(
    deps: GraphDeps,
    *,
    arm: str | None = None,
    checkpointer: Any = None,
    interrupt_before: list[str] | None = None,
):
    """Compose the workflow graph for `arm` and compile it.

    `checkpointer` is a LangGraph saver (see `open_checkpointer`); with
    one attached, thread_id = incident_id makes runs suspendable and
    resumable by any process. `interrupt_before` exists for tests and the
    1.8 approval gate - it parks the run ahead of the named nodes so the
    checkpoint round-trip can be exercised."""
    from langgraph.graph import START, StateGraph

    arm = (arm or get_settings().arm).upper()
    if arm not in _ARM_NODES:
        raise ValueError(f"unknown HELIX_ARM {arm!r}; expected one of {sorted(_ARM_NODES)}")

    builder = StateGraph(WorkflowState)
    builder.add_node("intake", _intake_node(deps))
    builder.add_node("coordinator", coordinator_node(deps))
    for name, subphase in _ARM_NODES[arm].items():
        node = _placeholder(name, subphase)
        if arm == "E" and name == "diagnosis":
            node = diagnosis_node(deps)
        elif arm == "E" and name == "plan":
            node = planner_node(deps)
        elif name == "validate" and arm in ("E", "A"):
            # The validate node is pure code - no LLM - so arm A shares
            # the real implementation rather than a placeholder.
            node = validation_node(deps)
        elif name == "execute":
            # The execute node is pure code - no LLM - shared by all arms.
            node = execution_node(deps)
        builder.add_node(name, node)

    builder.add_edge(START, "intake")
    builder.add_edge("intake", "coordinator")
    builder.add_conditional_edges("coordinator", make_router(arm))
    for name in _ARM_NODES[arm]:
        builder.add_edge(name, "coordinator")

    return builder.compile(
        checkpointer=checkpointer,
        interrupt_before=list(interrupt_before or ()),
    )


@contextmanager
def open_checkpointer(database_url: str) -> Iterator[Any]:
    """A PostgresSaver bound to `database_url` (D13).

    HELIX_DATABASE_URL is a SQLAlchemy URL (`postgresql+psycopg://...`);
    the checkpointer wants a plain psycopg conninfo, so the driver suffix
    is stripped. Non-Postgres URLs yield None - the documented fallback
    for stores (e.g. test SQLite) where suspend/resume has no substrate.
    `setup()` is idempotent, so it is safe to run per invocation.
    """
    if "postgresql" not in database_url and "postgres" not in database_url:
        yield None
        return
    from langgraph.checkpoint.postgres import PostgresSaver

    conninfo = database_url.replace("+psycopg", "", 1)
    with PostgresSaver.from_conn_string(conninfo) as saver:
        saver.setup()
        yield saver
