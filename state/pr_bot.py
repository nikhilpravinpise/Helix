"""The pull request bot's effective config (the Connections page's "Pull
request bot" card).

The stored row (models.PRBotSettings) holds only what a developer chose; a
NULL column means "use the server default", so the page can show which
values are defaults and a later change to the server's env still reaches
every unset field. `resolve` is the single place that turns row + env into
what the bot does.

Real PRs, in order of precedence:
  1. HELIX_GITHUB_WRITES=recorded - always recorded, whatever the DB says
     (seeding relies on it to never open a PR).
  2. the stored real_prs, when set.
  3. HELIX_GITHUB_WRITES=live, else the evidence-source rule (live
     evidence writes for real, recorded evidence records).
The other fields: repo -> HELIX_GITHUB_REPO, base branch -> "main", draft ->
off, daily limit -> none.

Stored settings are off under HELIX_CONNECTIONS_ENABLED=false (the
benchmark's isolation).
An unreadable store reads as "no row", with a warning, so a database hiccup
never stops a run that works from the server's own settings.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from helix.config import Settings, get_settings
from helix.logging import get_logger
from state import models

BASE_BRANCH = "main"
MAX_DAILY_LIMIT = 1000


@dataclass(frozen=True)
class PRBotConfig:
    real_prs: bool
    repo: str
    base_branch: str
    draft: bool
    daily_limit: int | None


def default_real_prs(settings: Settings) -> bool:
    """Real PRs by the server's own settings alone (no stored row)."""
    if settings.github_writes is not None:
        return settings.github_writes == "live"
    return settings.evidence_source != "fixtures"


def resolve(settings: Settings, row: models.PRBotSettings | None) -> PRBotConfig:
    """The effective config for `row` (None = nothing stored)."""
    if settings.github_writes == "recorded":
        real_prs = False
    elif row is not None and row.real_prs is not None:
        real_prs = row.real_prs
    else:
        real_prs = default_real_prs(settings)
    return PRBotConfig(
        real_prs=real_prs,
        repo=(row.repo if row else None) or settings.github_repo,
        base_branch=(row.base_branch if row else None) or BASE_BRANCH,
        draft=bool(row.draft) if row and row.draft is not None else False,
        daily_limit=row.daily_limit if row else None,
    )


def _settings_enabled(settings: Settings) -> bool:
    return settings.connections_enabled


def load_config() -> PRBotConfig:
    """The effective config, read through a short session on
    HELIX_DATABASE_URL (the PR bot's clients are built without one)."""
    settings = get_settings()
    if not _settings_enabled(settings):
        return resolve(settings, None)
    from state.db import session_scope

    try:
        with session_scope() as session:
            return resolve(settings, session.get(models.PRBotSettings, 1))
    except Exception as exc:
        get_logger().warning("pr_bot_settings_unreadable", error=str(exc))
        return resolve(settings, None)


def live_prs_last_day(session: Session) -> int:
    """Real fix PRs opened in the last 24 hours (live executions rows)."""
    since = datetime.now(UTC) - timedelta(hours=24)
    return (
        session.scalar(
            select(func.count())
            .select_from(models.Execution)
            .where(
                models.Execution.action == "open_fix_pr",
                models.Execution.origin == "live",
                models.Execution.created_at >= since,
            )
        )
        or 0
    )
