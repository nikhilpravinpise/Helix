"""OPT-1: the model's estimate is shown beside a decision, never used to
make one.

`helix provenance` gains one advisory line under Decision: how often the
selected action fixed similar past incidents, with the model's honest
verdict. Nothing in agent/ (diagnosis, planning, policy, execution) may
read it, so an advisory signal cannot become an unaudited decision path
(plan.md OPT-1).
"""

import json
from pathlib import Path

from sqlalchemy.orm import Session
from test_history_export import _execution, _incident, _session

from state.machine import IncidentStatus
from state.provenance import assemble_provenance, render_markdown

ROOT = Path(__file__).resolve().parents[1]


def _advisory(path: Path, table: dict, verdict: str = "beats baseline") -> Path:
    path.write_text(
        json.dumps(
            {
                "version": "v1",
                "verdict": verdict,
                "accuracy": 0.95,
                "baseline_accuracy": 0.6,
                "table": table,
            }
        )
    )
    return path


def _crash_incident(session: Session) -> None:
    incident = _incident(
        session, "INC-9", IncidentStatus.RECOVERED, {"waiting_reason": "CrashLoopBackOff"}
    )
    _execution(session, incident, "reapply_manifest", 0)
    session.commit()


def test_provenance_shows_the_advisory_estimate(tmp_path, monkeypatch):
    path = _advisory(
        tmp_path / "advisory.json",
        {"kubernetes:CrashLoopBackOff|reapply_manifest": {"p_fixed": 0.83, "n": 12}},
    )
    monkeypatch.setenv("HELIX_ADVISORY_PATH", str(path))
    session = _session()
    _crash_incident(session)
    text = render_markdown(assemble_provenance(session, "INC-9"))
    assert "advisory" in text and "83%" in text and "12 similar" in text
    assert "not used by any decision" in text


def test_no_model_means_no_advisory_line(tmp_path, monkeypatch):
    monkeypatch.setenv("HELIX_ADVISORY_PATH", str(tmp_path / "missing.json"))
    session = _session()
    _crash_incident(session)
    record = assemble_provenance(session, "INC-9")
    assert record["stages"]["advisory"] is None
    assert "advisory" not in render_markdown(record)


def test_a_model_no_better_than_guessing_says_so(tmp_path, monkeypatch):
    path = _advisory(
        tmp_path / "advisory.json",
        {"kubernetes:CrashLoopBackOff|reapply_manifest": {"p_fixed": 0.5, "n": 4}},
        verdict="no better than baseline",
    )
    monkeypatch.setenv("HELIX_ADVISORY_PATH", str(path))
    session = _session()
    _crash_incident(session)
    assert "no better than baseline" in render_markdown(assemble_provenance(session, "INC-9"))


def test_no_decision_code_reads_the_advisory():
    for source in (ROOT / "agent").rglob("*.py"):
        text = source.read_text()
        assert "helix.advisory" not in text and "advisory_path" not in text, source
