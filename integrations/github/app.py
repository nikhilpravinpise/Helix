"""The Helix GitHub App (Connections page, FOLLOWUPS #15).

Connect GitHub installs this app on the user's account; Helix then acts as
the app, with short-lived installation tokens instead of a stored password.
Small functions over the endpoints that needs, so the UI routes and the
clients stay thin:

- `app_jwt`: a 9 minute RS256 token signed with the app's private key, the
  credential for /app/* calls. Signed directly with `cryptography` (already a
  dependency), which is shorter than adding a JWT library for one claim set.
- `installation_token`: POST /app/installations/{id}/access_tokens, cached
  in-process per installation until 5 minutes before GitHub's expiry.
- `get_installation` and `list_installation_repos`.

Every failure (transport, HTTP, non-JSON, a missing setting) is a
GitHubAppError whose message is a short code (`http_404`, `not_configured`)
safe to show. The key, the JWT and the tokens are never logged or put in an
error. `_http` is the injection seam for tests (an httpx.Client on a
MockTransport).
"""

import base64
import json
import re
import time
from datetime import datetime
from typing import Any

import httpx
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from helix.config import get_settings
from state import connections

API_ROOT = "https://api.github.com"
#: Repo-list pages of 100 followed at most this many times.
MAX_REPO_PAGES = 5
#: GitHub allows a JWT up to 10 minutes; iat is backdated for clock skew.
JWT_BACKDATE_SECONDS = 60
JWT_LIFETIME_SECONDS = 9 * 60
#: A cached installation token is replaced this long before it expires.
REFRESH_MARGIN_SECONDS = 5 * 60
_INSTALLATION_ID = re.compile(r"[0-9]{1,20}")

#: installation id -> (token, expiry as epoch seconds)
_token_cache: dict[str, tuple[str, float]] = {}


class GitHubAppError(Exception):
    """A GitHub App call failed; the message is a short safe code."""


def _http() -> httpx.Client:
    return httpx.Client(timeout=10.0)


def _now() -> float:
    return time.time()


def _parse_time(value: str) -> float:
    return datetime.fromisoformat(value).timestamp()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _private_key() -> rsa.RSAPrivateKey:
    # A one-line env var carries newlines as a literal backslash-n.
    pem = (get_settings().github_app_private_key or "").replace("\\n", "\n").strip()
    if not pem:
        raise GitHubAppError("not_configured")
    try:
        key = serialization.load_pem_private_key(pem.encode(), password=None)
    except (ValueError, TypeError, UnsupportedAlgorithm) as exc:
        raise GitHubAppError("bad_private_key") from exc
    if not isinstance(key, rsa.RSAPrivateKey):
        raise GitHubAppError("bad_private_key")
    return key


def app_jwt() -> str:
    """A JWT identifying the app itself (iss = the app id)."""
    app_id = get_settings().github_app_id
    if not app_id:
        raise GitHubAppError("not_configured")
    key = _private_key()
    now = _now()
    header = _b64(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
    claims = _b64(
        json.dumps(
            {
                "iat": now - JWT_BACKDATE_SECONDS,
                "exp": now + JWT_LIFETIME_SECONDS,
                "iss": app_id,
            },
            separators=(",", ":"),
        ).encode()
    )
    signature = key.sign(f"{header}.{claims}".encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{header}.{claims}.{_b64(signature)}"


def _call(method: str, path: str, bearer: str, **kwargs: Any) -> Any:
    headers = {
        "Authorization": f"Bearer {bearer}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        with _http() as client:
            response = client.request(method, f"{API_ROOT}{path}", headers=headers, **kwargs)
    except httpx.HTTPError as exc:
        raise GitHubAppError("transport_error") from exc
    if response.status_code >= 400:
        raise GitHubAppError(f"http_{response.status_code}")
    try:
        return response.json()
    except ValueError as exc:
        raise GitHubAppError("non_json_response") from exc


def _checked_id(installation_id: str) -> str:
    """The id as a path segment: digits only, so a forged value can never
    steer the request to another endpoint."""
    if not _INSTALLATION_ID.fullmatch(str(installation_id)):
        raise GitHubAppError("bad_installation")
    return str(installation_id)


def get_installation(installation_id: str) -> dict:
    """GET /app/installations/{id} as the app: succeeds only for an
    installation of this app, which is what proves a callback's id is real."""
    body = _call("GET", f"/app/installations/{_checked_id(installation_id)}", app_jwt())
    account = body.get("account") if isinstance(body, dict) else None
    if not isinstance(account, dict) or not account.get("login"):
        raise GitHubAppError("malformed_response")
    return body


def installation_token(installation_id: str) -> str:
    """A token for the installation, minted at most once per hour."""
    installation_id = _checked_id(installation_id)
    cached = _token_cache.get(installation_id)
    if cached and cached[1] - REFRESH_MARGIN_SECONDS > _now():
        return cached[0]
    body = _call("POST", f"/app/installations/{installation_id}/access_tokens", app_jwt())
    try:
        token = body["token"]
        expires = _parse_time(body["expires_at"])
    except (KeyError, TypeError, ValueError) as exc:
        raise GitHubAppError("malformed_response") from exc
    if not isinstance(token, str) or not token:
        raise GitHubAppError("malformed_response")
    _token_cache[installation_id] = (token, expires)
    return token


def list_installation_repos(installation_id: str) -> list[str]:
    """The repos the installation can reach, as `owner/name`: GET
    /installation/repositories, paginated, bounded at MAX_REPO_PAGES pages."""
    token = installation_token(installation_id)
    found: list[str] = []
    for page in range(1, MAX_REPO_PAGES + 1):
        body = _call(
            "GET", "/installation/repositories", token, params={"per_page": 100, "page": page}
        )
        repos = body.get("repositories") if isinstance(body, dict) else None
        if not isinstance(repos, list):
            raise GitHubAppError("malformed_response")
        found += [r["full_name"] for r in repos if isinstance(r, dict) and r.get("full_name")]
        if len(repos) < 100:
            break
    return found


class ConnectedToken:
    """The token a GitHub client uses when it was given none: the connected
    installation's, else HELIX_GITHUB_TOKEN.

    The store is read on first use and never at construction, so building a
    client touches neither the database nor the network (and the benchmark's
    isolation, which makes load_connection return None, still holds). The
    installation token is resolved on every call through the cache: it lasts
    an hour, so a long run re-mints near expiry instead of failing midway."""

    def __init__(self) -> None:
        self._read = False
        self._installation: str | None = None
        self._stored: str | None = None

    def get(self) -> str | None:
        """Raises GitHubAppError when the installation's token cannot be
        minted - it does not fall back to HELIX_GITHUB_TOKEN, since that would
        silently change who the PR is authored by."""
        if not self._read:
            record = connections.load_connection("github")
            if record is not None:
                self._installation = record.settings.get("installation_id")
                self._stored = record.access_token
            self._read = True
        if self._installation:
            return installation_token(self._installation)
        return self._stored or get_settings().github_token
