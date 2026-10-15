"""The pull request bot's effective config (slice 10): DB settings over
server defaults, the daily limit enforced where the write client is chosen,
and the repo/base/draft choices reaching GitHub. The store is a throwaway
SQLite file; GitHub is httpx.MockTransport. Nothing leaves the test."""

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from agent.graph import _live_github_readback
from helix.config import get_settings
from integrations.fakes import FakeGitHubWrite
from integrations.github.write import GitHubWriteClient
from integrations.writes import get_github_write
from state import models, pr_bot
from state.intake import Signal, intake_signal


@pytest.fixture
def engine(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'bot.db'}"
    monkeypatch.setenv("HELIX_DATABASE_URL", url)
    engine = create_engine(url)
    models.Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("HELIX_ENV_FILE", "")
    monkeypatch.setenv("HELIX_GITHUB_REPO", "acme/server-default")
    monkeypatch.setenv("HELIX_GITHUB_TOKEN", "ghp_server")
    for name in (
        "HELIX_GITHUB_WRITES",
        "HELIX_EVIDENCE_SOURCE",
        "HELIX_CONNECTIONS_ENABLED",
    ):
        monkeypatch.delenv(name, raising=False)


def _store(engine, **columns):
    with Session(engine) as session:
        session.add(models.PRBotSettings(id=1, **columns))
        session.commit()


def _add_live_prs(engine, count, *, origin="live", age_hours=1, action="open_fix_pr", first=0):
    created = datetime.now(UTC) - timedelta(hours=age_hours)
    with Session(engine) as session:
        incident_id = intake_signal(
            session,
            Signal(source="kubernetes", service="payment-api", signature={"reason": "CrashLoop"}),
        ).incident_id
        pk = session.query(models.Incident.id).filter_by(incident_id=incident_id).scalar()
        for attempt in range(first, first + count):
            session.add(
                models.Execution(
                    incident_id=pk,
                    attempt=attempt,
                    action=action,
                    params={},
                    fingerprint="f",
                    policy_decision="allow",
                    origin=origin,
                    result=models.ExecutionResult.SUCCESS,
                    detail="",
                    started_at=created,
                    created_at=created,
                )
            )
        session.commit()


# -- the effective config ------------------------------------------------


def test_defaults_equal_todays_behaviour(engine, monkeypatch):
    # Recorded evidence: PRs are recorded; live evidence: they are real.
    monkeypatch.setenv("HELIX_EVIDENCE_SOURCE", "fixtures")
    assert pr_bot.load_config() == pr_bot.PRBotConfig(
        real_prs=False,
        repo="acme/server-default",
        base_branch="main",
        draft=False,
        daily_limit=None,
    )
    monkeypatch.setenv("HELIX_EVIDENCE_SOURCE", "live")
    assert pr_bot.load_config().real_prs is True


def test_server_live_setting_is_the_default_over_recorded_evidence(engine, monkeypatch):
    monkeypatch.setenv("HELIX_EVIDENCE_SOURCE", "fixtures")
    monkeypatch.setenv("HELIX_GITHUB_WRITES", "live")
    assert pr_bot.load_config().real_prs is True


def test_db_values_override_the_server_defaults(engine, monkeypatch):
    monkeypatch.setenv("HELIX_EVIDENCE_SOURCE", "fixtures")
    _store(engine, real_prs=True, repo="acme/bot", base_branch="develop", draft=True, daily_limit=3)
    assert pr_bot.load_config() == pr_bot.PRBotConfig(
        real_prs=True, repo="acme/bot", base_branch="develop", draft=True, daily_limit=3
    )


def test_db_off_beats_server_live(engine, monkeypatch):
    monkeypatch.setenv("HELIX_GITHUB_WRITES", "live")
    _store(engine, real_prs=False)
    assert pr_bot.load_config().real_prs is False


