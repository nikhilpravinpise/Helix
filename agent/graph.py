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
per-family strategies in agent/validation/), `execute` the real
executor in 1.8 (the safety-policy gate lives in the coordinator's
routing plus the executor's own re-check), `verify` the real
Verification Agent in 1.9 (per-action desired-state checks over a
stability window - agent/verification/), and `compensate` the 1.10 undo
node (agent/agents/compensation.py - a failed verification on a
compensable action runs the catalogue's compensating_action before the
coordinator replans or escalates). Runs also end at
`awaiting_approval` (policy requires a human decision - the run suspends
there until an approval row is decided, or the approval-timeout sweep
escalates it), `remediation_pending` ->
replanned -> back through `validating` on a failed check or a failed
verification (bounded by the HELIX_MAX_ATTEMPTS retry budget),
`recovered` (verification held), `escalated` (planner
escalated, the single validation retry was exhausted, an irreversible
action failed verification, a compensation failed, or the retry budget
ran out) or `failed`. Every arm node is wrapped in
`_with_step_timeout` (HELIX_STEP_TIMEOUT): a node that outlives the
bound reports `node_failed` and the coordinator fails the run instead
of hanging it.

Arms (D8): HELIX_ARM selects node composition, not code forks.
- E: full graph - real diagnosis + plan + validate + execute + verify.
- B: `diagnose_and_plan` (one fused LLM call, 1.11) + `execute`; no
  validate/verify/replan nodes - the coordinator skips straight to the
  shared policy gate and the run parks at `verifying` as its designed
  end state.
- A: `rules_diagnosis` + `rules_plan` deterministic rule tables
  (agent/rules.py, 1.11 - zero LLM calls); validate/execute/verify/
  compensate are shared with E.

Checkpointer (D13): `open_checkpointer()` yields a PostgresSaver bound to
HELIX_DATABASE_URL (thread_id = incident_id), which is what makes a
suspended run resumable by any process. Non-Postgres URLs yield None -
the documented fallback: state then lives only for the invoke.
"""

import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from agent.agents.compensation import compensation_node, outstanding_compensation
from agent.agents.coordinator import coordinator_node, make_router
from agent.agents.execution import execution_node
from agent.agents.fused import diagnose_and_plan_node
from agent.agents.review import review_node
from agent.agents.triage import triage_node
from agent.agents.validation import validation_node
from agent.agents.verification import verification_node
from agent.ops import ops_agent_nodes
from agent.rules import rules_diagnosis_node, rules_plan_node
from agent.state import WorkflowState
from agent.validation import candidate_fingerprint
from events.schemas import EventSource
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
    # 1.9 - verification window/poll/threshold and the functional toggle;
    # `sleep` is the test seam: unit tests inject a counting fake so the
    # stability window does not cost real seconds.
    verify_window: float = 60.0
    verify_poll: float = 20.0
    # Seconds the verify node waits for a deployment rollout to converge
    # before the stability window opens - without it, intentional pod
    # turnover mid-rollout reads as flapping.
    verify_settle: float = 120.0
    elk_error_threshold: int = 0
    verify_functional: bool = True
    # 1.13 - functional-leg transport: None = kubectl port-forward; set =
    # probe this ingress base URL (e.g. http://payment-api.local).
    verify_base_url: str | None = None
    sleep: Callable[[float], None] = time.sleep
    # 1.10 - unhappy-path bounds: per-incident retry budget (cycles of
    # diagnose->...->verify consumed), seconds a pending approvals row may
    # sit before the runner escalates it, and the wall-clock bound on one
    # graph node inside a run. Zeros disable the timeout sweeps.
    max_attempts: int = 3
    approval_timeout: float = 86400.0
    step_timeout: float = 600.0


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
        verify_window=settings.verify_window,
        verify_poll=settings.verify_poll,
        verify_settle=settings.verify_settle,
        elk_error_threshold=settings.elk_error_threshold,
        verify_functional=settings.verify_functional,
        verify_base_url=settings.verify_base_url,
        max_attempts=settings.max_attempts,
        approval_timeout=settings.approval_timeout,
        step_timeout=settings.step_timeout,
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
    in-state list from. Union of four row sources: `fail` validations,
    `rejected` approvals (an operator declined this exact action+params),
    refused/error executions (a denied or already-failed write is not
    blindly retried), and `fail` verifications (an action that ran and
    demonstrably did not recover the system is never blindly repeated)."""
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
    unverified = session.scalars(
        select(models.Verification.fingerprint)
        .where(models.Verification.incident_id == incident_pk)
        .where(models.Verification.outcome == models.VerificationOutcome.FAIL)
    )
    return sorted({*failed, *rejected, *dead, *unverified})


def _selected_candidate_state(session, incident: models.Incident, arm: str = "E") -> dict | None:
    """On entry at `validating`/`awaiting_approval`/`executing`/`verifying`
    - resumable non-terminal statuses where a selection must already exist -
    reload the persisted selected candidate so the validate/execute/verify
    nodes have something to act on: in-memory selections do not survive
    between `helix run` invocations. Any other status starts with no
    selection.

    Arm B and C additionally reload at `remediation_pending` (1.11): their
    fused `diagnose_and_plan` node produced and persisted the selection
    inside the `diagnosing` hop, and neither has a plan node to re-derive
    it - the reload is what carries a B/C run from remediation_pending
    onward."""
    statuses = {
        IncidentStatus.VALIDATING,
        IncidentStatus.AWAITING_APPROVAL,
        IncidentStatus.EXECUTING,
        IncidentStatus.VERIFYING,
    }
    if arm in ("B", "C", "D"):
        statuses.add(IncidentStatus.REMEDIATION_PENDING)
    if IncidentStatus(incident.status) not in statuses:
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
    """On entry at `executing`/`verifying`, reload the newest executions
    row matching the selected candidate's fingerprint: a run that resumes
    after a crash must not re-fire an action that already completed (its
    row exists), and a resumed `verifying` run verifies the recorded
    action. No matching row means the earlier attempt never finished -
    the executor re-runs."""
    if (
        IncidentStatus(incident.status)
        not in (
            IncidentStatus.EXECUTING,
            IncidentStatus.VERIFYING,
        )
        or not selected
    ):
        return None
    fp = candidate_fingerprint(selected["action"], selected.get("params") or {})
    row = session.scalar(
        select(models.Execution)
        .where(models.Execution.incident_id == incident.id)
        .where(models.Execution.fingerprint == fp)
        # Compensation rows are undos, not the remediation being resumed -
        # a rollback's compensating rollback shares the fingerprint, so
        # exclude them explicitly (1.10).
        .where(models.Execution.compensates_id.is_(None))
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


def _latest_verification_state(
    session, incident: models.Incident, execution: dict | None
) -> dict | None:
    """On entry at `verifying` with a finished executions row already
    reloaded, the newest verifications row FOR THAT execution is the
    completed verdict a crash interrupted before the coordinator could
    route it - reload it so the run routes on the recorded verdict
    instead of re-sampling a whole window. A verifications row for a
    different (older) execution is stale history, not a verdict."""
    if IncidentStatus(incident.status) != IncidentStatus.VERIFYING or not execution:
        return None
    row = session.scalar(
        select(models.Verification)
        .where(models.Verification.incident_id == incident.id)
        .where(models.Verification.execution_id == execution["execution_id"])
        .order_by(models.Verification.id.desc())
        .limit(1)
    )
    if row is None:
        return None
    return {
        "action": row.action,
        "params": row.params,
        "fingerprint": row.fingerprint,
        "outcome": row.outcome.value,
        "detail": row.detail,
        "checks": row.checks,
        "window_seconds": row.window_seconds,
        "needs_compensation": row.needs_compensation,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
    }


def _outstanding_compensation(session, incident_pk: int) -> bool:
    """Rebuild the in-state debt flag on every entry: a `needs_compensation`
    verifications row is the durable record that a compensable action
    failed verification and its change was never undone - "unpaid" means
    no successful executions row compensating it (1.10). The definition
    lives in agent/agents/compensation.py so intake, verify and
    compensate cannot disagree on what outstanding means."""
    return outstanding_compensation(session, incident_pk)


def _signal_from_events(incident: models.Incident) -> dict[str, Any]:
    """Fallback `signal` view for an incident with no `state.models.Signal`
    row (subphase E7): an incident opened by `events.ingest.ingest_event`
    carries its trigger only as a canonical `EventRecord`, and
    `agent/rules.py`'s `match_rule` and `agent/agents/diagnosis.py`'s
    `_signal_context` both read the `{source, signature, raw}` shape the
    direct intake path builds from `state.models.Signal`.

    Reconstructed from the most recent non-Helix event: `signature` and
    `raw` come straight out of that event's `metadata`, which is where
    `state.intake.build_event` puts them for a Phase 1 signal ingested
    through the event path, and where a vendor-adapter event's own detail
    lives too. A key `metadata` genuinely does not carry is left out
    rather than defaulted to `{}` - `match_rule` and `_signal_context`
    already treat an absent key as "nothing here", so inventing one would
    only hide what the event actually contained.

    Helix's own lifecycle events (`incident.created` and the rest) are
    excluded - they describe what Helix did, not what was reported to it,
    the same exclusion `events.ingest.correlated_sources` and
    `events.correlation` make.
    """
    events = [r for r in incident.event_records if r.source != EventSource.HELIX.value]
    if not events:
        return {}
    latest = events[-1]  # `event_records` is relationship-ordered by id (state/models.py)
    metadata = (latest.envelope or {}).get("metadata") or {}
    signal: dict[str, Any] = {"source": latest.source}
    if "signature" in metadata:
        signal["signature"] = metadata["signature"]
    if "raw" in metadata:
        signal["raw"] = metadata["raw"]
    return signal


def _intake_node(deps: GraphDeps, arm: str = "E"):
    """Load the incident (and its latest signal + prior evidence rows)
    into WorkflowState. Resets run-scoped fields so a re-run cannot
    inherit stale node output from a checkpoint. `arm` selects the
    composition the run is resuming under (1.11)."""

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
                selected = _selected_candidate_state(session, incident, arm)
                execution = _latest_execution_state(session, incident, selected)
                return {
                    "incident_pk": incident.id,
                    "service": incident.service,
                    "environment": incident.environment,
                    "severity": incident.severity,
                    "status": IncidentStatus(incident.status).value,
                    # Phase 2 (2.1): which lifecycle this incident follows.
                    "kind": incident.kind.value,
                    "triage": None,
                    "triage_complete": False,
                    "review": None,
                    "review_complete": False,
                    "fix_pushed": False,
                    "signal": (
                        {
                            "source": latest.source,
                            "signature": latest.signature,
                            "raw": latest.raw,
                        }
                        if latest
                        else _signal_from_events(incident)
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
                    # Arm B/C at remediation_pending: the fused node's work
                    # is already done - with a persisted selection the run
                    # proceeds to validating; without one (the fused call
                    # escalated) plan_complete=True lets the coordinator
                    # land the escalation instead of parking forever.
                    "plan_complete": bool(
                        arm in ("B", "C", "D")
                        and IncidentStatus(incident.status) == IncidentStatus.REMEDIATION_PENDING
                    ),
                    "plan_failed": None,
                    "escalation_cause": None,
                    "validation": None,
                    "validation_attempts": 0,
                    "excluded_candidates": _excluded_fingerprints(session, incident.id),
                    "policy": None,
                    "approval": None,
                    # At `executing`/`verifying` a finished executions row
                    # reloads so a crash-resumed run does not double-execute
                    # and verifies the recorded action.
                    "execution": execution,
                    # At `verifying` a completed verifications row for THIS
                    # execution reloads so a crash-interrupted run routes on
                    # the recorded verdict; otherwise the node re-verifies
                    # (a stale in-state report must never close an incident).
                    "verification": _latest_verification_state(session, incident, execution),
                    # The recorded compensation debt survives across runs
                    # until the compensate node (1.10) pays it.
                    "needs_compensation": _outstanding_compensation(session, incident.id),
                    # 1.10 run-scoped fields start clean: the compensate
                    # node's report and a timed-out node's marker never
                    # leak across run boundaries.
                    "compensation": None,
                    "node_failed": None,
                }

    return _intake


def _placeholder(name: str, subphase: str):
    """A registered node slot whose implementation lands later (D8 arms
    and 1.6-1.9 fill these in). Fails loudly if ever routed to."""

    def _node(state: WorkflowState) -> dict:
        raise NotImplementedError(f"node {name!r} lands in subphase {subphase}")

    return _node


#: Node slots per ablation arm (D8). The subphase each slot landed in is
#: the comment on the right; the arm A/B variants (1.11) remain
#: placeholders.
_ARM_NODES: dict[str, dict[str, str]] = {
    "E": {
        "diagnosis": "1.5",
        "plan": "1.6",
        "validate": "1.7",
        "execute": "1.8",
        "verify": "1.9",
        "compensate": "1.10",
    },
    "B": {
        "diagnose_and_plan": "1.11",
        "execute": "1.8",
    },
    # C is B plus retrieval (E6). Identical composition on purpose: the
    # only difference is that the fused node is given prior resolved
    # incidents as context, so a C-vs-B comparison isolates what RAG buys
    # over a bare LLM instead of confounding it with validation or
    # verification.
    "C": {
        "diagnose_and_plan": "1.11",
        "execute": "1.8",
    },
    # D is C plus the Knowledge Agent's GraphRAG context (2.8): same
    # single-call composition, so a C-vs-D comparison isolates what the
    # event timeline + relationship graph add over retrieval alone.
    "D": {
        "diagnose_and_plan": "2.8",
        "execute": "1.8",
    },
    "A": {
        "rules_diagnosis": "1.11",
        "rules_plan": "1.11",
        "validate": "1.7",
        "execute": "1.8",
        "verify": "1.9",
        "compensate": "1.10",
    },
}


def _with_step_timeout(node_fn: Callable, name: str, timeout_s: float):
    """Bound one worker node's wall-clock inside a run (subphase 1.10).

    A node still running at HELIX_STEP_TIMEOUT does not hang the run: the
    wrapper returns a `node_failed` state update the coordinator turns
    into a FAILED transition (the failed -> diagnosing re-entry and retry
    budget then apply like any other failure). Implemented as a worker
    thread + `future.result(timeout)` so it works on Windows as well as
    Unix - no signals, no DB-layer timeout.

    Known Phase 1 tradeoff: a timed-out node's worker thread cannot be
    killed - it lingers until it finishes on its own. Its returned update
    is discarded (LangGraph already merged the timeout update), but its
    side effects may still land: a slow transport could write after the
    run failed. The persisted executions rows and the status machine
    remain the authoritative record of what happened, so the stray row is
    auditable rather than hidden. 0 disables the bound."""

    def _node(state: WorkflowState) -> dict:
        if timeout_s <= 0:
            return node_fn(state)
        pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"helix-step-{name}")
        future = pool.submit(node_fn, state)
        try:
            result = future.result(timeout=timeout_s)
        except TimeoutError:
            if future.done():
                # The node raised a TimeoutError itself - a node failure,
                # not the step deadline: let it propagate.
                pool.shutdown(wait=True)
                raise
            future.cancel()  # cancels only if the worker has not started
            pool.shutdown(wait=False, cancel_futures=True)
            cause = f"step timeout after {timeout_s:g}s"
            return {
                "errors": [*state.get("errors", []), f"{name}: {cause}"],
                "node_failed": {"node": name, "cause": cause},
            }
        except BaseException:
            pool.shutdown(wait=False, cancel_futures=True)
            raise
        pool.shutdown(wait=True)
        return result

    return _node


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
    builder.add_node("intake", _intake_node(deps, arm))
    builder.add_node("coordinator", coordinator_node(deps, arm))
    # Phase 2 (2.1/2.5/2.6): the change-review lifecycle's nodes are not
    # arm-gated - Triage and Review are deterministic regardless of which
    # ablation arm handles the Ops loop a failed deploy hands off to, so
    # every arm's graph carries them rather than duplicating them per arm.
    builder.add_node("triage", _with_step_timeout(triage_node(deps), "triage", deps.step_timeout))
    builder.add_node("review", _with_step_timeout(review_node(deps), "review", deps.step_timeout))
    builder.add_edge("triage", "coordinator")
    builder.add_edge("review", "coordinator")
    # 2.1: arm E's recovery-loop nodes come from the Ops Agent boundary
    # itself - the graph asks the agent for its node set rather than
    # knowing which `agent.agents.*` builders belong to it. Internals are
    # the same builders the elifs below give the other arms.
    ops_nodes = ops_agent_nodes(deps) if arm == "E" else {}
    for name, subphase in _ARM_NODES[arm].items():
        node = _placeholder(name, subphase)
        if name in ops_nodes:
            node = ops_nodes[name]
        elif arm in ("B", "C") and name == "diagnose_and_plan":
            # 1.11: the fused single-call node - one complete_structured
            # produces hypothesis + selection; no validate/verify/replan.
            # Arm C is the same node with retrieval context (E6).
            node = diagnose_and_plan_node(deps, retrieval=arm == "C")
        elif arm == "D" and name == "diagnose_and_plan":
            # 2.8: the same fused node as C, fed the composed GraphRAG
            # context (retrieval + event timeline + graph neighborhood)
            # instead of retrieval alone.
            node = diagnose_and_plan_node(deps, retrieval=True, knowledge=True)
        elif arm == "A" and name == "rules_diagnosis":
            # 1.11: deterministic rule tables - zero LLM calls.
            node = rules_diagnosis_node(deps)
        elif arm == "A" and name == "rules_plan":
            node = rules_plan_node(deps)
        elif name == "validate" and arm in ("E", "A"):
            # The validate node is pure code - no LLM - so arm A shares
            # the real implementation rather than a placeholder.
            node = validation_node(deps)
        elif name == "execute":
            # The execute node is pure code - no LLM - shared by all arms.
            node = execution_node(deps)
        elif name == "verify" and arm in ("E", "A"):
            # The verify node is pure code too - arm A shares it.
            node = verification_node(deps)
        elif name == "compensate" and arm in ("E", "A"):
            # The compensate node (1.10) is pure code - shared too.
            node = compensation_node(deps)
        builder.add_node(name, _with_step_timeout(node, name, deps.step_timeout))

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
