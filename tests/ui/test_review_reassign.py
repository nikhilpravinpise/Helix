"""Reassigning a pending review from the dashboard and the API (decided
2026-10-16): owners and admins only; sign-in off is trusted. The decision
rules do not change, so the new reviewer decides it afterwards."""

from pathlib import Path

import pytest
from sqlalchemy import select
from test_home import sign_in
from test_self_approval import _change, _incident_id

from state import models

BENCHMARK_REGISTRY = Path(__file__).resolve().parents[2] / "benchmark" / "service_registry.yaml"


@pytest.fixture(autouse=True)
def registry(tmp_path, monkeypatch, sqlite_sessions):
    """alice owner, bob admin, carol and dave members (people table for the
    sign-in role, the file registry for the team list)."""
    path = tmp_path / "registry.yaml"
    path.write_text(
        BENCHMARK_REGISTRY.read_text() + "\npeople:\n  alice: {name: Alice Anders, role: owner}\n"
        "  bob: {name: Bob Brown, role: admin}\n  carol: {role: member}\n  dave: {role: member}\n"
    )
    monkeypatch.setenv("HELIX_SERVICE_REGISTRY", str(path))
    monkeypatch.setenv("HELIX_ENV_FILE", "")
    monkeypatch.setattr("agent.runner.run_incident", lambda *a, **k: None)
    monkeypatch.setattr("agent.runner.run_until_parked", lambda *a, **k: None)
    with sqlite_sessions() as session:
        for login, role in (("alice", "owner"), ("bob", "admin"), ("carol", "member")):
            session.add(models.Person(login=login, name=login.title(), role=role))
        session.commit()


def _review(sqlite_sessions, author="alice", reviewer="carol") -> tuple[int, str]:
    pk = _change(sqlite_sessions, author)
    with sqlite_sessions() as session:
        row = models.ReviewRequest(
            incident_id=pk,
            attempt=0,
            reviewer=reviewer,
            rationale="registry",
            status=models.ApprovalStatus.PENDING,
        )
        session.add(row)
        session.commit()
        return row.id, _incident_id(sqlite_sessions, pk)


def _reviewer_of(sqlite_sessions, review_id) -> str:
    with sqlite_sessions() as session:
        return session.get(models.ReviewRequest, review_id).reviewer


class TestApi:
    @pytest.mark.parametrize(("login", "target"), [("alice", "bob"), ("bob", "alice")])
    def test_owner_and_admin_can_reassign(
        self, client, sqlite_sessions, monkeypatch, login, target
    ):
        review_id, _ = _review(sqlite_sessions, author="dave", reviewer="carol")
        sign_in(client, monkeypatch, login)
        response = client.post(f"/reviews/{review_id}/reassign", json={"reviewer": target})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["id"] == review_id
        assert body["reviewer"] == target
        assert f"Reassigned by {login} (was carol)." in body["rationale"]
        assert _reviewer_of(sqlite_sessions, review_id) == target

    def test_a_member_is_refused(self, client, sqlite_sessions, monkeypatch):
        review_id, _ = _review(sqlite_sessions)
        sign_in(client, monkeypatch, "carol")
        response = client.post(f"/reviews/{review_id}/reassign", json={"reviewer": "dave"})
        assert response.status_code == 403
        assert _reviewer_of(sqlite_sessions, review_id) == "carol"

    def test_sign_in_off_is_trusted(self, client, sqlite_sessions):
        review_id, _ = _review(sqlite_sessions)
        response = client.post(f"/reviews/{review_id}/reassign", json={"reviewer": "dave"})
        assert response.status_code == 200, response.text
        assert response.json()["reviewer"] == "dave"

    @pytest.mark.parametrize(
        ("reviewer", "message"),
        [
            ("mallory", "not on the team"),
            ("alice", "wrote this change"),
            ("carol", "already the reviewer"),
        ],
    )
    def test_a_broken_rule_is_a_409_with_the_plain_message(
        self, client, sqlite_sessions, reviewer, message
    ):
        review_id, _ = _review(sqlite_sessions, author="alice", reviewer="carol")
        response = client.post(f"/reviews/{review_id}/reassign", json={"reviewer": reviewer})
        assert response.status_code == 409
        assert message in response.json()["detail"]
        assert _reviewer_of(sqlite_sessions, review_id) == "carol"

    def test_a_decided_review_is_a_409(self, client, sqlite_sessions):
        review_id, _ = _review(sqlite_sessions)
        client.post(f"/reviews/{review_id}/reject", json={"actor": "carol"})
        response = client.post(f"/reviews/{review_id}/reassign", json={"reviewer": "dave"})
        assert response.status_code == 409
        assert "already" in response.json()["detail"]

    def test_an_unknown_review_is_a_404(self, client):
        assert client.post("/reviews/999/reassign", json={"reviewer": "dave"}).status_code == 404

    def test_the_intake_token_gates_it(self, client, sqlite_sessions, monkeypatch):
        review_id, _ = _review(sqlite_sessions)
        monkeypatch.setenv("HELIX_INTAKE_TOKEN", "tok")
        url = f"/reviews/{review_id}/reassign"
        assert client.post(url, json={"reviewer": "dave"}).status_code == 401
        ok = client.post(url, json={"reviewer": "dave"}, headers={"Authorization": "Bearer tok"})
        assert ok.status_code == 200

    def test_the_old_reviewer_is_refused_and_the_new_one_decides(self, client, sqlite_sessions):
        review_id, _ = _review(sqlite_sessions)
        assert (
            client.post(f"/reviews/{review_id}/reassign", json={"reviewer": "dave"}).status_code
            == 200
        )
        old = client.post(f"/reviews/{review_id}/approve", json={"actor": "carol"})
        assert old.status_code == 409
        new = client.post(f"/reviews/{review_id}/approve", json={"actor": "dave"})
        assert new.status_code == 200, new.text
        assert new.json()["review"]["decided_by"] == "dave"

    def test_the_new_reviewer_is_queued_for_slack(self, client, sqlite_sessions):
        review_id, _ = _review(sqlite_sessions)
        client.post(f"/reviews/{review_id}/reassign", json={"reviewer": "dave"})
        with sqlite_sessions() as session:
            rows = session.scalars(
                select(models.IntegrationOutbox).where(
                    models.IntegrationOutbox.op == "review_request"
                )
            ).all()
        assert [r.payload["review_id"] for r in rows] == [review_id]