def test_null_columns_fall_back_one_by_one(engine, monkeypatch):
    monkeypatch.setenv("HELIX_EVIDENCE_SOURCE", "fixtures")
    _store(engine, draft=True)
    config = pr_bot.load_config()
    assert config.draft is True
    assert (config.real_prs, config.repo, config.base_branch, config.daily_limit) == (
        False,
        "acme/server-default",
        "main",
        None,
    )


def test_env_recorded_beats_db_real_prs_true(engine, monkeypatch):
    monkeypatch.setenv("HELIX_GITHUB_WRITES", "recorded")
    _store(engine, real_prs=True)
    assert pr_bot.load_config().real_prs is False
    assert isinstance(get_github_write(), FakeGitHubWrite)


def test_isolation_ignores_the_db(engine, monkeypatch):
    monkeypatch.setenv("HELIX_EVIDENCE_SOURCE", "fixtures")
    monkeypatch.setenv("HELIX_CONNECTIONS_ENABLED", "false")
    _store(engine, real_prs=True, repo="acme/bot", draft=True, daily_limit=1)
    assert pr_bot.load_config() == pr_bot.PRBotConfig(
        real_prs=False,
        repo="acme/server-default",
        base_branch="main",
        draft=False,
        daily_limit=None,
    )
    assert isinstance(get_github_write(), FakeGitHubWrite)


def test_unreadable_store_falls_back_to_server_defaults(tmp_path, monkeypatch):
    # No tables at all in this database.
    monkeypatch.setenv("HELIX_DATABASE_URL", f"sqlite:///{tmp_path / 'empty.db'}")
    monkeypatch.setenv("HELIX_EVIDENCE_SOURCE", "fixtures")
    assert pr_bot.load_config() == pr_bot.PRBotConfig(
        real_prs=False,
        repo="acme/server-default",
        base_branch="main",
        draft=False,
        daily_limit=None,
    )


# -- the daily limit, where the write client is chosen ---------------------


def _live(monkeypatch):
    monkeypatch.setenv("HELIX_EVIDENCE_SOURCE", "fixtures")


def test_limit_reached_returns_the_recording_fake(engine, monkeypatch):
    _live(monkeypatch)
    _store(engine, real_prs=True, daily_limit=2)
    _add_live_prs(engine, 2)
    assert isinstance(get_github_write(), FakeGitHubWrite)


def test_under_the_limit_is_live(engine, monkeypatch):
    _live(monkeypatch)
    _store(engine, real_prs=True, daily_limit=2)
    _add_live_prs(engine, 1)
    assert isinstance(get_github_write(), GitHubWriteClient)


def test_no_limit_is_live_whatever_the_count(engine, monkeypatch):
    _live(monkeypatch)
    _store(engine, real_prs=True)
    _add_live_prs(engine, 50)
    assert isinstance(get_github_write(), GitHubWriteClient)


def test_old_recorded_and_other_executions_do_not_count(engine, monkeypatch):
    _live(monkeypatch)
    _store(engine, real_prs=True, daily_limit=1)
    _add_live_prs(engine, 3, age_hours=25)
    _add_live_prs(engine, 3, origin="fixture", first=10)
    _add_live_prs(engine, 3, action="close_pr", first=20)
    assert isinstance(get_github_write(), GitHubWriteClient)


def test_limit_is_inert_when_real_prs_are_off(engine, monkeypatch):
    _live(monkeypatch)
    _store(engine, real_prs=False, daily_limit=1)
    _add_live_prs(engine, 5)
    assert isinstance(get_github_write(), FakeGitHubWrite)


def test_limit_applies_to_the_server_live_default_too(engine, monkeypatch):
    _live(monkeypatch)
    monkeypatch.setenv("HELIX_GITHUB_WRITES", "live")
    _store(engine, daily_limit=1)
    _add_live_prs(engine, 1)
    assert isinstance(get_github_write(), FakeGitHubWrite)


