#!/usr/bin/env bash
# POST a failure signal to the Helix intake API (push model, decision D6).
# The benchmark harness and fault scripts use this helper; `helix intake`
# does the same thing without needing the API server.
#
# Usage: scripts/emit-signal.sh <kubernetes|jenkins> [signal.json]
#   signal.json  file containing the signal payload; omit or pass "-" to
#                read it from stdin.
#
# Environment:
#   HELIX_API_URL       intake API base URL (default http://<HELIX_API_BIND>:8787)
#   HELIX_INTAKE_TOKEN  optional bearer token, sent when set
#
# The payload is a state.intake.Signal document: source, service,
# environment (default sandbox), severity (default medium), signature and
# raw. `signature` carries the fault-identifying fields the dedup
# fingerprint is computed over - kubernetes: waiting_reason + container;
# jenkins: job + stage (docs/architecture/state-machine.md).
#
# Example:
#   scripts/emit-signal.sh kubernetes <<'JSON'
#   {"source": "kubernetes", "service": "payment-api", "environment": "sandbox",
#    "signature": {"waiting_reason": "CrashLoopBackOff", "container": "payment-api"},
#    "raw": {"pod": "payment-api-7c886558c8-f29pk", "namespace": "payment-api"}}
#   JSON
set -euo pipefail

usage() {
  echo "usage: $0 <kubernetes|jenkins> [signal.json|-]" >&2
  exit 1
}

SOURCE="${1:-}"
FILE="${2:--}"
case "${SOURCE}" in
  kubernetes | jenkins) ;;
  *) usage ;;
esac

# Default to wherever `helix serve` binds (HELIX_API_BIND in .env), so a
# Linux box bound to the docker0 bridge does not need HELIX_API_URL set.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIND="$(grep -E '^HELIX_API_BIND=' "${SCRIPT_DIR}/../.env" 2>/dev/null | cut -d= -f2- || true)"
API_URL="${HELIX_API_URL:-http://${BIND:-127.0.0.1}:8787}"

if [[ "${FILE}" == "-" ]]; then
  PAYLOAD="$(cat)"
else
  [[ -f "${FILE}" ]] || { echo "emit-signal: no such file: ${FILE}" >&2; exit 1; }
  PAYLOAD="$(cat "${FILE}")"
fi

AUTH_ARGS=()
if [[ -n "${HELIX_INTAKE_TOKEN:-}" ]]; then
  AUTH_ARGS+=(-H "Authorization: Bearer ${HELIX_INTAKE_TOKEN}")
fi

curl -sfS --max-time 10 -X POST "${API_URL}/intake/${SOURCE}" \
  -H 'Content-Type: application/json' \
  "${AUTH_ARGS[@]}" \
  -d "${PAYLOAD}"
echo
