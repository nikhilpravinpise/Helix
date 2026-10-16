"""Send email through Resend's HTTPS API (future_plans.md "Invite emails").
Render blocks SMTP, so this is one POST. Every failure (transport, HTTP,
non-JSON) is an EmailError whose message is a short code (`http_403`,
`transport_error`) safe to show; the provider's own message is logged, never
the API key. `_http` is the injection seam for tests (an httpx.Client on a
MockTransport).
"""

from typing import Any

import httpx

from helix.config import get_settings
from helix.logging import get_logger

API_URL = "https://api.resend.com/emails"


class EmailError(RuntimeError):
    """The send failed; the message is a short code, safe to show."""


def _http() -> httpx.Client:
    return httpx.Client(timeout=10.0)


def is_configured() -> bool:
    settings = get_settings()
    return bool(settings.email_api_key and settings.email_from)


def send_email(to: str, subject: str, html: str, text: str) -> str:
    """Send one message; returns the provider's id for it."""
    settings = get_settings()
    if not (settings.email_api_key and settings.email_from):
        raise EmailError("not_configured")
    try:
        with _http() as client:
            response = client.post(
                API_URL,
                headers={"Authorization": f"Bearer {settings.email_api_key}"},
                json={
                    "from": settings.email_from,
                    "to": [to],
                    "subject": subject,
                    "html": html,
                    "text": text,
                },
            )
    except httpx.HTTPError as exc:
        raise EmailError("transport_error") from exc
    try:
        body: Any = response.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    if response.status_code >= 400:
        get_logger().warning(
            "email_send_failed", status=response.status_code, message=str(body.get("message"))
        )
        raise EmailError(f"http_{response.status_code}")
    if not body.get("id"):
        raise EmailError("non_json_response")
    return str(body["id"])
