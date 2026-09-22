"""Manual E2E driver for the Phase 2 change lifecycle (2.9 verification).

Drives a real `helix` subprocess chain against a throwaway SQLite DB:
pr.opened -> triage -> owner -> review_pending -> `helix review approve`
-> approved -> deploying -> build.passed -> recovered, asserting the
durable record (triage/review rows, work item, knowledge page, drained
outbox) at every hop. Not part of the pytest suite - a developer tool for
the same verification, runnable standalone.
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


def run_helix(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["uv", "run", "helix", *args],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="helix-e2e-"))
    db = tmp / "state.db"
    env = dict(os.environ)
    env.update(
        {
            "HELIX_DATABASE_URL": f"sqlite:///{db}",
            "HELIX_ARM": "A",
            "HELIX_EVIDENCE_SOURCE": "fixtures",
            # A fixture path is required to even construct fixture-mode
            # capabilities; the change lifecycle never reads it.
            "HELIX_FIXTURE_PATH": "cluster/crashloop",
            "HELIX_WORK_PROVIDER": "memory",
            "HELIX_VERIFY_WINDOW": "2",
            "HELIX_VERIFY_POLL": "1",
            "HELIX_VERIFY_FUNCTIONAL": "false",
        }
    )

    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session

    from events.ingest import ingest_event
    from events.schemas import EngineeringEvent, EventSource, EventType
    from state import models
    from state.db import session_scope
    from state.machine import IncidentStatus

    engine = create_engine(f"sqlite:///{db}")
    models.Base.metadata.create_all(engine)

    def db_state() -> dict:
        with Session(engine) as s:
            inc = s.scalars(select(models.Incident)).all()[-1]
            return {
                "status": IncidentStatus(inc.status).value,
                "owner": inc.owner,
                "kind": inc.kind.value,
                "triage": [
                    (t.affected_service, t.owner, t.owner_source, t.duplicate_of_id)
                    for t in inc.triage_results
                ],
                "reviews": [
                    (r.reviewer, r.status.value, r.decided_by) for r in inc.review_requests
                ],
                "work_items": [(w.provider, w.external_id, w.status) for w in inc.work_items],
                "page": inc.knowledge_page.external_id if inc.knowledge_page else None,
                "outbox": {
                    r.status.value: r.status.value
                    for r in s.scalars(select(models.IntegrationOutbox)).all()
                },
                "outbox_rows": [
                    (r.integration, r.op, r.status.value)
                    for r in s.scalars(select(models.IntegrationOutbox)).all()
                ],
            }

    # 1. pr.opened -> change incident
    with session_scope(engine) as session:
        result = ingest_event(
            session,
            EngineeringEvent(
                event_type=EventType.PR_OPENED,
                source=EventSource.GITHUB,
                service="payment-api",
                environment="sandbox",
                actor="nikhil",
                metadata={"pr": 42, "branch": "feature/pay-retry", "files": ["payments/retry.py"]},
            ),
        )
    incident_id = result.incident_id
    print(f"incident: {incident_id}")
    st = db_state()
    assert st["kind"] == "change", st
    assert st["status"] == "received", st

    # 2. hop 1: received -> triaged (triage node runs, coordinator routes)
    run_helix(["run", incident_id], env)
    st = db_state()
    assert st["status"] == "triaged", st
    assert st["triage"], st
    print(f"triaged: owner={st['triage'][-1][1]} via {st['triage'][-1][2]}")

    # 3. hop 2: triaged -> owner_assigned -> review_pending
    run_helix(["run", incident_id], env)
    st = db_state()
    assert st["status"] == "review_pending", st
    assert st["owner"] == "alice", st  # service_registry.yaml payment-api owner
    assert st["reviews"], st
    reviewer = st["reviews"][-1][0]
    assert reviewer != "nikhil", "author must never self-review"
    print(f"review_pending: owner={st['owner']} reviewer={reviewer}")

    # 4. review decision via the real CLI
    proc = run_helix(["review", incident_id, "approve", "--actor", reviewer], env)
    print("review:", proc.stdout.strip() or proc.stderr.strip())
    st = db_state()
    assert st["reviews"][-1][1] == "approved", st

    # 5. hops to approved -> deploying
    for _ in range(4):
        st = db_state()
        if st["status"] == "deploying":
            break
        run_helix(["run", incident_id], env)
    st = db_state()
    assert st["status"] == "deploying", st
    print("deploying")

    # 6. build.passed for the PR -> recovered
    with session_scope(engine) as session:
        ingest_event(
            session,
            EngineeringEvent(
                event_type=EventType.BUILD_PASSED,
                source=EventSource.GITHUB,
                service="payment-api",
                environment="sandbox",
                actor="nikhil",
                metadata={"pr": 42, "branch": "feature/pay-retry", "sha": "abc123"},
            ),
        )
    run_helix(["run", incident_id], env)
    st = db_state()
    assert st["status"] == "recovered", st
    print("recovered")

    # 7. drain + sync
    proc = run_helix(["sync"], env)
    print("sync:", proc.stdout.strip())
    st = db_state()
    print("outbox:", st["outbox_rows"])
    print("work_items:", st["work_items"])
    print("knowledge_page:", st["page"])
    assert st["work_items"], "a work item should exist"
    assert all(r[2] in ("sent", "skipped", "failed") for r in st["outbox_rows"]), (
        "no row may stay pending after sync"
    )
    print("E2E change lifecycle: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
