"""The work-management provider package (subphases 2.3/2.10).

`contract.py` owns the provider-neutral vocabulary; the other modules
are the four implementations behind it - `memory` (fixture baseline and
the default), `jira` (2.3's first live provider), `linear` and `trello`
(2.10's proof the contract is real). `get_work_provider` is the only
construction site the outbox drain knows, so agents and the drain never
name a vendor - which is the 2.3 exit criterion made structural.
"""

from datetime import datetime

import httpx
from sqlalchemy.orm import Session

from helix.config import get_settings
from integrations.work.contract import (
    NEUTRAL_PRIORITIES,
    NEUTRAL_STATUSES,
    ProviderCapabilities,
    WorkItemError,
    WorkItemRef,
    WorkItemRejected,
    WorkItemSpec,
    WorkManagementProvider,
)
from integrations.work.jira import JiraProvider
from integrations.work.linear import LinearProvider
from integrations.work.memory import MemoryWorkProvider
from integrations.work.trello import TrelloProvider

__all__ = [
    "JiraProvider",
    "LinearProvider",
    "MemoryWorkProvider",
    "NEUTRAL_PRIORITIES",
    "NEUTRAL_STATUSES",
    "ProviderCapabilities",
    "TrelloProvider",
    "WorkItemError",
    "WorkItemRejected",
    "WorkItemRef",
    "WorkItemSpec",
    "WorkManagementProvider",
    "get_work_provider",
    "connected_jira_provider",
    "effective_work_provider",
    "get_work_provider_for",
]


def get_work_provider_for(kind: str) -> WorkManagementProvider | None:
    settings = get_settings()
    if kind == "memory":
        return MemoryWorkProvider()
    if kind == "jira" and settings.jira_api_token:
        return JiraProvider(
            settings.jira_base_url,
            settings.jira_email,
            settings.jira_api_token,
            settings.jira_project_key,
        )
    if kind == "linear" and settings.linear_api_key and settings.linear_team_id:
        return LinearProvider(settings.linear_api_key, settings.linear_team_id)
    if (
        kind == "trello"
        and settings.trello_api_key
        and settings.trello_api_token
        and settings.trello_board_id
    ):
        return TrelloProvider(
            settings.trello_api_key, settings.trello_api_token, settings.trello_board_id
        )
    return None


def connected_jira_provider(
    session: Session, *, client: httpx.Client | None = None
) -> JiraProvider | None:
    """A Jira provider over the stored OAuth connection, or None when none
    is usable (no connection, connections disabled, or no cloud id).

    Rotated tokens are saved in their own short transaction on the same
    database: the drain's session may be mid-transaction and roll back,
    but the old refresh token is already invalid, so the new pair must
    survive that."""
    from state.connections import active_connection, update_tokens
    from state.db import session_scope

    connection = active_connection(session, "jira")
    cloud_id = (connection.settings.get("cloud_id") if connection else None) or None
    site_url = (connection.settings.get("site_url") if connection else None) or None
    if connection is None or not cloud_id or not site_url:
        return None
    engine = session.get_bind().engine

    def on_refresh(access_token: str, refresh_token: str, expires_at: datetime) -> None:
        with session_scope(engine) as own:
            update_tokens(own, "jira", access_token, refresh_token, expires_at)

    settings = get_settings()
    return JiraProvider.from_oauth(
        cloud_id=cloud_id,
        site_url=site_url,
        project_key=connection.settings.get("project_key") or settings.jira_project_key,
        access_token=connection.access_token,
        refresh_token=connection.refresh_token,
        expires_at=connection.expires_at,
        client_id=settings.jira_oauth_client_id,
        client_secret=settings.jira_oauth_client_secret,
        on_refresh=on_refresh,
        client=client,
    )


def effective_work_provider(session: Session) -> str:
    """The work provider kind to enqueue for and drain with. The default
    `memory` yields to a connected Jira account, so connecting Jira needs no
    .env edit; an explicitly configured provider is left as it is.
    `active_connection` reads as None under HELIX_CONNECTIONS_ENABLED=false,
    so the benchmark keeps its memory provider."""
    from state.connections import active_connection

    kind = get_settings().work_provider
    if kind != "memory":
        return kind
    connection = active_connection(session, "jira")
    if connection is not None and connection.settings.get("cloud_id"):
        return "jira"
    return kind


def get_work_provider() -> WorkManagementProvider | None:
    """Resolve HELIX_WORK_PROVIDER into a provider instance.

    `memory` always resolves; a live provider is built only when its
    credentials are configured - missing credentials return None so the
    outbox drain records its rows `skipped` rather than crashing (the
    same resolver contract the Slack/Notion clients follow).
    """
    return get_work_provider_for(get_settings().work_provider)
