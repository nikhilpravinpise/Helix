"""Self-approval (decided 2026-10-16): an owner or admin who wrote a change
may approve it themselves from the dashboard, as an explicit "Approve my own
change"; every such decision reads as self-approved. Members, Slack clicks
and the file-registry benchmark keep the strict rule."""

from pathlib import Path

import pytest
from sqlalchemy import func, select
from test_home import section, sign_in

from events.ingest import ingest_event
from events.schemas import EngineeringEvent, EventSource, EventType
from helix import ui
from state import models

BENCHMARK_REGISTRY = Path(__file__).resolve().parents[2] / "benchmark" / "service_registry.yaml"


@pytest.fixture(autouse=True)
def registry(tmp_path, monkeypatch):
    """The benchmark cast with roles: alice owner, bob admin, carol member
    (alice and bob own payment-api; carol is a reviewer only)."""
    path = tmp_path / "registry.yaml"
    path.write_text(
        BENCHMARK_REGISTRY.read_text() + "\npeople:\n  alice: {name: Alice Anders, role: owner}\n"
        "  bob: {name: Bob Brown, role: admin}\n  carol: {role: member}\n"
    )
    monkeypatch.setenv("HELIX_SERVICE_REGISTRY", str(path))
    monkeypatch.setenv("HELIX_ENV_FILE", "")
    # The decision is the contract here; the resumed run is not.
    monkeypatch.setattr("agent.runner.run_incident", lambda *a, **k: None)
    monkeypatch.setattr("agent.runner.run_until_parked", lambda *a, **k: None)


def _change(sessions, author: str) -> int:
    with sessions() as session:
        pr = session.scalar(select(func.count()).select_from(models.Incident)) + 1
    with sessions() as session:
        result = ingest_event(
            session,
            EngineeringEvent(
                event_type=EventType.PR_OPENED,
                source=EventSource.GITHUB,
                service="payment-api",
                environment="sandbox",
                actor=author,
                metadata={"pr": pr, "branch": f"feature/{pr}", "title": "Tune pool"},
            ),
        )
        session.commit()
        return session.scalar(
            select(models.Incident.id).where(models.Incident.incident_id == result.incident_id)
        )


def _incident_id(sessions, pk: int) -> str:
    with sessions() as session:
        return session.get(models.Incident, pk).incident_id


def _approval(sessions, author: str) -> tuple[int, str]:
    pk = _change(sessions, author)
    with sessions() as session:
        row = models.Approval(
            incident_id=pk,
            action="reapply_manifest",
            params={"path": "k8s/x.yaml"},
            fingerprint="f" * 64,
            status=models.ApprovalStatus.PENDING,
        )
        session.add(row)
        session.commit()
        return row.id, _incident_id(sessions, pk)


def _review(sessions, author: str, reviewer: str = "carol") -> tuple[int, str]:
    pk = _change(sessions, author)
    with sessions() as session:
        row = models.ReviewRequest(
            incident_id=pk,
            attempt=0,
            reviewer=reviewer,
            rationale="registry",
            status=models.ApprovalStatus.PENDING,
        )
        session.add(row)
        session.commit()
        return row.id, _incident_id(sessions, pk)


def _status_of(sessions, model, row_id):
    with sessions() as session:
        return session.get(model, row_id)


OWN_BUTTON = "Approve my own change"
OWN_LINE = "You wrote this change. Approving it yourself is recorded as self-approved."


class TestApi:
    def test_an_owner_author_who_asks_explicitly_is_recorded_as_the_decider(
        self, client, sqlite_sessions
    ):
        approval_id, _ = _approval(sqlite_sessions, "alice")
        response = client.post(
            f"/approvals/{approval_id}/approve",
            json={"actor": "alice", "self_approve": True},
        )
        assert response.status_code == 200, response.text
        assert response.json()["approval"]["decided_by"] == "alice"
        assert response.json()["approval"]["status"] == "approved"

    def test_the_same_request_without_the_flag_is_refused(self, client, sqlite_sessions):
        approval_id, _ = _approval(sqlite_sessions, "alice")
        response = client.post(f"/approvals/{approval_id}/approve", json={"actor": "alice"})
        assert response.status_code == 409
        assert "author" in response.json()["detail"]

    def test_a_member_author_is_refused_with_the_old_reason(self, client, sqlite_sessions):
        approval_id, _ = _approval(sqlite_sessions, "carol")
        response = client.post(
            f"/approvals/{approval_id}/approve", json={"actor": "carol", "self_approve": True}
        )
        assert response.status_code == 409
        assert response.json()["detail"].endswith(
            "is the author of this change and may not approve it"
        )

    def test_an_admin_author_may_approve_the_review_on_their_own_change(
        self, client, sqlite_sessions
    ):
        review_id, _ = _review(sqlite_sessions, "bob", reviewer="carol")
        response = client.post(
            f"/reviews/{review_id}/approve", json={"actor": "bob", "self_approve": True}
        )
        assert response.status_code == 200, response.text
        assert response.json()["review"]["decided_by"] == "bob"

    def test_a_member_author_cannot_approve_their_own_review(self, client, sqlite_sessions):
        review_id, _ = _review(sqlite_sessions, "carol", reviewer="carol")
        response = client.post(
            f"/reviews/{review_id}/approve", json={"actor": "carol", "self_approve": True}
        )
        assert response.status_code == 409


