"""Incident history as training data for the MLOps pipeline (OPT-1).

One row per remediation action Helix executed: the fault it was aimed at,
the action, the arm that chose it, the environment and risk tier, and
whether that action fixed the incident. The model trained on this lives in
`mlops/` (its own Python 3.11 project: PyCaret refuses 3.12), so Helix
never imports it; this module only reads Postgres and writes a CSV.

`fixed` is 1 only for the incident's last remediation action, and only
when the incident recovered. An earlier attempt that failed and was
replanned is a miss, not credited with the recovery that followed it.
Compensating actions (a rollback of Helix's own write) are not rows.
"""

import csv
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from state import models
from state.machine import IncidentStatus

FIELDS = (
    "incident_id",
    "fault",
    "action",
    "arm",
    "risk_tier",
    "environment",
    "kind",
    "attempt",
    "fixed",
)

#: Signature keys that name what went wrong, most specific first.
_FAULT_KEYS = (
    "waiting_reason",
    "terminated_reason",
    "stage",
    "conclusion",
    "comparison",
    "review_state",
    "reason",
    "symptom",
    "type",
)


def _fault(incident: models.Incident) -> str:
    """`<source>:<what went wrong>`, from the first signal or, for an
    incident the event path opened, its first outside event."""
    if incident.signals:
        signal = incident.signals[0]
        signature = signal.signature or {}
        what = next((signature[k] for k in _FAULT_KEYS if signature.get(k)), "unknown")
        return f"{signal.source}:{what}"
    for record in incident.event_records:
        if record.source == "helix":
            continue
        metadata = (record.envelope or {}).get("metadata") or {}
        what = next((metadata[k] for k in _FAULT_KEYS if metadata.get(k)), record.event_type)
        return f"{record.source}:{what}"
    return "unknown:unknown"


def export_rows(session: Session) -> list[dict[str, Any]]:
    """Every executed remediation action, oldest first, as a training row."""
    rows: list[dict[str, Any]] = []
    incidents = session.scalars(select(models.Incident).order_by(models.Incident.id)).all()
    for incident in incidents:
        actions = sorted(
            (e for e in incident.executions if e.compensates_id is None),
            key=lambda e: (e.started_at, e.id),
        )
        if not actions:
            continue
        recovered = incident.status == IncidentStatus.RECOVERED
        fault = _fault(incident)
        for index, execution in enumerate(actions):
            components = (execution.candidate.components if execution.candidate else None) or {}
            rows.append(
                {
                    "incident_id": incident.incident_id,
                    "fault": fault,
                    "action": execution.action,
                    "arm": components.get("arm", "unknown"),
                    "risk_tier": execution.candidate.risk_tier if execution.candidate else "",
                    "environment": incident.environment,
                    "kind": getattr(incident.kind, "value", incident.kind),
                    "attempt": execution.attempt,
                    "fixed": int(recovered and index == len(actions) - 1),
                }
            )
    return rows


def write_csv(rows: list[dict[str, Any]], out: Path) -> int:
    """Write `rows` under the fixed FIELDS header. Returns the row count."""
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)
