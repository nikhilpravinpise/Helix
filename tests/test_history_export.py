"""OPT-1: the incident history the MLOps pipeline trains on.

One row per remediation action Helix executed: the fault it was aimed at,
the action, the arm, the environment and risk tier, and whether that
action fixed the incident. An action counts as the fix only when the
incident recovered and no later action ran, so an earlier attempt that
failed and was replanned is labelled a miss, not credited with the
recovery that followed it.
"""

import csv
from datetime import UTC, datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from helix.history_export import FIELDS, export_rows, write_csv
from state import models
from state.machine import IncidentStatus


def _session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    models.Base.metadata.create_all(engine)
    return Session(engine)


def _incident(session, incident_id, status, signature):
    incident = models.Incident(
        incident_id=incident_id,
        fingerprint=incident_id,
        service="payment-api",
        environment="sandbox",
        severity="high",
        status=status,
    )
    session.add(incident)
    session.flush()
    session.add(
        models.Signal(
            incident_id=incident.id,
            source="kubernetes",
            environment="sandbox",
            severity="high",
            signature=signature,
            raw={},
        )
    )
    return incident


def _execution(session, incident, action, order, arm="A", compensates=None):
    candidate = models.Candidate(
        incident_id=incident.id,
        attempt=order,
        rank=1,
        action=action,
        params={},
        rationale="r",
        evidence_refs=[],
        score=1.0,
        risk_tier="medium",
        selected=True,
        components={"arm": arm},
    )
    session.add(candidate)
    session.flush()
    execution = models.Execution(
        incident_id=incident.id,
        candidate_id=candidate.id,
        compensates_id=compensates,
        attempt=order,
        action=action,
        params={},
        fingerprint=f"{incident.incident_id}-{order}",
        policy_decision="allow_with_checks",
        origin="fixture",
        result=models.ExecutionResult.SUCCESS,
        detail="d",
        started_at=datetime(2026, 10, 1, 12, order, tzinfo=UTC),
    )
    session.add(execution)
    session.flush()
    return execution


def test_only_the_action_that_fixed_the_incident_is_labelled_a_fix():
    session = _session()
    crash = {"waiting_reason": "CrashLoopBackOff", "container": "c"}
    recovered = _incident(session, "INC-1", IncidentStatus.RECOVERED, crash)
    _execution(session, recovered, "restart_deployment", 0)
    _execution(session, recovered, "reapply_manifest", 1)
    escalated = _incident(session, "INC-2", IncidentStatus.ESCALATED, crash)
    _execution(session, escalated, "rollback_deployment", 0, arm="E")

    rows = {(r["incident_id"], r["action"]): r for r in export_rows(session)}
    assert rows[("INC-1", "restart_deployment")]["fixed"] == 0
    assert rows[("INC-1", "reapply_manifest")]["fixed"] == 1
    assert rows[("INC-2", "rollback_deployment")]["fixed"] == 0
    row = rows[("INC-1", "reapply_manifest")]
    assert row["fault"] == "kubernetes:CrashLoopBackOff"
    assert row["arm"] == "A" and row["risk_tier"] == "medium"
    assert rows[("INC-2", "rollback_deployment")]["arm"] == "E"


def test_compensating_actions_are_not_rows():
    session = _session()
    incident = _incident(session, "INC-3", IncidentStatus.ESCALATED, {"stage": "Test"})
    first = _execution(session, incident, "open_fix_pr", 0)
    _execution(session, incident, "close_pr", 1, compensates=first.id)
    rows = export_rows(session)
    assert [r["action"] for r in rows] == ["open_fix_pr"]
    assert rows[0]["fault"] == "kubernetes:Test"


def test_the_csv_has_a_fixed_header(tmp_path):
    session = _session()
    incident = _incident(session, "INC-4", IncidentStatus.RECOVERED, {"stage": "Build image"})
    _execution(session, incident, "rerun_pipeline", 0)
    out = tmp_path / "history.csv"
    assert write_csv(export_rows(session), out) == 1
    with out.open() as handle:
        reader = csv.DictReader(handle)
        assert reader.fieldnames == list(FIELDS)
        assert next(reader)["fixed"] == "1"


def test_helix_export_history_writes_the_csv(tmp_path, monkeypatch, capsys):
    from sqlalchemy import create_engine as make_engine

    from helix import cli

    db = tmp_path / "state.db"
    engine = make_engine(f"sqlite:///{db}")
    models.Base.metadata.create_all(engine)
    with Session(engine) as session:
        incident = _incident(session, "INC-5", IncidentStatus.RECOVERED, {"stage": "Test"})
        _execution(session, incident, "open_fix_pr", 0)
        session.commit()
    monkeypatch.setenv("HELIX_DATABASE_URL", f"sqlite:///{db}")
    out = tmp_path / "history.csv"
    assert cli.main(["export-history", "--out", str(out)]) == 0
    assert "1 row(s)" in capsys.readouterr().out
    assert out.read_text().splitlines()[1].startswith("INC-5,")
