"""ELK evidence capability: bounded log queries against Elasticsearch REST.

Mirrors scripts/query-logs.sh exactly: service term on
`kubernetes.labels.app` OR `kubernetes.container.name`, time window via a
`now-<window>` range on @timestamp, optional `match_phrase` level filter on
`message`, size cap 200, sort @timestamp ascending. Unlike the script -
whose truncation warning goes to stderr and is invisible to anything
reading stdout (CF-3) - the result's `truncated`/`total` fields are
explicit on the returned Evidence.

Read-only: a single `_search` POST. Error mapping: 401/403 -> auth,
404 -> not found (index absent), 429 -> rate limit, other >=400 ->
EvidenceError; httpx timeouts -> timeout, transport errors ->
EvidenceError; `timed_out` or a malformed hits block -> partial.
"""

import httpx

from helix.config import get_settings
from integrations.evidence import (
    Evidence,
    EvidenceError,
    EvidenceKind,
    EvidenceOrigin,
    EvidencePartialError,
    EvidenceSource,
    EvidenceTimeoutError,
    build_evidence,
)
from integrations.http import raise_for_status

INDEX_PATTERN = "helix-logs-*"
MAX_HITS = 200


def build_log_query(service: str, window: str, level: str | None = None) -> dict:
    """The exact ES query body query-logs.sh constructs - kept as a shared
    function so the fixture path can document the same semantics."""
    if service == "*":
        service_clause: dict = {"match_all": {}}
    else:
        service_clause = {
            "bool": {
                "should": [
                    {"term": {"kubernetes.labels.app": service}},
                    {"term": {"kubernetes.container.name": service}},
                ],
                "minimum_should_match": 1,
            }
        }
    must = [service_clause]
    if level:
        must.append({"match_phrase": {"message": level}})
    return {
        "track_total_hits": True,
        "size": MAX_HITS,
        "sort": [{"@timestamp": "asc"}],
        "query": {
            "bool": {
                "must": must,
                "filter": [{"range": {"@timestamp": {"gte": f"now-{window}"}}}],
            }
        },
    }


def format_log_line(source: dict) -> str:
    """One hit's `_source` as `<timestamp> [<pod>] <message>` - the same
    line shape query-logs.sh prints and fixtures store."""
    ts = source.get("@timestamp", "")
    pod = (source.get("kubernetes") or {}).get("pod", {}).get("name", "")
    msg = source.get("message", "")
    return f"{ts} [{pod}] {msg}"


class ElkEvidenceClient:
    """Live ELK evidence capability over ES REST (httpx)."""

    def __init__(
        self,
        base_url: str | None = None,
        *,
        client: httpx.Client | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._base_url = (base_url or get_settings().es_url).rstrip("/")
        self._client = client or httpx.Client(timeout=timeout)

    def get_logs(self, service: str, window: str, *, level: str | None = None) -> Evidence:
        query = build_log_query(service, window, level)
        try:
            response = self._client.post(f"{self._base_url}/{INDEX_PATTERN}/_search", json=query)
        except httpx.TimeoutException as exc:
            raise EvidenceTimeoutError(f"elasticsearch timed out: {exc}") from exc
        except httpx.TransportError as exc:
            raise EvidenceError(f"elasticsearch transport error: {exc}") from exc
        raise_for_status(response)
        return normalize_search_response(
            response.json(), service=service, window=window, level=level
        )


def normalize_search_response(
    data: dict,
    *,
    service: str,
    window: str,
    level: str | None,
    origin: EvidenceOrigin = EvidenceOrigin.LIVE,
) -> Evidence:
    """An ES `_search` body into `log_lines` evidence. `timed_out` means the
    window was only partially scanned - that is EvidencePartialError, not a
    silently shortened result."""
    if data.get("timed_out"):
        raise EvidencePartialError("elasticsearch reported timed_out on the search")
    hits_block = data.get("hits")
    if not isinstance(hits_block, dict) or "hits" not in hits_block:
        raise EvidencePartialError("elasticsearch response had no hits block")
    hits = hits_block["hits"]
    total = (hits_block.get("total") or {}).get("value", len(hits))
    lines = [format_log_line(hit.get("_source") or {}) for hit in hits]
    truncated = total > len(lines)
    summary = f"{len(lines)} log line(s) for {service} (window {window}"
    summary += f", level {level!r}" if level else ""
    summary += f"; {total} matched total)" if truncated else ")"
    return build_evidence(
        source=EvidenceSource.ELK,
        kind=EvidenceKind.LOG_LINES,
        service=service,
        summary=summary,
        payload={
            "index": INDEX_PATTERN,
            "service": service,
            "window": window,
            "level": level,
            "lines": lines,
        },
        truncated=truncated,
        total=total,
        origin=origin,
    )
