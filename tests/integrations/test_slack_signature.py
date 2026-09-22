"""Signature-verification tests (subphase 2.2).

The HMAC check is the webhook's only authentication, so the contract is
"False for anything that is not a genuine, fresh Slack signature" - and
never an exception, since malformed input must not crash the ack path.
"""

import hashlib
import hmac
import time

from integrations.slack.signature import verify_signature

SECRET = "test-signing-secret"
BODY = b"payload=%7B%22type%22%3A%22block_actions%22%7D"


def _sign(secret: str, timestamp: str, body: bytes) -> str:
    """Slack's signing recipe: v0=hex(hmac_sha256(secret, "v0:ts:body"))."""
    digest = hmac.new(
        secret.encode(), b"v0:" + timestamp.encode() + b":" + body, hashlib.sha256
    ).hexdigest()
    return f"v0={digest}"


def test_valid_signature_passes():
    ts = str(int(time.time()))
    assert verify_signature(SECRET, ts, BODY, _sign(SECRET, ts, BODY)) is True


def test_now_override_is_deterministic():
    ts = "1700000000"
    sig = _sign(SECRET, ts, BODY)
    assert verify_signature(SECRET, ts, BODY, sig, now=1700000000.0) is True
    assert verify_signature(SECRET, ts, BODY, sig, now=1700000000.0 + 299) is True


def test_wrong_secret_fails():
    ts = str(int(time.time()))
    assert verify_signature(SECRET, ts, BODY, _sign("other-secret", ts, BODY)) is False


def test_tampered_body_fails():
    ts = str(int(time.time()))
    sig = _sign(SECRET, ts, BODY)
    assert verify_signature(SECRET, ts, BODY + b"&injected=1", sig) is False


def test_tampered_timestamp_fails():
    ts = str(int(time.time()))
    sig = _sign(SECRET, ts, BODY)
    assert verify_signature(SECRET, str(int(time.time()) - 1), BODY, sig) is False


def test_expired_timestamp_fails():
    ts = str(int(time.time()) - 301)
    assert verify_signature(SECRET, ts, BODY, _sign(SECRET, ts, BODY)) is False


def test_future_timestamp_beyond_tolerance_fails():
    ts = str(int(time.time()) + 301)
    assert verify_signature(SECRET, ts, BODY, _sign(SECRET, ts, BODY)) is False


def test_boundary_timestamp_passes():
    ts = str(int(time.time()) - 299)
    assert verify_signature(SECRET, ts, BODY, _sign(SECRET, ts, BODY)) is True


def test_malformed_signatures_fail():
    ts = str(int(time.time()))
    for bad in ("", "nonsense", "v1=abc", "v0=", "v0=nothex", _sign(SECRET, ts, BODY)[2:]):
        assert verify_signature(SECRET, ts, BODY, bad) is False


def test_malformed_timestamps_fail():
    ts = str(int(time.time()))
    sig = _sign(SECRET, ts, BODY)
    for bad in ("", "not-a-number", "v0=abc"):
        assert verify_signature(SECRET, bad, BODY, sig) is False


def test_never_raises_on_garbage():
    assert verify_signature(SECRET, None, BODY, "v0=x") is False  # type: ignore[arg-type]
    assert verify_signature(SECRET, "1700000000", BODY, None) is False  # type: ignore[arg-type]
    assert verify_signature(SECRET, "1700000000", b"", "v0=x", now=1700000000.0) is False
