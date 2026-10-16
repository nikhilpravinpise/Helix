"""The invite email (future_plans.md "Invite emails"): the message, and a
best-effort send that never blocks an invite. The vendor call is
integrations/email.py.
"""

from datetime import UTC, datetime
from html import escape

from sqlalchemy.orm import Session

from helix.logging import get_logger
from integrations import email
from integrations.email import EmailError
from state import models, team


def build(
    inviter: str, role: str, login: str, link: str, expires_at: datetime
) -> tuple[str, str, str]:
    """(subject, html, text). Every value is escaped in the HTML."""
    a_role = {"owner": "an owner", "admin": "an admin"}.get(role, "a member")
    when = team._aware(expires_at).astimezone(UTC)
    expires = f"{when:%B} {when.day}, {when.year}"
    subject = f"{inviter} invited you to Helix"
    lead = f"{inviter} invited you to join the team on Helix as {a_role}."
    sign_in = f"Sign in with GitHub as {login} to accept."
    expiry = f"This invite link expires on {expires}."
    text = f"{lead}\n\nAccept the invite: {link}\n\n{sign_in}\n{expiry}\n"
    e = escape
    html = (
        '<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;'
        'font-size:15px;line-height:1.5;color:#1f2328;max-width:480px">'
        f"<p>{e(lead)}</p>"
        f'<p><a href="{e(link, quote=True)}" style="display:inline-block;padding:10px 18px;'
        "background:#1f2328;color:#ffffff;text-decoration:none;border-radius:6px;"
        'font-weight:600">Accept the invite</a></p>'
        f"<p>Sign in with GitHub as <b>{e(login)}</b> to accept.</p>"
        f'<p style="color:#59636e">{e(expiry)}</p>'
        f'<p style="color:#59636e;font-size:13px">Button not working? Open {e(link)}</p>'
        "</div>"
    )
    return subject, html, text


def send_invite(
    session: Session, row: models.Invitation, inviter: str, public_url: str
) -> tuple[str, str]:
    """Email the invite link to `row.email`, if there is one. Returns
    (status, code): status is `none` (no address), `off` (email not set up),
    `sent` or `failed`; code is the short error for `failed`. Sets
    `emailed_at` on success. Best effort: nothing here raises."""
    if not row.email:
        return "none", ""
    if not email.is_configured():
        return "off", ""
    subject, html, text = build(
        inviter, row.role, row.login, f"{public_url}/invite/{row.token}", row.expires_at
    )
    try:
        email.send_email(row.email, subject, html, text)
    except EmailError as exc:
        get_logger().warning("invite_email_failed", error=str(exc))
        return "failed", str(exc)
    row.emailed_at = datetime.now(UTC)
    session.flush()
    return "sent", ""
