"""The Slack integration (subphase 2.2) - human communication, approval
and escalation as a projection of canonical state. CONTEXT.md section 10
is explicit: "Slack is not the canonical database" - sends go through
the integration_outbox drain, decisions come back signed through
POST /slack/interactions, and the approvals row is the truth both ways.
"""

from helix.config import get_settings
from integrations.slack.client import SlackClient, SlackError
from integrations.slack.memory import MemorySlackClient
from integrations.slack.notify import approval_blocks
from integrations.slack.signature import verify_signature
from integrations.slack.web import WebSlackClient

__all__ = [
    "MemorySlackClient",
    "SlackClient",
    "SlackError",
    "WebSlackClient",
    "approval_blocks",
    "get_slack_client",
    "verify_signature",
]


def get_slack_client() -> SlackClient | None:
    """Resolve the configured transport: WebSlackClient when a bot token is
    configured, None otherwise - the drain records that case as
    `skipped`, never a crash. Fixture evidence does not fake Slack: tests
    and the benchmark stay offline by blanking the token."""
    settings = get_settings()
    if not settings.slack_bot_token:
        return None
    return WebSlackClient(settings.slack_bot_token)