CONTROL = 'data-reassign="'


def _options(body: str, review_id: int) -> list[str]:
    start = body.index(f'id="reassign-to-{review_id}"')
    block = body[start : body.index("</select>", start)]
    return [part.split('"', 1)[0] for part in block.split('<option value="')[1:]]


class TestPages:
    @pytest.mark.parametrize("login", ["alice", "bob"])
    def test_owner_and_admin_see_the_control_on_both_pages(
        self, client, sqlite_sessions, monkeypatch, login
    ):
        review_id, incident_id = _review(sqlite_sessions, author="dave", reviewer="carol")
        sign_in(client, monkeypatch, login)
        for url in ("/ui/approvals", f"/ui/incidents/{incident_id}"):
            body = client.get(url).text
            assert f'{CONTROL}{review_id}"' in body
            # the author (dave) and the current reviewer (carol) are not offered
            assert _options(body, review_id) == ["alice", "bob"]

    def test_a_member_does_not_see_it(self, client, sqlite_sessions, monkeypatch):
        _, incident_id = _review(sqlite_sessions)
        sign_in(client, monkeypatch, "carol")
        for url in ("/ui/approvals", f"/ui/incidents/{incident_id}"):
            assert CONTROL not in client.get(url).text

    def test_sign_in_off_is_trusted_like_the_api(self, client, sqlite_sessions):
        review_id, _ = _review(sqlite_sessions)
        assert f'{CONTROL}{review_id}"' in client.get("/ui/approvals").text

    def test_options_exclude_the_author_and_the_current_reviewer(
        self, client, sqlite_sessions, monkeypatch
    ):
        review_id, _ = _review(sqlite_sessions, author="alice", reviewer="carol")
        sign_in(client, monkeypatch, "alice")
        assert _options(client.get("/ui/approvals").text, review_id) == ["bob", "dave"]

    def test_a_decided_review_has_no_control(self, client, sqlite_sessions, monkeypatch):
        review_id, incident_id = _review(sqlite_sessions)
        client.post(f"/reviews/{review_id}/reject", json={"actor": "carol"})
        sign_in(client, monkeypatch, "alice")
        for url in ("/ui/approvals", f"/ui/incidents/{incident_id}"):
            assert CONTROL not in client.get(url).text

    def test_the_page_shows_the_new_reviewer_after_reassigning(
        self, client, sqlite_sessions, monkeypatch
    ):
        review_id, incident_id = _review(sqlite_sessions, author="alice", reviewer="carol")
        sign_in(client, monkeypatch, "bob")
        client.post(f"/reviews/{review_id}/reassign", json={"reviewer": "dave"})
        body = client.get(f"/ui/incidents/{incident_id}").text
        assert "dave" in body and "Reassigned by bob (was carol)." in body


class TestOwnerLabel:
    def test_a_review_card_labels_the_service_owner_as_owner(self, client, sqlite_sessions):
        """Requirement changed per issue #61: "assigned to X" on a review card
        read as if the review went to X, so the label is now "owner X"."""
        review_id, incident_id = _review(sqlite_sessions)
        with sqlite_sessions() as session:
            incident = session.scalar(
                select(models.Incident).where(models.Incident.incident_id == incident_id)
            )
            incident.owner = "alice"
            session.commit()
        page = client.get("/ui/approvals").text
        assert f'id="review-{review_id}"' in page
        assert "assigned to" not in page
        assert 'class="muted">owner <span class="person" title="alice">Alice Anders</span>' in page
