#!/usr/bin/env bash
# Bounded log query against the host Elasticsearch: service, time window,
# level. This is the exact query shape subphase 1.3's evidence layer needs -
# kept thin on purpose, no speculative options beyond that.
#
# Usage: scripts/query-logs.sh <service> <window> [level]
#   service  kubernetes.labels.app / kubernetes.container.name to match
#            (e.g. payment-api, downstream-stub). Use "*" for any service.
#   window   how far back to search, an Elasticsearch date-math offset
#            (e.g. 15m, 1h, 24h)
#   level    optional case-insensitive substring match against the log line
#            (e.g. error, info). Omit for all levels.
#
# Prints matching log lines, newest last, one per line: "<timestamp> [<pod>]
# <message>".
set -euo pipefail

if [[ $# -lt 2 || $# -gt 3 ]]; then
  echo "usage: $0 <service> <window e.g. 15m|1h> [level]" >&2
  exit 1
fi

SERVICE="$1"
WINDOW="$2"
LEVEL="${3:-}"

ES_URL="${ES_URL:-http://localhost:9200}"
INDEX_PATTERN="helix-logs-*"
MAX_HITS=200

service_clause='{"match_all": {}}'
if [[ "${SERVICE}" != "*" ]]; then
  service_clause='{"bool": {"should": [
    {"term": {"kubernetes.labels.app": "'"${SERVICE}"'"}},
    {"term": {"kubernetes.container.name": "'"${SERVICE}"'"}}
  ], "minimum_should_match": 1}}'
fi

level_clause=""
if [[ -n "${LEVEL}" ]]; then
  level_clause=',{"match_phrase": {"message": "'"${LEVEL}"'"}}'
fi

query='{
  "track_total_hits": true,
  "size": '"${MAX_HITS}"',
  "sort": [{"@timestamp": "asc"}],
  "query": {
    "bool": {
      "must": ['"${service_clause}"''"${level_clause}"'],
      "filter": [{"range": {"@timestamp": {"gte": "now-'"${WINDOW}"'"}}}]
    }
  }
}'

RESPONSE_FILE="$(mktemp)"
trap 'rm -f "${RESPONSE_FILE}"' EXIT

curl -s -X POST "${ES_URL}/${INDEX_PATTERN}/_search" \
  -H 'Content-Type: application/json' \
  -d "${query}" -o "${RESPONSE_FILE}"

# Read from a file, not a command-line argument: Elasticsearch responses for
# a full window of logs can exceed Linux's 128KB per-argv limit
# (MAX_ARG_STRLEN), which silently truncates a value passed as "$@".
if ! python3 -c "import json; json.load(open('${RESPONSE_FILE}'))" >/dev/null 2>&1; then
  echo "query-logs failed: elasticsearch did not return valid JSON" >&2
  cat "${RESPONSE_FILE}" >&2
  exit 1
fi

python3 - "${RESPONSE_FILE}" <<'EOF'
import json
import sys

with open(sys.argv[1]) as f:
    data = json.load(f)
if "error" in data:
    print("elasticsearch error:", json.dumps(data["error"]), file=sys.stderr)
    sys.exit(1)

hits = data.get("hits", {}).get("hits", [])
if not hits:
    print("no matching log lines")
    sys.exit(0)

# Elasticsearch applies the sort before truncating to "size", so a window
# holding more than MAX_HITS lines returns the oldest ones and drops the
# newest, which is often exactly where a failure is. Capturing the crashloop
# fixture hit this: over a wide window the pod's own startup failure did not
# come back, and nothing in the output said the result had been cut short.
#
# The direction is not the bug - whether the fault sits at the head or the
# tail of the window depends on when the query runs, so neither ascending
# nor descending is right in general. The bug is doing it silently. Say so
# on stderr, so the caller knows to narrow the window rather than reading a
# partial result as the whole story. stderr specifically: stdout is the log
# lines, and callers redirect it.
total = data.get("hits", {}).get("total", {}).get("value", len(hits))
if total > len(hits):
    print(
        f"warning: {total} lines matched, showing the oldest {len(hits)}. "
        f"Narrow the window to see the rest.",
        file=sys.stderr,
    )

for hit in hits:
    src = hit["_source"]
    ts = src.get("@timestamp", "")
    pod = src.get("kubernetes", {}).get("pod", {}).get("name", "")
    msg = src.get("message", "")
    print(f"{ts} [{pod}] {msg}")
EOF