class TestPages:
    def test_the_button_is_offered_to_an_owner_or_admin_author_only(
        self, client, sqlite_sessions, monkeypatch
    ):
        _approval(sqlite_sessions, "alice")
        _review(sqlite_sessions, "bob", reviewer="carol")
        for login in ("alice", "bob"):
            client.cookies.clear()
            sign_in(client, monkeypatch, login)
            body = client.get("/ui/approvals").text
            assert OWN_LINE in body
            assert 'data-self-approve="1"' in body
        # alice wrote the approval's change: she gets the button there and
        # the ordinary controls on bob's review; bob the reverse.
        client.cookies.clear()
        sign_in(client, monkeypatch, "alice")
        body = client.get("/ui/approvals").text
        assert body.count(OWN_BUTTON) == 1
        assert 'data-decide="reject"' in body

    def test_a_member_author_sees_who_it_waits_on_and_no_button(
        self, client, sqlite_sessions, monkeypatch
    ):
        _approval(sqlite_sessions, "carol")
        sign_in(client, monkeypatch, "carol")
        body = client.get("/ui/approvals").text
        assert OWN_BUTTON not in body
        assert 'data-decide="approve"' not in body
        assert "You wrote this change, so you cannot decide it." in body
        assert "Alice Anders" in body

    def test_other_signed_in_users_and_signed_out_visitors_see_the_normal_controls(
        self, client, sqlite_sessions, monkeypatch
    ):
        _approval(sqlite_sessions, "carol")
        sign_in(client, monkeypatch, "bob")
        body = client.get("/ui/approvals").text
        assert OWN_BUTTON not in body
        assert 'data-decide="approve"' in body

    def test_a_signed_out_visitor_sees_the_normal_controls(self, client, sqlite_sessions):
        _approval(sqlite_sessions, "alice")
        body = client.get("/ui/approvals").text
        assert OWN_BUTTON not in body
        assert 'data-decide="approve"' in body

    def test_the_incident_page_offers_the_button_on_a_pending_review(
        self, client, sqlite_sessions, monkeypatch
    ):
        _, incident_id = _review(sqlite_sessions, "alice", reviewer="carol")
        sign_in(client, monkeypatch, "alice")
        body = client.get(f"/ui/incidents/{incident_id}").text
        assert OWN_BUTTON in body and OWN_LINE in body
        assert 'data-decide-kind="review"' in body
        client.cookies.clear()
        sign_in(client, monkeypatch, "bob")
        body = client.get(f"/ui/incidents/{incident_id}").text
        assert OWN_BUTTON not in body and 'data-decide="approve"' in body

    def test_a_self_approved_decision_is_marked_everywhere_it_is_shown(
        self, client, sqlite_sessions
    ):
        approval_id, approval_incident = _approval(sqlite_sessions, "alice")
        review_id, review_incident = _review(sqlite_sessions, "bob", reviewer="carol")
        assert (
            client.post(
                f"/approvals/{approval_id}/approve", json={"actor": "alice", "self_approve": True}
            ).status_code
            == 200
        )
        assert (
            client.post(
                f"/reviews/{review_id}/approve", json={"actor": "bob", "self_approve": True}
            ).status_code
            == 200
        )
        queue = client.get("/ui/approvals").text
        assert queue.count("self-approved") >= 2
        for incident_id in (approval_incident, review_incident):
            assert "self-approved" in client.get(f"/ui/incidents/{incident_id}").text

    def test_a_normal_decision_is_not_marked(self, client, sqlite_sessions):
        approval_id, incident_id = _approval(sqlite_sessions, "carol")
        assert (
            client.post(f"/approvals/{approval_id}/approve", json={"actor": "bob"}).status_code
            == 200
        )
        assert "self-approved" not in client.get("/ui/approvals").text
        assert "self-approved" not in client.get(f"/ui/incidents/{incident_id}").text

    def test_the_summary_says_self_approved_for_an_author_decision(self):
        record = {
            "incident": {"status": "recovered", "kind": "failure", "service": "payment-api"},
            "stages": {
                "execution": [
                    {
                        "id": 1,
                        "action": "restart",
                        "result": "success",
                        "detail": "",
                        "output": {},
                        "decided_by": "alice",
                    }
                ]
            },
        }
        did = ui._did(record["incident"], record["stages"], None, "alice")
        assert "self-approved by Alice Anders" in did["text"]
        assert (
            "approved by Alice Anders"
            in ui._did(record["incident"], record["stages"], None, "dana")["text"]
        )
        assert "self-approved" not in ui._did(record["incident"], record["stages"])["text"]


class TestHome:
    def test_an_owner_author_sees_their_own_change_as_needing_them(
        self, client, sqlite_sessions, monkeypatch
    ):
        _, incident_id = _approval(sqlite_sessions, "alice")
        sign_in(client, monkeypatch, "alice")
        body = client.get("/ui/").text
        needs = section(body, "needs-you")
        assert incident_id in needs and "your own change" in needs

    def test_a_member_author_only_waits(self, client, sqlite_sessions, monkeypatch):
        _, incident_id = _approval(sqlite_sessions, "carol")
        sign_in(client, monkeypatch, "carol")
        body = client.get("/ui/").text
        assert incident_id not in section(body, "needs-you")
        assert incident_id in section(body, "waiting-on")
