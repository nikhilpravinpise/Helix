"""Links out of the dashboard to the team's Slack channel, Jira project and
GitHub repository (Home's Tools) and to an incident's Slack thread, Jira
ticket and pull request. No request leaves the test: Slack is faked behind
integrations.slack.oauth._http and nothing is called at render time."""

import re
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session
from test_home import NOW, section, seed

from integrations.slack import oauth
from state import connections, models
from state.machine import IncidentStatus

SLACK_URL = "https://app.slack.com/client/T1/C2"
JIRA_URL = "https://acme.atlassian.net/browse/OPS"
GITHUB_URL = "https://github.com/acme/widgets"


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("HELIX_ENV_FILE", "")
    monkeypatch.setenv("HELIX_CONNECTIONS_ENABLED", "true")
    monkeypatch.setenv("HELIX_CONNECTIONS_KEY", "test connections key")
    monkeypatch.setenv("HELIX_GITHUB_REPO", "acme/widgets")
    monkeypatch.delenv("HELIX_EVIDENCE_SOURCE", raising=False)


def _save(engine, provider, **settings):
    with Session(engine) as session:
        connections.save_connection(
            session,
            provider,
            account="Acme",
            scopes=[],
            access_token="tok" if provider != "github" else None,
            refresh_token=None,
            expires_at=None,
            settings=settings,
        )
        session.commit()


def _tool(page: str, provider: str) -> str:
    return re.search(rf'data-tool="{provider}".*?</li>', section(page, "tools"), re.S).group(0)


def _links(html: str) -> list[str]:
    return re.findall(r'<a [^>]*target="_blank"[^>]*>', html)


class TestHomeTools:
    def test_configured_tools_open_the_exact_external_urls(self, client, sqlite_engine):
        _save(sqlite_engine, "slack", team_id="T1", channel="C2", channel_name="#ops")
        _save(
            sqlite_engine,
            "jira",
            cloud_id="c",
            site_url="https://acme.atlassian.net",
            site_name="acme",
            project_key="OPS",
        )
        _save(sqlite_engine, "github", installation_id="9")
        page = client.get("/ui/").text
        for provider, url in (("slack", SLACK_URL), ("jira", JIRA_URL), ("github", GITHUB_URL)):
            [link] = _links(_tool(page, provider))
            assert f'href="{url}"' in link
            assert 'rel="noopener noreferrer"' in link
        assert "#ops" in _tool(page, "slack")
        assert "OPS" in _tool(page, "jira") and "acme" in _tool(page, "jira")
        assert "acme/widgets" in _tool(page, "github")

    def test_an_old_slack_row_without_a_name_says_your_channel(self, client, sqlite_engine):
        _save(sqlite_engine, "slack", team_id="T1", channel="C2")
        slack = _tool(client.get("/ui/").text, "slack")
        assert f'href="{SLACK_URL}"' in slack and "your channel" in slack

    def test_connected_but_unconfigured_asks_for_a_pick(self, client, sqlite_engine, monkeypatch):
        monkeypatch.setenv("HELIX_GITHUB_REPO", "")
        _save(sqlite_engine, "slack", team_id="T1")
        _save(sqlite_engine, "jira", cloud_id="c", site_url="https://acme.atlassian.net")
        _save(sqlite_engine, "github", installation_id="9")
        page = client.get("/ui/").text
        assert "Pick a channel" in _tool(page, "slack")
        assert "Pick a project" in _tool(page, "jira")
        assert "Pick a repository" in _tool(page, "github")
        for provider in ("slack", "jira", "github"):
            assert not _links(_tool(page, provider))
            assert 'href="/ui/connections"' in _tool(page, provider)

    def test_github_without_a_repository_asks_for_a_pick(self, client, sqlite_engine, monkeypatch):
        monkeypatch.setenv("HELIX_GITHUB_REPO", "")
        _save(sqlite_engine, "github", installation_id="9")
        github = _tool(client.get("/ui/").text, "github")
        assert "Pick a repository" in github and not _links(github)

    def test_not_connected_shows_no_open_or_pick_link(self, client):
        tools = section(client.get("/ui/").text, "tools")
        assert "Not connected" in tools
        assert not _links(tools) and "Pick a" not in tools

    def test_a_malformed_id_never_becomes_a_link(self, client, sqlite_engine):
        _save(sqlite_engine, "slack", team_id="T1/../x", channel="C2")
        assert not _links(_tool(client.get("/ui/").text, "slack"))


def _slack_row(engine, incident_id: str, **payload):
    with Session(engine) as session:
        pk = session.query(models.Incident).filter_by(incident_id=incident_id).one().id
        session.add(
            models.IntegrationOutbox(
                incident_id=pk,
                integration="slack",
                op="notify",
                payload={"text": "x", **payload},
                status=models.OutboxStatus.SENT,
                sent_at=NOW,
            )
        )
        session.commit()


