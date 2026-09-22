"""Incident-page builder (subphase 2.4): canonical state -> Notion blocks.

The page is a PROJECTION (CONTEXT.md section 13): PostgreSQL is truth, so
`build_incident_page` renders the whole incident snapshot from
`state.provenance.assemble_provenance` - the full trigger -> evidence ->
hypothesis -> candidates -> decision -> validation -> approval ->
execution -> verification chain - plus the incident row's own timeline,
triage, review and work-item state. Nothing is written back; a page is
rebuilt from stored rows at sync time, never edited into truth.

The outbox drain (integrations/outbox.py::_send_notion) creates the page
once - `ux_knowledge_pages_one_per_incident` is the "no duplicate pages"
guarantee - then APPENDS a fresh snapshot per milestone transition
(lifecycle_sync.PAGE_SYNC_STATUSES). Notion pages grow by append, never
rewrite, so each sync lands under a timestamped "Synced at" divider:
re-syncs are visibly additive and the page itself is the audit trail.

Two bounds are enforced here: rich_text content is clipped to ~1900 chars
(the API cap is 2000) and every block is a plain JSON dict - no vendor
objects cross the boundary (C8). Empty sections render "none recorded"
so the template is stable across syncs and a reader sees what has not
run yet.
"""

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from state import models
from state.machine import IncidentStatus
from state.provenance import assemble_provenance

#: Notion caps one rich_text element at 2000 chars; keep margin.
_MAX_TEXT = 1900


def _clip(text: Any) -> str:
    text = str(text)
    return text if len(text) <= _MAX_TEXT else text[: _MAX_TEXT - 3] + "..."


def _rich(text: str) -> list[dict[str, Any]]:
    return [{"type": "text", "text": {"content": _clip(text)}}]


def _block(kind: str, text: str = "") -> dict[str, Any]:
    """One Notion API block dict; `divider` carries no rich_text."""
    payload: dict[str, Any] = {} if kind == "divider" else {"rich_text": _rich(text)}
    return {"object": "block", "type": kind, kind: payload}


def _v(value: Any) -> Any:
    """Enum-or-str -> its serialized value (status columns are StrEnums)."""
    return getattr(value, "value", value)


def _ts(value: Any) -> str | None:
    """datetime -> isoformat; anything else (already-iso str, None) passes."""
    return value.isoformat() if isinstance(value, datetime) else value


def _section(blocks: list[dict[str, Any]], title: str, lines: list[str]) -> None:
    """One headed section. Empty stages render `none recorded` so the
    template is stable across syncs - a reader sees what has not run yet."""
    blocks.append(_block("heading_2", title))
    for line in lines or ["none recorded"]:
        blocks.append(_block("bulleted_list_item", line))


def _json(value: Any) -> str:
    """Compact deterministic rendering of a params/signature dict."""
    return json.dumps(value, sort_keys=True, default=str)


def _checks_text(checks: list[dict[str, Any]] | None) -> str:
    return ", ".join(f"{c['name']}={c['status']}" for c in checks or []) or "no checks"


def _decision_lines(decision: dict[str, Any] | None) -> list[str]:
    if decision is None:
        return []
    lines = [
        f"selected candidate #{decision['candidate_id']} {decision['action']} "
        f"({decision['risk_tier']} risk, score {decision['score']:.2f}); "
        f"policy: {decision['policy_decision'] or 'not recorded'}"
    ]
    if decision["approval_id"]:
        lines.append(
            f"approval #{decision['approval_id']} {decision['approval_status']} "
            f"by {decision['decided_by']} at {decision['decided_at']}"
        )
    else:
        lines.append("no approval required")
    return lines


def _outcome_lines(incident: models.Incident, stages: dict[str, Any]) -> list[str]:
    """The closing section: where the incident landed, and what is still
    owed - pending approvals/reviews, compensation debt, open work items."""
    lines = [f"outcome: {_v(incident.status)} (attempts {incident.attempt_count})"]
    escalated = [t for t in incident.transitions if t.to_status == IncidentStatus.ESCALATED]
    if escalated:
        # Same first-class surfacing provenance._incident_block gives it.
        lines.append(f"escalation cause: {escalated[-1].cause}")
    if incident.linked_pr:
        lines.append(f"linked PR: {incident.linked_pr}")
    if incident.linked_deployment:
        lines.append(f"linked deployment: {incident.linked_deployment}")
    followups = [
        f"approval #{a['id']} ({a['action']}) still pending"
        for a in stages.get("approval", [])
        if a["status"] == "pending"
    ]
    followups += [
        f"review #{r.id} for {r.reviewer} still pending"
        for r in incident.review_requests
        if _v(r.status) == "pending"
    ]
    followups += [
        f"compensation owed for execution #{v['execution_id']} (verification #{v['id']})"
        for v in stages.get("verification", [])
        if v["needs_compensation"]
    ]
    followups += [
        f"work item {w.external_key or w.external_id} ({w.provider}) still {w.status}"
        for w in incident.work_items
        if w.status not in ("done", "closed")
    ]
    return lines + (followups or ["no open follow-ups"])


