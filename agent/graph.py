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
(agent/agents/coordinator.py). For 1.5 the run ends at
`remediation_pending` - plan/validate/execute/verify are registered slots
whose bodies land in 1.6-1.9; routing to them is added alongside.

Arms (D8): HELIX_ARM selects node composition, not code forks.
- E: full graph - real diagnosis node plus placeholder slots for the rest.
- B: `diagnose_and_plan` (one fused LLM call) + `execute`; no
  validate/verify/replan nodes.
- A: `rules_diagnosis` + `rules_plan` deterministic variants; the
  deterministic validate/execute/verify slots are shared with E.
Arm A/B variants are placeholders until 1.11; the composition switch is
what lands now.

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
from agent.state import WorkflowState
from helix.config import get_settings
from helix.llm import LLMClient, OpenAICompatibleLLMClient
from helix.observability import trace_workflow_step
from integrations.capabilities import (
    DockerEvidence,
    ElkEvidence,
    GitHubEvidence,
    JenkinsEvidence,
    KubernetesEvidence,
    get_docker_evidence,
    get_elk_evidence,
    get_github_evidence,
    get_jenkins_evidence,
    get_kubernetes_evidence,
)
from integrations.evidence import Evidence
from state import models
from state.db import session_scope
from state.machine import IncidentStatus


@dataclass(frozen=True)
class GraphDeps:
    """Everything the graph needs that is not part of WorkflowState:
    the session factory for persisted state, the LLM client, the five
    evidence capabilities, and the evidence-round bound. Nodes see only
    Helix-vocabulary capability Protocols - never vendor SDK types (C8)."""

    session_scope: Callable[[], Any]  # context manager yielding a Session
    llm: LLMClient
    kubernetes: KubernetesEvidence
    jenkins: JenkinsEvidence
    elk: ElkEvidence
    github: GitHubEvidence
    docker: DockerEvidence
    database_url: str
    max_evidence_rounds: int = 3


def default_deps() -> GraphDeps:
    """Wire deps from settings: live or fixture capabilities per
    HELIX_EVIDENCE_SOURCE, the configured LLM client, HELIX_DATABASE_URL."""
    settings = get_settings()
    return GraphDeps(
        session_scope=session_scope,
        llm=OpenAICompatibleLLMClient(),
        kubernetes=get_kubernetes_evidence(),
        jenkins=get_jenkins_evidence(),
        elk=get_elk_evidence(),
        github=get_github_evidence(),
        docker=get_docker_evidence(),
        database_url=settings.database_url,
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
                    "hypothesis": None,
                }

    return _intake


def _placeholder(name: str, subphase: str):
    """A registered node slot whose implementation lands later (D8 arms
    and 1.6-1.9 fill these in). Fails loudly if ever routed to."""

    def _node(state: WorkflowState) -> dict:
        raise NotImplementedError(f"node {name!r} lands in subphase {subphase}")

    return _node


#: Node slots per ablation arm (D8). Arm E wires the real diagnosis node;
#: every other slot is a placeholder until its subphase lands.
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
        if arm == "E" and name == "diagnosis":
            builder.add_node(name, diagnosis_node(deps))
        else:
            builder.add_node(name, _placeholder(name, subphase))

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