def _work_item(engine, incident_id: str, **fields):
    with Session(engine) as session:
        pk = session.query(models.Incident).filter_by(incident_id=incident_id).one().id
        session.add(
            models.WorkItem(
                incident_id=pk,
                provider="jira",
                external_id="10001",
                title="Fix it",
                status="open",
                **fields,
            )
        )
        session.commit()


def _incident(engine) -> str:
    return seed(engine, 1, IncidentStatus.TRIAGED, NOW - timedelta(hours=1))


def _row(page: str) -> str:
    match = re.search(r'<div class="ext-links".*?</div>', page, re.S)
    return match.group(0) if match else ""


class TestIncidentLinks:
    def test_slack_thread_link_from_the_outbox_ts_and_channel(self, client, sqlite_engine):
        _save(sqlite_engine, "slack", team_id="T1", channel="C9")
        inc = _incident(sqlite_engine)
        _slack_row(sqlite_engine, inc, ts="1700000000.123456", channel="C2")
        row = _row(client.get(f"/ui/incidents/{inc}").text)
        url = "https://app.slack.com/client/T1/C2/thread/C2-1700000000.123456"
        assert f'href="{url}"' in row and "Slack thread" in row
        assert 'rel="noopener noreferrer"' in row and 'target="_blank"' in row

    def test_an_old_row_without_channel_falls_back_to_the_configured_one(
        self, client, sqlite_engine
    ):
        _save(sqlite_engine, "slack", team_id="T1", channel="C9")
        inc = _incident(sqlite_engine)
        _slack_row(sqlite_engine, inc, ts="1700000000.123456")
        row = _row(client.get(f"/ui/incidents/{inc}").text)
        assert 'href="https://app.slack.com/client/T1/C9/thread/C9-1700000000.123456"' in row

    def test_no_slack_link_without_channel_or_connection_or_ts(self, client, sqlite_engine):
        inc = _incident(sqlite_engine)
        _slack_row(sqlite_engine, inc, ts="1700000000.123456", channel="C2")
        assert "Slack thread" not in client.get(f"/ui/incidents/{inc}").text  # not connected
        _save(sqlite_engine, "slack", team_id="T1")
        inc2 = seed(sqlite_engine, 2, IncidentStatus.TRIAGED, NOW)
        _slack_row(sqlite_engine, inc2, ts="1700000000.123456")  # no channel anywhere
        assert "Slack thread" not in client.get(f"/ui/incidents/{inc2}").text
        inc3 = seed(sqlite_engine, 3, IncidentStatus.TRIAGED, NOW)
        _slack_row(sqlite_engine, inc3, channel="C2")  # never posted
        assert "Slack thread" not in client.get(f"/ui/incidents/{inc3}").text

    def test_jira_link_from_the_work_item(self, client, sqlite_engine):
        inc = _incident(sqlite_engine)
        _work_item(
            sqlite_engine,
            inc,
            external_key="OPS-7",
            external_url="https://acme.atlassian.net/browse/OPS-7",
        )
        row = _row(client.get(f"/ui/incidents/{inc}").text)
        assert 'href="https://acme.atlassian.net/browse/OPS-7"' in row
        assert "Jira OPS-7" in row and 'rel="noopener noreferrer"' in row

    def test_a_work_item_without_a_web_url_shows_no_jira_link(self, client, sqlite_engine):
        inc = _incident(sqlite_engine)
        _work_item(sqlite_engine, inc, external_key="OPS-7", external_url="javascript:alert(1)")
        assert "Jira OPS-7" not in client.get(f"/ui/incidents/{inc}").text

    def test_pull_request_link_from_an_execution_output(self, client, sqlite_engine):
        inc = _incident(sqlite_engine)
        with Session(sqlite_engine) as session:
            pk = session.query(models.Incident).filter_by(incident_id=inc).one().id
            session.add(
                models.Execution(
                    incident_id=pk,
                    action="open_fix_pr",
                    params={},
                    fingerprint="f" * 64,
                    policy_decision="allow",
                    origin="live",
                    result=models.ExecutionResult.SUCCESS,
                    detail="",
                    output={"pr_url": "https://github.com/acme/widgets/pull/5"},
                    started_at=datetime.now(UTC),
                )
            )
            session.commit()
        row = _row(client.get(f"/ui/incidents/{inc}").text)
        assert 'href="https://github.com/acme/widgets/pull/5"' in row and "Pull request" in row

    def test_no_links_row_when_nothing_is_known(self, client, sqlite_engine):
        inc = _incident(sqlite_engine)
        page = client.get(f"/ui/incidents/{inc}").text
        assert 'class="ext-links"' not in page


def test_slack_stub_is_never_called(client, sqlite_engine, monkeypatch):
    """Rendering both pages must not reach Slack."""

    def boom():
        raise AssertionError("render called Slack")

    monkeypatch.setattr(oauth, "_http", boom)
    _save(sqlite_engine, "slack", team_id="T1", channel="C2")
    inc = _incident(sqlite_engine)
    _slack_row(sqlite_engine, inc, ts="1.2", channel="C2")
    assert client.get("/ui/").status_code == 200
    assert client.get(f"/ui/incidents/{inc}").status_code == 200