def build_incident_page(
    session: Session, incident: models.Incident
) -> tuple[str, list[dict[str, Any]]]:
    """(title, blocks) for the drain's `sync_page` op.

    The title carries live status so the page header itself reflects the
    projection ("INC-7: payment-api (recovered)"). The blocks are a
    complete snapshot of canonical state; the drain appends them whole on
    each milestone sync - never diffed, so the render must be
    deterministic apart from the Synced-at marker.
    """
    status = _v(incident.status)
    title = f"{incident.incident_id}: {incident.service} ({status})"
    prov = assemble_provenance(session, incident.incident_id) or {}
    stages = prov.get("stages", {})

    synced = datetime.now(UTC).isoformat(timespec="seconds")
    blocks: list[dict[str, Any]] = [
        _block(
            "paragraph",
            f"Synced at {synced} - projection of PostgreSQL state; "
            "the incident row is canonical (CONTEXT.md section 13).",
        ),
        _block("divider"),
        _block("heading_2", "Summary"),
        _block(
            "paragraph",
            f"{_v(incident.kind)} incident {incident.incident_id} on {incident.service} "
            f"({incident.environment}); severity {incident.severity}; status {status}; "
            f"owner {incident.owner or 'unassigned'}; "
            f"priority {incident.priority or 'unset'}; "
            f"attempts {incident.attempt_count}",
        ),
        _block("divider"),
    ]

    _section(
        blocks,
        "Timeline",
        [
            f"{_v(t.from_status) or '(new)'} -> {_v(t.to_status)}: {t.cause} "
            f"({_ts(t.created_at) or '?'})"
            for t in incident.transitions
        ],
    )
    _section(
        blocks,
        "Trigger",
        [
            f"signal #{s['signal_id']} ({s['source']}) at {s['created_at']}: "
            f"{s['severity']} on {s['environment']}; signature {_json(s['signature'])}"
            for s in stages.get("trigger", [])
        ],
    )
    _section(
        blocks,
        "Triage",
        [
            f"affected service {t.affected_service}; blast radius {t.blast_radius}; "
            f"owner {t.owner or 'unresolved'} via {t.owner_source}: {t.rationale}"
            + (f" [duplicate of incident #{t.duplicate_of_id}]" if t.duplicate_of_id else "")
            for t in incident.triage_results
        ],
    )
    _section(
        blocks,
        "Review requests",
        [
            f"#{r.id} {r.reviewer}: {_v(r.status)} - {r.rationale}"
            + (
                f" (decided by {r.decided_by} at {_ts(r.decided_at)}"
                + (f": {r.note}" if r.note else "")
                + ")"
                if r.decided_by
                else ""
            )
            for r in incident.review_requests
        ],
    )
    _section(
        blocks,
        "Evidence",
        [
            f"{e['evidence_id']} ({e['source']}/{e['kind']}, {e['origin']}) "
            f"at {e['collected_at']}: {e['summary']}" + (" [truncated]" if e["truncated"] else "")
            for e in stages.get("evidence", [])
        ],
    )
    _section(
        blocks,
        "Hypotheses",
        [
            f"#{h['id']} (attempt {h['attempt']}, confidence {h['confidence']:.2f}): "
            f"{h['claim']} [refs: {', '.join(h['evidence_refs']) or 'none'}]"
            for h in stages.get("hypothesis", [])
        ],
    )
    _section(
        blocks,
        "Candidates",
        [
            f"#{c['id']} (attempt {c['attempt']}, rank {c['rank']}, "
            f"score {c['score']:.2f}, {c['risk_tier']})"
            f"{' [selected]' if c['selected'] else ''} {c['action']} "
            f"{_json(c['params'])}: {c['rationale']}"
            for c in stages.get("candidates", [])
        ],
    )
    _section(blocks, "Decision", _decision_lines(stages.get("decision")))
    _section(
        blocks,
        "Actions taken",
        [
            f"#{e['id']} (attempt {e['attempt']}) {e['action']}: {e['result']} "
            f"[policy={e['policy_decision']}, origin={e['origin']}] "
            f"{e['started_at']} -> {e['finished_at']}: {e['detail']}"
            + (
                f" [compensation for execution #{e['compensates_id']}]"
                if e.get("compensates_id")
                else ""
            )
            for e in stages.get("execution", [])
        ],
    )
    _section(
        blocks,
        "Validations",
        [
            f"#{v['id']} (attempt {v['attempt']}) {v['action']}: {v['outcome']} - {v['reason']}"
            for v in stages.get("validation", [])
        ],
    )
    _section(
        blocks,
        "Approvals",
        [
            f"#{a['id']} {a['action']}: {a['status']}"
            + (f" by {a['decided_by']} at {a['decided_at']}" if a["decided_by"] else "")
            + (f" - {a['note']}" if a.get("note") else "")
            for a in stages.get("approval", [])
        ],
    )
    _section(
        blocks,
        "Verifications",
        [
            f"#{v['id']} (attempt {v['attempt']}) {v['action']}: {v['outcome']} "
            f"[{_checks_text(v['checks'])}] "
            f"{v['started_at']} -> {v['finished_at']}: {v['detail']}"
            + (" [needs_compensation]" if v["needs_compensation"] else "")
            for v in stages.get("verification", [])
        ],
    )
    _section(
        blocks,
        "Work items",
        [
            f"{w.provider} {w.external_key or w.external_id}: {w.status}"
            + (f" - {w.external_url}" if w.external_url else "")
            + (f" (assignee {w.assignee})" if w.assignee else "")
            for w in incident.work_items
        ],
    )
    _section(blocks, "Outcome and follow-ups", _outcome_lines(incident, stages))
    return title, blocks
