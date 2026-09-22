"""Slack request-signature verification (subphase 2.2).

CONTEXT.md section 10: Slack is where humans approve and escalate, so an
interaction payload is a decision waiting to be written - a forged or
replayed one is a forged approval. Slack signs every POST with the app
signing secret (`X-Slack-Signature: v0=<hmac-sha256 of
"v0:{timestamp}:{body}">`); verifying it is the webhook's only
authentication, since Slack cannot carry the intake bearer token.

Every failure path returns False rather than raising - the endpoint maps
False to 401, and a malformed header must never crash the ack path.
"""

import hashlib
import hmac
import time


def verify_signature(
    signing_secret: str,
    timestamp: str,
    body: bytes,
    signature: str,
    *,
    now: float | None = None,
    tolerance: float = 300.0,
) -> bool:
    """True only when `signature` is Slack's HMAC over the exact raw body.

    The timestamp is part of the signed string AND a freshness check:
    requests older than `tolerance` seconds (Slack recommends 5 min) are
    rejected so a captured POST cannot be replayed later. `now` is
    injectable so tests are deterministic.
    """
    try:
        ts = float(timestamp)
    except (TypeError, ValueError):
        return False
    if now is None:
        now = time.time()
    if abs(now - ts) > tolerance:
        return False
    digest = hmac.new(
        signing_secret.encode("utf-8"),
        b"v0:" + timestamp.encode("utf-8") + b":" + body,
        hashlib.sha256,
    ).hexdigest()
    expected = b"v0=" + digest.encode("ascii")
    try:
        provided = signature.encode("utf-8")
    except (AttributeError, UnicodeEncodeError):
        return False
    # compare_digest on bytes never raises on length/content mismatch -
    # a malformed signature is simply not equal.
    return hmac.compare_digest(expected, provided)
