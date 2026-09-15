"""Recursive secret redaction for evidence payloads.

`redact()` walks dicts, lists and strings and strips credentials wherever
they appear:

- `Authorization: Bearer/Basic ...` headers and bare `Bearer <token>` text
- well-known token shapes (`sk-...`, `ghp_...`, `github_pat_...`,
  `glpat-...`, `xox[baprs]-...`, `AKIA...`, JWTs)
- connection strings carrying credentials (`scheme://user:pass@host`)
- any dict KEY matching /api_?key|password|token|secret|credential/i, at
  any depth - the value is replaced with `<redacted>` whatever it held
- Kubernetes Secret objects (`kind: Secret`): every value under
  `.data`/`.stringData` becomes `<redacted>` with key names preserved

Key names are always preserved - which key existed is legitimate evidence;
only values are hidden. Note that a key merely *containing* a sensitive
word (e.g. `secretRef`) is redacted too: losing a reference is acceptable,
leaking a credential is not.

This runs inside every capability's return path via `build_evidence`
(C6). Callers must never rely on redacting at the call site.
"""

import re
from typing import Any

REDACTED = "<redacted>"

_SENSITIVE_KEY = re.compile(r"api_?key|password|token|secret|credential", re.IGNORECASE)

# "Authorization: Bearer x" / "Authorization=Basic x" header text.
_AUTHORIZATION = re.compile(r"(?i)(authorization\s*[:=]\s*)(bearer|basic)\s+\S+")
# A bare "Bearer <token>" outside an Authorization header.
_BEARER = re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]{4,}")

# Credential-bearing URI: scheme://user:pass@host - the authority's
# userinfo is replaced, scheme and host stay visible for diagnosis.
_CONNECTION_STRING = re.compile(r"\b([a-zA-Z][a-zA-Z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@")

# Well-known secret token shapes seen in DevOps tooling.
_TOKEN = re.compile(
    r"\b(?:"
    r"sk-[A-Za-z0-9_-]{4,}"  # sk-* style keys (OpenAI/Groq/sandbox placeholder)
    r"|xox[baprs]-[A-Za-z0-9-]{4,}"  # Slack
    r"|gh[pousr]_[A-Za-z0-9]{10,}"  # GitHub PAT/oauth/etc
    r"|github_pat_[A-Za-z0-9_]{10,}"  # GitHub fine-grained PAT
    r"|glpat-[A-Za-z0-9_-]{10,}"  # GitLab PAT
    r"|AKIA[A-Z0-9]{16}"  # AWS access key id
    r"|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"  # JWT
    r")"
)


def _redact_string(text: str) -> str:
    text = _AUTHORIZATION.sub(rf"\1\2 {REDACTED}", text)
    text = _BEARER.sub(rf"\1 {REDACTED}", text)
    text = _CONNECTION_STRING.sub(rf"\1{REDACTED}@", text)
    text = _TOKEN.sub(REDACTED, text)
    return text


def _redact_dict(obj: dict) -> dict:
    is_secret = obj.get("kind") == "Secret"
    out = {}
    for key, value in obj.items():
        if isinstance(key, str) and _SENSITIVE_KEY.search(key):
            out[key] = REDACTED
        elif is_secret and key in ("data", "stringData") and isinstance(value, dict):
            out[key] = {k: REDACTED for k in value}
        else:
            out[key] = _redact_any(value)
    return out


def _redact_any(value: Any) -> Any:
    if isinstance(value, dict):
        return _redact_dict(value)
    if isinstance(value, list):
        return [_redact_any(item) for item in value]
    if isinstance(value, str):
        return _redact_string(value)
    return value


def redact(value: Any) -> Any:
    """Return a copy of `value` with credentials stripped. Safe on any
    JSON-shaped structure; non-container scalars pass through unchanged
    except strings, which are pattern-scrubbed."""
    return _redact_any(value)
