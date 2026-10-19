"""Find teammates in Slack through the connected workspace (future_plans.md
"Team and services", Slack matching). Best effort by design: a Slack failure
is reported as a status for the caller to word, never raised, so it can never
block an invite or a sign-in. Tokens are never logged.
"""

from dataclasses import dataclass

from sqlalchemy.orm import Session

from helix.logging import get_logger
from integrations.slack import SlackError, oauth
from state import connections

EMAIL_SCOPE = "users:read.email"


@dataclass(frozen=True)
class Lookup:
    """`status` is found, not_found, not_connected, reconnect (the connection
    predates the email scopes) or error (Slack unreachable or refused)."""

    status: str
    slack_id: str | None = None
    #: The email that matched, when found.
    email: str | None = None


def needs_reconnect(session: Session) -> bool:
    """Slack is connected but without the scope to look people up. Decided
    from the stored scopes, so no network call."""
    stored = connections.active_connection(session, "slack")
    return stored is not None and EMAIL_SCOPE not in stored.scopes


def find_by_emails(session: Session, emails: list[str]) -> Lookup:
    """The first email Slack knows, in order."""
    stored = connections.active_connection(session, "slack")
    if stored is None or not stored.access_token:
        return Lookup("not_connected")
    if EMAIL_SCOPE not in stored.scopes:
        return Lookup("reconnect")
    for email in emails:
        try:
            found = oauth.lookup_user_by_email(stored.access_token, email)
        except SlackError as exc:
            if str(exc) == "missing_scope":
                return Lookup("reconnect")
            get_logger().warning("slack_lookup_failed", error=str(exc))
            return Lookup("error")
        if found:
            return Lookup("found", found, email)
    return Lookup("not_found")


def connected(session: Session) -> bool:
    """Slack is connected (whatever its scopes)."""
    stored = connections.active_connection(session, "slack")
    return stored is not None and bool(stored.access_token)


MAX_PICKS = 10


def search_members(session: Session, query: str) -> tuple[str, list[dict[str, str]]]:
    """Workspace members whose name or display name contains `query`, for the
    admin to pick from. (status, members): status is ok, not_connected,
    reconnect or error. Nothing is cached; every search asks Slack."""
    stored = connections.active_connection(session, "slack")
    if stored is None or not stored.access_token:
        return "not_connected", []
    try:
        members = oauth.list_members(stored.access_token)
    except SlackError as exc:
        if str(exc) == "missing_scope":
            return "reconnect", []
        get_logger().warning("slack_list_failed", error=str(exc))
        return "error", []
    needle = query.strip().lower()
    hits = [m for m in members if needle and needle in f"{m['name']} {m['display']}".lower()]
    return "ok", hits[:MAX_PICKS]


def send_dm(session: Session, slack_id: str, text: str) -> bool:
    """DM a member; False when Slack is not connected or refuses."""
    stored = connections.active_connection(session, "slack")
    if stored is None or not stored.access_token:
        return False
    try:
        oauth.send_dm(stored.access_token, slack_id, text)
    except SlackError as exc:
        get_logger().warning("slack_dm_failed", error=str(exc))
        return False
    return True
