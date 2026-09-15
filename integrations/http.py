"""Shared HTTP plumbing for the httpx-based capabilities (github, jenkins,
elk). One place for the uniform vendor-error mapping so all of them speak
the same EvidenceError vocabulary."""

import httpx

from integrations.evidence import (
    EvidenceAuthError,
    EvidenceError,
    EvidenceNotFoundError,
    EvidenceRateLimitError,
)


def raise_for_status(response: httpx.Response) -> None:
    """Map an HTTP error response onto the evidence error hierarchy:
    401/403 -> auth, 404 -> not found, 429 -> rate limit, any other >=400
    -> EvidenceError. Callers handle timeouts and transport errors
    separately."""
    status = response.status_code
    if status < 400:
        return
    detail = f"HTTP {status} from {response.request.url}"
    if status in (401, 403):
        raise EvidenceAuthError(detail)
    if status == 404:
        raise EvidenceNotFoundError(detail)
    if status == 429:
        raise EvidenceRateLimitError(detail)
    raise EvidenceError(detail)
