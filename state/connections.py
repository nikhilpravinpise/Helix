"""Encrypted store for connected provider accounts (future_plans.md Stage 1).

Tokens are Fernet-encrypted before they reach the `connections` table, under
a key derived from HELIX_CONNECTIONS_KEY (any free-form secret; the Fernet
key is the urlsafe base64 of its SHA-256). With no key configured, saving
raises and reading returns None - "not connected", never a crash. A row the
current key cannot decrypt (key rotated or wrong) reads as not connected too,
with a warning that names the provider and never a token.

Functions flush but never commit; the caller owns the transaction
(state/db.session_scope).
"""

import base64
import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.orm import Session

from helix.config import get_settings
from helix.logging import get_logger
from state.models import Connection


class ConnectionsKeyMissing(RuntimeError):
    """HELIX_CONNECTIONS_KEY is not set, so tokens cannot be stored."""


@dataclass(frozen=True)
class ConnectionRecord:
    """A decrypted connection. Tokens are excluded from repr."""

    provider: str
    account: str
    scopes: list[str]
    access_token: str = field(repr=False)
    refresh_token: str | None = field(repr=False)
    expires_at: datetime | None
    settings: dict[str, Any]


def _fernet() -> Fernet | None:
    secret = get_settings().connections_key
    if not secret:
        return None
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest()))


def _require_fernet() -> Fernet:
    fernet = _fernet()
    if fernet is None:
        raise ConnectionsKeyMissing("HELIX_CONNECTIONS_KEY is not set; cannot store a connection")
    return fernet


def _encrypt(fernet: Fernet, value: str) -> str:
    return fernet.encrypt(value.encode()).decode()


def save_connection(
    session: Session,
    provider: str,
    *,
    account: str,
    scopes: list[str],
    access_token: str,
    refresh_token: str | None,
    expires_at: datetime | None,
    settings: dict[str, Any],
) -> None:
    """Insert or replace the provider's connection."""
    fernet = _require_fernet()
    row = session.get(Connection, provider) or Connection(provider=provider)
    row.account = account
    row.scopes = list(scopes)
    row.access_token = _encrypt(fernet, access_token)
    row.refresh_token = _encrypt(fernet, refresh_token) if refresh_token else None
    row.expires_at = expires_at
    row.settings = dict(settings)
    session.add(row)
    session.flush()


def get_connection(session: Session, provider: str) -> ConnectionRecord | None:
    """The decrypted connection, or None if absent, no key is configured, or
    the key cannot decrypt the stored tokens."""
    row = session.get(Connection, provider)
    fernet = _fernet()
    if row is None or fernet is None:
        return None
    try:
        access = fernet.decrypt(row.access_token.encode()).decode()
        refresh = fernet.decrypt(row.refresh_token.encode()).decode() if row.refresh_token else None
    except InvalidToken:
        get_logger().warning("connection_undecryptable", provider=provider)
        return None
    return ConnectionRecord(
        provider=row.provider,
        account=row.account,
        scopes=list(row.scopes),
        access_token=access,
        refresh_token=refresh,
        expires_at=row.expires_at,
        settings=dict(row.settings),
    )


def delete_connection(session: Session, provider: str) -> bool:
    """Remove the provider's connection; False if there was none."""
    row = session.get(Connection, provider)
    if row is None:
        return False
    session.delete(row)
    session.flush()
    return True


def update_tokens(
    session: Session,
    provider: str,
    access_token: str,
    refresh_token: str | None,
    expires_at: datetime | None,
) -> bool:
    """Store refreshed tokens, leaving account, scopes and settings alone. A
    None refresh_token keeps the existing one (providers that do not rotate
    it). False if the provider is not connected."""
    row = session.get(Connection, provider)
    if row is None:
        return False
    fernet = _require_fernet()
    row.access_token = _encrypt(fernet, access_token)
    if refresh_token:
        row.refresh_token = _encrypt(fernet, refresh_token)
    row.expires_at = expires_at
    session.flush()
    return True
