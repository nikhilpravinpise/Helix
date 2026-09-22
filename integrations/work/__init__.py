"""The work-management provider package (subphases 2.3/2.10).

`contract.py` owns the provider-neutral vocabulary; the other modules
are the four implementations behind it - `memory` (fixture baseline and
the default), `jira` (2.3's first live provider), `linear` and `trello`
(2.10's proof the contract is real). `get_work_provider` is the only
construction site the outbox drain knows, so agents and the drain never
name a vendor - which is the 2.3 exit criterion made structural.
"""

from helix.config import get_settings
from integrations.work.contract import (
    NEUTRAL_PRIORITIES,
    NEUTRAL_STATUSES,
    ProviderCapabilities,
    WorkItemError,
    WorkItemRef,
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
    "WorkItemRef",
    "WorkItemSpec",
    "WorkManagementProvider",
    "get_work_provider",
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


def get_work_provider() -> WorkManagementProvider | None:
    """Resolve HELIX_WORK_PROVIDER into a provider instance.

    `memory` always resolves; a live provider is built only when its
    credentials are configured - missing credentials return None so the
    outbox drain records its rows `skipped` rather than crashing (the
    same resolver contract the Slack/Notion clients follow).
    """
    return get_work_provider_for(get_settings().work_provider)
