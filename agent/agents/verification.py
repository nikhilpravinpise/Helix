"""Verification Agent node (subphase 1.9): did the executed action leave
the system in the desired state?

Flow per run of the node:

1. Resolve WHAT to verify from durable state: the executions row this
   run produced (`state["execution"]["execution_id"]`), else the newest
   successful executions row for the incident - a resumed `verifying`
   incident verifies the same recorded action, never re-executes it.
2. Dispatch on the action to per-action desired-state legs
   (`agent/verification/`): k8s runtime actions assert pods Ready with
   no bad waiting reasons + rollout complete + ELK error rate under
   threshold + the port-forward functional leg; `rerun_pipeline` asserts
   the latest build green; `open_fix_pr` asserts the PR exists carrying
   the intended files. Sampled legs must HOLD across HELIX_VERIFY_WINDOW
   at HELIX_VERIFY_POLL intervals - flapping fails.
3. Persist a `verifications` row (incident, execution, action, params,
   fingerprint, per-check JSONB, outcome, window timestamps, attempt,
   `needs_compensation`) and write the report into `state["verification"]`.

The node is evidence-read-only plus the functional probe (a port-forward
tunnel and HTTP/browser GETs/POSTs against the service - observation, not
mutation). It NEVER executes remediation actions: the catalogue's write
transports are not reachable from this module by construction.

The coordinator owns what happens next: pass -> RECOVERED; fail ->
REMEDIATION_PENDING with the failed fingerprint excluded from the replan
- except compensable failures, which the `compensate` node (1.10) undoes
first while the recorded `needs_compensation` debt is unpaid, and
irreversible failures, which escalate directly.
"""

from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from agent.agents.compensation import outstanding_compensation
from agent.state import WorkflowState
from agent.verification import VerificationContext, run_verification
from helix.observability import trace_workflow_step
from state import models

if TYPE_CHECKING:
    from agent.graph import GraphDeps


def _execution_for(session, state: WorkflowState) -> models.Execution | None:
    """The executions row to verify: the one this run produced, else the
    newest successful REMEDIATION row for the incident (the resume path -
    compensation rows are undos, never the action being verified)."""
    execution = state.get("execution") or {}
    if execution.get("execution_id"):
        row = session.get(models.Execution, int(execution["execution_id"]))
        if row is not None:
            return row
    return session.scalar(
        select(models.Execution)
        .where(models.Execution.incident_id == state["incident_pk"])
        .where(models.Execution.result == models.ExecutionResult.SUCCESS)
        .where(models.Execution.compensates_id.is_(None))
        .order_by(models.Execution.id.desc())
        .limit(1)
    )


def _namespace(state: WorkflowState, params: dict[str, Any]) -> str:
    """The namespace checks scope to: the action's params, else the
    signal's, else the service name (the sandbox convention)."""
    if params.get("namespace"):
        return str(params["namespace"])
    signal = state.get("signal") or {}
    raw = signal.get("raw") or {}
    signature = signal.get("signature") or {}
    return str(raw.get("namespace") or signature.get("namespace") or state.get("service"))


def verification_node(deps: "GraphDeps"):
    """The verify graph node: dispatch -> persist -> state. Wrapped in
    trace_workflow_step like every node (C4)."""

    def _verify(state: WorkflowState) -> dict:
        with trace_workflow_step("verify"):
            errors: list[str] = list(state.get("errors", []))
            with deps.session_scope() as session:
                execution = _execution_for(session, state)
                if execution is None:
                    errors.append("verify: no successful execution to verify")
                    report = run_verification(
                        VerificationContext(
                            kubernetes=deps.kubernetes,
                            elk=deps.elk,
                            jenkins=deps.jenkins,
                            github=deps.github,
                            service=state.get("service") or "unknown",
                            namespace=_namespace(state, {}),
                            origin=deps.write_origin,
                            functional_enabled=False,
                            elk_error_threshold=deps.elk_error_threshold,
                            functional_base_url=deps.verify_base_url,
                        ),
                        "(none)",
                        {},
                        None,
                        window_s=0,
                        poll_s=deps.verify_poll,
                        sleep=deps.sleep,
                    )
                else:
                    params = execution.params or {}
                    ctx = VerificationContext(
                        kubernetes=deps.kubernetes,
                        elk=deps.elk,
                        jenkins=deps.jenkins,
                        github=deps.github,
                        service=state.get("service") or execution.action,
                        namespace=_namespace(state, params),
                        origin=deps.write_origin,
                        functional_enabled=deps.verify_functional,
                        elk_error_threshold=deps.elk_error_threshold,
                        functional_base_url=deps.verify_base_url,
                    )
                    report = run_verification(
                        ctx,
                        execution.action,
                        params,
                        execution.output,
                        window_s=deps.verify_window,
                        poll_s=deps.verify_poll,
                        sleep=deps.sleep,
                        settle_s=deps.verify_settle,
                    )
                session.add(
                    models.Verification(
                        incident_id=state["incident_pk"],
                        execution_id=execution.id if execution is not None else None,
                        candidate_id=(execution.candidate_id if execution is not None else None),
                        attempt=state.get("attempts", 0),
                        action=report.action,
                        params=report.params,
                        fingerprint=execution.fingerprint if execution is not None else "",
                        outcome=models.VerificationOutcome(report.outcome),
                        detail=report.detail,
                        checks=report.checks_json(),
                        window_seconds=report.window_seconds,
                        poll_seconds=report.poll_seconds,
                        needs_compensation=report.needs_compensation,
                        started_at=report.started_at,
                        finished_at=report.finished_at,
                    )
                )
                outstanding = outstanding_compensation(session, state["incident_pk"])
            return {
                "verification": {
                    "action": report.action,
                    "params": report.params,
                    "fingerprint": execution.fingerprint if execution is not None else "",
                    "outcome": report.outcome,
                    "detail": report.detail,
                    "reversibility": report.reversibility,
                    "checks": report.checks_json(),
                    "window_seconds": report.window_seconds,
                    "started_at": report.started_at.isoformat() if report.started_at else None,
                    "finished_at": report.finished_at.isoformat() if report.finished_at else None,
                    "needs_compensation": report.needs_compensation,
                },
                "needs_compensation": outstanding or report.needs_compensation,
                "errors": errors,
            }

    return _verify