def test_unreadable_count_with_a_limit_fails_closed(engine, monkeypatch):
    _live(monkeypatch)
    _store(engine, real_prs=True, daily_limit=5)

    def boom(session):
        raise RuntimeError("db down")

    monkeypatch.setattr(pr_bot, "live_prs_last_day", boom)
    assert isinstance(get_github_write(), FakeGitHubWrite)


# -- repo, base and draft reach GitHub -------------------------------------


def _recording_client(seen):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        seen.append((request.method, request.url.path, body))
        if request.url.path.endswith("/pulls"):
            return httpx.Response(201, json={"number": 7, "html_url": "https://x/pull/7"})
        if "/git/ref/" in request.url.path:
            return httpx.Response(200, json={"object": {"sha": "abc"}})
        if "/contents/" in request.url.path and request.method == "GET":
            return httpx.Response(404, json={})
        return httpx.Response(201, json={})

    return httpx.Client(transport=httpx.MockTransport(handler))


FILES = [{"path": "sandbox/payment-api/x.yaml", "content": "a: 1\n"}]


def test_draft_sends_draft_true_in_the_pr_create_request():
    seen = []
    client = GitHubWriteClient("acme/r", "t", client=_recording_client(seen), draft=True)
    client.open_fix_pr(branch="helix/INC-1", files=FILES, title="t")
    create = next(body for method, path, body in seen if path.endswith("/pulls"))
    assert create["draft"] is True


def test_not_draft_leaves_the_request_as_it_was():
    seen = []
    client = GitHubWriteClient("acme/r", "t", client=_recording_client(seen))
    client.open_fix_pr(branch="helix/INC-1", files=FILES, title="t")
    create = next(body for method, path, body in seen if path.endswith("/pulls"))
    assert "draft" not in create


def test_repo_and_base_from_the_bot_config_reach_the_write_client(engine, monkeypatch):
    _live(monkeypatch)
    _store(engine, real_prs=True, repo="acme/bot", base_branch="develop", draft=True)
    client = get_github_write()
    assert isinstance(client, GitHubWriteClient)
    seen = []
    client._client = _recording_client(seen)
    result = client.open_fix_pr(branch="helix/INC-1", files=FILES, title="t")
    paths = [path for _, path, _ in seen]
    assert "/repos/acme/bot/git/ref/heads/develop" in paths
    assert all(path.startswith("/repos/acme/bot/") for path in paths)
    create = next(body for method, path, body in seen if path.endswith("/pulls"))
    assert create["base"] == "develop" and create["draft"] is True
    assert result["repo"] == "acme/bot" and result["base"] == "develop"


def test_bounds_still_refuse_a_protected_branch_with_the_bot_config(engine, monkeypatch):
    from integrations.writes import WriteRefusedError

    _live(monkeypatch)
    _store(engine, real_prs=True, base_branch="develop")
    client = get_github_write()
    with pytest.raises(WriteRefusedError):
        client.open_fix_pr(branch="develop", files=FILES, title="t")


# -- the read-back reads where the bot wrote -------------------------------


def test_readback_reads_the_bots_repo_when_real_prs_are_on(engine, monkeypatch):
    _live(monkeypatch)
    _store(engine, real_prs=True, repo="acme/bot")
    reader = _live_github_readback(get_settings())
    assert reader is not None and reader._repo == "acme/bot"


def test_readback_is_off_when_real_prs_are_off(engine, monkeypatch):
    _live(monkeypatch)
    _store(engine, real_prs=False)
    assert _live_github_readback(get_settings()) is None


def test_readback_is_off_when_the_env_forces_recorded(engine, monkeypatch):
    _live(monkeypatch)
    monkeypatch.setenv("HELIX_GITHUB_WRITES", "recorded")
    _store(engine, real_prs=True)
    assert _live_github_readback(get_settings()) is None


def test_readback_is_off_over_live_evidence(engine, monkeypatch):
    monkeypatch.setenv("HELIX_EVIDENCE_SOURCE", "live")
    assert _live_github_readback(get_settings()) is None
