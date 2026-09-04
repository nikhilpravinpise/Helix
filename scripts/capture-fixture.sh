#!/usr/bin/env bash
# One-off capture tool for subphase 1.2 ticket #7 - not part of the
# committed fault-injection or bring-up/teardown scripts, and not consumed
# by run-all.sh or reset.sh. Runs one cluster-layer fault for real, against
# the live kind sandbox and the live Elasticsearch, and writes exactly what
# came back to fixtures/faults/cluster/<fault>.json. Nothing in the captured
# evidence is synthesised, trimmed, or redacted - that is 1.3's job, against
# these fixtures as ground truth.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
source "${SCRIPT_DIR}/common.sh"

FAULT="${1:?usage: capture-fixture.sh <fault-name> <trigger-description>}"
TRIGGER_DESC="${2:?usage: capture-fixture.sh <fault-name> <trigger-description>}"

OUT_DIR="${REPO_ROOT}/fixtures/faults/cluster"
mkdir -p "${OUT_DIR}"
OUT_FILE="${OUT_DIR}/${FAULT}.json"

echo "===== ${FAULT}: reset before ====="
"${SCRIPT_DIR}/reset.sh"

echo "===== ${FAULT}: trigger ====="
FAULT_LOG="$(mktemp)"
set +e
"${SCRIPT_DIR}/faults/${FAULT}.sh" >"${FAULT_LOG}" 2>&1
FAULT_EXIT=$?
set -e
cat "${FAULT_LOG}"

PODS_PA_FILE="$(mktemp)"
PODS_DS_FILE="$(mktemp)"
EVENTS_FILE="$(mktemp)"
LOGS_PA_FILE="$(mktemp)"
LOGS_DS_FILE="$(mktemp)"

kctl -n "${NAMESPACE}" get pods -l app=payment-api -o json >"${PODS_PA_FILE}"
kctl -n "${NAMESPACE}" get pods -l app=downstream-stub -o json >"${PODS_DS_FILE}"
kctl -n "${NAMESPACE}" get events -o json >"${EVENTS_FILE}"

# Filebeat's default scan_frequency is 10s and Elasticsearch still needs to
# index the bulk request after that - querying immediately after the fault
# script returns can race ahead of the shipper and miss the very log line
# (e.g. crashloop's own startup-validation failure) that just fired. Give
# the pipeline time to catch up before querying, rather than querying twice
# and silently picking whichever attempt looks better.
echo "[capture-fixture] waiting 20s for filebeat to ship the fault's own log lines"
sleep 20

CAPTURED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# A short window, not a generous one: query-logs.sh caps at 200 hits and
# sorts oldest-first, so a wide window that also catches the readiness/
# liveness probe chatter from earlier in the run silently truncates away the
# newest lines - which are exactly the fault's own evidence. Found this the
# hard way capturing crashloop: a 10m window had 275 matching payment-api
# docs and cut off the pod's own startup-validation failure. 3m keeps the
# probe-chatter volume for one pod well under the 200-hit cap.
"${SCRIPT_DIR}/query-logs.sh" payment-api 3m >"${LOGS_PA_FILE}" 2>&1 || true
"${SCRIPT_DIR}/query-logs.sh" downstream-stub 3m >"${LOGS_DS_FILE}" 2>&1 || true

python3 "${SCRIPT_DIR}/_build_fixture.py" \
  "${OUT_FILE}" "${FAULT}" "${TRIGGER_DESC}" "${CAPTURED_AT}" \
  "faults/${FAULT}.sh" "${FAULT_EXIT}" "${FAULT_LOG}" \
  "${PODS_PA_FILE}" "${PODS_DS_FILE}" "${EVENTS_FILE}" \
  "${LOGS_PA_FILE}" "${LOGS_DS_FILE}"

rm -f "${FAULT_LOG}" "${PODS_PA_FILE}" "${PODS_DS_FILE}" "${EVENTS_FILE}" "${LOGS_PA_FILE}" "${LOGS_DS_FILE}"

echo "===== ${FAULT}: reset after ====="
"${SCRIPT_DIR}/reset.sh"

echo "===== ${FAULT}: fixture written to ${OUT_FILE} ====="
if [[ "${FAULT_EXIT}" -ne 0 ]]; then
  echo "WARNING: fault script exited ${FAULT_EXIT} (non-zero) - fixture still captured as-is" >&2
fi
