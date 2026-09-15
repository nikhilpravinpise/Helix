"""Decision provenance (subphase 1.9): one ordered timeline per incident,
assembled from stored rows.

"How did the system arrive at this action and this outcome?" - the
answer is a pure read over the incident's rows: `assemble_provenance`
takes a session + an INC-* id and returns a JSON-serializable record (no
external calls, no writes - every entry resolves to a stored row id and
timestamp). The sections follow CONTEXT.md section 15's order:

    trigger -> evidence -> hypothesis -> candidates -> decision ->
    validation -> approval -> execution -> verification

`decision` is the one derived stage: the newest selected candidate plus
the policy verdict and approval that governed its execution - resolved
through candidate_id foreign keys so the entry still points at stored
rows, not memory.

`render_markdown` formats the same record for `helix provenance INC-*`.
The JSON record is the API surface (GET /incidents/{id}/provenance), the
data source the Phase 1.12 operator timeline will render, and the audit
handoff artifact the phase closes on.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from state import models


def _ts(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _incident_block(incident: models.Incident) -> dict[str, Any]:
    return {
        "incident_id": incident.incident_id,
        "service": incident.service,
        "environment": incident.environment,
        "severity": incident.severity,
        "status": incident.status.value,
        "attempt_count": incident.attempt_count,
        "fingerprint": incident.fingerprint,
        "linked_pr": incident.linked_pr,
        "linked_deployment": incident.linked_deployment,
        "created_at": _ts(incident.created_at),
        "updated_at": _ts(incident.updated_at),
    }


def _trigger(signals: list[models.Signal]) -> list[dict[str, Any]]:
    return [
        {
            "signal_id": s.id,
            "source": s.source,
            "environment": s.environment,
            "severity": s.severity,
            "signature": s.signature,
            "raw": s.raw,
            "created_at": _ts(s.created_at),
        }
        for s in signals
    ]


def _evidence(items: list[models.EvidenceItem]) -> list[dict[str, Any]]:
    return [
        {
            "id": row.id,
            "evidence_id": row.evidence_id,
            "source": row.source,
            "kind": row.kind,
            "service": row.service,
            "summary": row.summary,
            "origin": row.origin,  # live | fixture - preserved, never blurred
            "truncated": row.truncated,
            "total": row.total,
            "collected_at": _ts(row.collected_at),
        }
        for row in items
    ]


def _hypotheses(rows: list[models.Hypothesis]) -> list[dict[str, Any]]:
    return [
        {
            "id": row.id,
            "claim": row.claim,
            "evidence_refs": row.evidence_refs,
            "confidence": row.confidence,
            "contradicting_signals": row.contradicting_signals,
            "attempt": row.attempt,
            "created_at": _ts(row.created_at),
        }
        for row in rows
    ]


def _candidates(rows: list[models.Candidate]) -> list[dict[str, Any]]:
    return [
        {
            "id": row.id,
            "attempt": row.attempt,
            "rank": row.rank,
            "action": row.action,
            "params": row.params,
            "rationale": row.rationale,
            "evidence_refs": row.evidence_refs,
            "score": row.score,
            "risk_tier": row.risk_tier,
            "components": row.components,
            "selected": row.selected,
            "created_at": _ts(row.created_at),
        }
        for row in rows
    ]


def _decision(
    session: Session, incident_pk: int, selected: models.Candidate | None
) -> dict[str, Any] | None:
    """The "decision" stage: the newest selected candidate plus the policy
    verdict and approval that governed it - resolved through stored rows
    (executions/approvals rows carry `candidate_id` FKs back to it)."""
    if selected is None:
        return None
    execution = session.scalar(
        select(models.Execution)
        .where(models.Execution.incident_id == incident_pk)
        .where(models.Execution.candidate_id == selected.id)
        .order_by(models.Execution.id.desc())
        .limit(1)
    )
    approval = session.scalar(
        select(models.Approval)
        .where(models.Approval.incident_id == incident_pk)
        .where(models.Approval.candidate_id == selected.id)
        .order_by(models.Approval.id.desc())
        .limit(1)
    )
    return {
        "candidate_id": selected.id,
        "action": selected.action,
        "params": selected.params,
        "rationale": selected.rationale,
        "score": selected.score,
        "risk_tier": selected.risk_tier,
        "policy_decision": execution.policy_decision if execution else None,
        "execution_id": execution.id if execution else None,
        "approval_id": approval.id if approval else None,
        "approval_status": approval.status.value if approval else None,
        "decided_by": approval.decided_by if approval else None,
        "decided_at": _ts(approval.decided_at) if approval else None,
        "created_at": _ts(selected.created_at),
    }


def _validations(rows: list[models.Validation]) -> list[dict[str, Any]]:
    return [
        {
            "id": row.id,
            "candidate_id": row.candidate_id,
            "attempt": row.attempt,
            "action": row.action,
            "params": row.params,
            "fingerprint": row.fingerprint,
            "outcome": row.outcome.value,
            "reason": row.reason,
            "details": row.details,
            "created_at": _ts(row.created_at),
        }
        for row in rows
    ]


def _approvals(rows: list[models.Approval]) -> list[dict[str, Any]]:
    return [
        {
            "id": row.id,
            "candidate_id": row.candidate_id,
            "action": row.action,
            "params": row.params,
            "fingerprint": row.fingerprint,
            "status": row.status.value,
            "decided_by": row.decided_by,
            "decided_at": _ts(row.decided_at),
            "note": row.note,
            "created_at": _ts(row.created_at),
        }
        for row in rows
    ]


def _executions(rows: list[models.Execution]) -> list[dict[str, Any]]:
    return [
        {
            "id": row.id,
            "candidate_id": row.candidate_id,
            "attempt": row.attempt,
            "action": row.action,
            "params": row.params,
            "fingerprint": row.fingerprint,
            "policy_decision": row.policy_decision,
            "verification_required": row.verification_required,
            "decided_by": row.decided_by,
            "approval_id": row.approval_id,
            "origin": row.origin,
            "result": row.result.value,
            "detail": row.detail,
            "output": row.output,
            "started_at": _ts(row.started_at),
            "finished_at": _ts(row.finished_at),
        }
        for row in rows
    ]


def _verifications(rows: list[models.Verification]) -> list[dict[str, Any]]:
    return [
        {
            "id": row.id,
            "execution_id": row.execution_id,
            "candidate_id": row.candidate_id,
            "attempt": row.attempt,
            "action": row.action,
            "params": row.params,
            "fingerprint": row.fingerprint,
            "outcome": row.outcome.value,
            "detail": row.detail,
            "checks": row.checks,
            "window_seconds": row.window_seconds,
            "poll_seconds": row.poll_seconds,
            "needs_compensation": row.needs_compensation,
            "started_at": _ts(row.started_at),
            "finished_at": _ts(row.finished_at),
        }
        for row in rows
    ]


def _transitions(rows: list[models.Transition]) -> list[dict[str, Any]]:
    return [
        {
            "id": row.id,
            "from_status": row.from_status.value if row.from_status else None,
            "to_status": row.to_status.value,
            "cause": row.cause,
            "created_at": _ts(row.created_at),
        }
        for row in rows
    ]


def assemble_provenance(session: Session, incident_id: str) -> dict[str, Any] | None:
    """The ordered provenance record for `incident_id`, or None when no
    such incident exists. Pure over its inputs: reads only, returns a
    JSON-serializable dict; every entry carries its stored row id and
    timestamp."""
    incident = session.scalar(
        select(models.Incident).where(models.Incident.incident_id == incident_id)
    )
    if incident is None:
        return None
    signals = sorted(incident.signals, key=lambda s: s.id)
    selected = next((c for c in reversed(incident.candidates) if c.selected), None)
    return {
        "incident": _incident_block(incident),
        "stages": {
            "trigger": _trigger(signals),
            "evidence": _evidence(list(incident.evidence_items)),
            "hypothesis": _hypotheses(list(incident.hypotheses)),
            "candidates": _candidates(list(incident.candidates)),
            "decision": _decision(session, incident.id, selected),
            "validation": _validations(list(incident.validations)),
            "approval": _approvals(list(incident.approvals)),
            "execution": _executions(list(incident.executions)),
            "verification": _verifications(list(incident.verifications)),
        },
        "transitions": _transitions(list(incident.transitions)),
    }


def render_markdown(record: dict[str, Any]) -> str:
    """The provenance record as Markdown - `helix provenance INC-*`
    output. Sections render in CONTEXT.md order; every line resolves to
    the stored row id and timestamp the JSON record carries."""
    incident = record["incident"]
    stages = record["stages"]
    lines = [
        f"# Provenance: {incident['incident_id']}",
        "",
        f"- service: {incident['service']} | environment: {incident['environment']} "
        f"| severity: {incident['severity']}",
        f"- status: {incident['status']} | attempts: {incident['attempt_count']}",
        f"- opened: {incident['created_at']} | updated: {incident['updated_at']}",
    ]
    if incident.get("linked_pr"):
        lines.append(f"- linked PR: {incident['linked_pr']}")
    if incident.get("linked_deployment"):
        lines.append(f"- linked deployment: {incident['linked_deployment']}")

    lines += ["", "## Trigger"]
    if stages["trigger"]:
        for s in stages["trigger"]:
            lines.append(
                f"- signal #{s['signal_id']} ({s['source']}) at {s['created_at']}: "
                f"{s['severity']} on {s['environment']}; signature {s['signature']}"
            )
    else:
        lines.append("- none recorded")

    lines += ["", "## Evidence"]
    if stages["evidence"]:
        for e in stages["evidence"]:
            lines.append(
                f"- {e['evidence_id']} ({e['source']}/{e['kind']}, {e['origin']}) "
                f"at {e['collected_at']}: {e['summary']}"
                + (" [truncated]" if e["truncated"] else "")
            )
    else:
        lines.append("- none recorded")

    lines += ["", "## Hypothesis"]
    if stages["hypothesis"]:
        for h in stages["hypothesis"]:
            refs = ", ".join(h["evidence_refs"]) or "none"
            lines.append(
                f"- #{h['id']} (attempt {h['attempt']}, confidence {h['confidence']:.2f}) "
                f"at {h['created_at']}: {h['claim']} [refs: {refs}]"
            )
    else:
        lines.append("- none recorded")

    lines += ["", "## Candidates"]
    if stages["candidates"]:
        for c in stages["candidates"]:
            mark = " [selected]" if c["selected"] else ""
            lines.append(
                f"- #{c['id']} (attempt {c['attempt']}, rank {c['rank']}, "
                f"score {c['score']:.2f}, {c['risk_tier']}){mark} "
                f"{c['action']} {c['params']}: {c['rationale']}"
            )
    else:
        lines.append("- none recorded")

    lines += ["", "## Decision"]
    d = stages["decision"]
    if d:
        lines.append(
            f"- selected candidate #{d['candidate_id']} {d['action']} "
            f"({d['risk_tier']} risk, score {d['score']:.2f})"
        )
        lines.append(
            f"- policy: {d['policy_decision'] or 'not recorded'}; approval: "
            + (
                f"#{d['approval_id']} {d['approval_status']} by {d['decided_by']} "
                f"at {d['decided_at']}"
                if d["approval_id"]
                else "none required"
            )
        )
    else:
        lines.append("- no selection recorded")

    lines += ["", "## Validation"]
    if stages["validation"]:
        for v in stages["validation"]:
            lines.append(
                f"- #{v['id']} (attempt {v['attempt']}) {v['action']}: "
                f"{v['outcome']} - {v['reason']} ({v['created_at']})"
            )
    else:
        lines.append("- none recorded")

    lines += ["", "## Approval"]
    if stages["approval"]:
        for a in stages["approval"]:
            lines.append(
                f"- #{a['id']} {a['action']}: {a['status']}"
                + (f" by {a['decided_by']} at {a['decided_at']}" if a["decided_by"] else "")
                + (f" - {a['note']}" if a.get("note") else "")
            )
    else:
        lines.append("- none required")

    lines += ["", "## Execution"]
    if stages["execution"]:
        for e in stages["execution"]:
            lines.append(
                f"- #{e['id']} (attempt {e['attempt']}) {e['action']}: {e['result']} "
                f"[policy={e['policy_decision']}, origin={e['origin']}] "
                f"{e['started_at']} -> {e['finished_at']}: {e['detail']}"
            )
    else:
        lines.append("- none recorded")

    lines += ["", "## Verification"]
    if stages["verification"]:
        for v in stages["verification"]:
            checks = (
                ", ".join(f"{c['name']}={c['status']}" for c in v["checks"] or []) or "no checks"
            )
            lines.append(
                f"- #{v['id']} (attempt {v['attempt']}) {v['action']}: {v['outcome']} "
                f"[{checks}] {v['started_at']} -> {v['finished_at']}"
                + (" [needs_compensation]" if v["needs_compensation"] else "")
            )
            for c in v["checks"] or []:
                lines.append(f"  - {c['name']}: {c['status']} - {c['detail']}")
    else:
        lines.append("- none recorded")

    lines += ["", "## Transitions"]
    for t in record["transitions"]:
        lines.append(
            f"- #{t['id']} {t['from_status'] or '(new)'} -> {t['to_status']} "
            f"at {t['created_at']}: {t['cause']}"
        )
    return "\n".join(lines) + "\n"
