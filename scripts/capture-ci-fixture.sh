#!/usr/bin/env bash
# One-off capture for the three CI-layer faults (ticket #7): test regression,
# dependency failure (unresolvable package), Docker build failure. These
# have no fault script - the ticket triggers them with a temporary
# uncommitted edit to sandbox/payment-api and a real Jenkins run against it
# (infra/jenkins/Jenkinsfile bind-mounts the live working tree, see its
# header comment). Not part of the committed scripts.
#
# Usage: capture-ci-fixture.sh <fault-name> <trigger-description>
# Caller is responsible for making and reverting the uncommitted edit around
# the call to this script.
set -euo pipefail

FAULT="${1:?usage: capture-ci-fixture.sh <fault-name> <trigger-description>}"
TRIGGER_DESC="${2:?usage: capture-ci-fixture.sh <fault-name> <trigger-description>}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
OUT_DIR="${REPO_ROOT}/fixtures/faults/ci"
mkdir -p "${OUT_DIR}"
OUT_FILE="${OUT_DIR}/${FAULT}.json"

JENKINS_URL="http://localhost:8080"
JUSER="admin"
JPASS="admin-sandbox-placeholder"
JOB="payment-api"

COOKIES="$(mktemp)"
trap 'rm -f "${COOKIES}"' EXIT

CRUMB_JSON="$(curl -s -c "${COOKIES}" -u "${JUSER}:${JPASS}" "${JENKINS_URL}/crumbIssuer/api/json")"
CRUMB_FIELD="$(python3 -c "import json,sys;print(json.load(sys.stdin)['crumbRequestField'])" <<<"${CRUMB_JSON}")"
CRUMB="$(python3 -c "import json,sys;print(json.load(sys.stdin)['crumb'])" <<<"${CRUMB_JSON}")"

NEXT_BUILD="$(curl -s -u "${JUSER}:${JPASS}" "${JENKINS_URL}/job/${JOB}/api/json" \
  | python3 -c "import json,sys;print(json.load(sys.stdin)['nextBuildNumber'])")"

echo "[capture-ci-fixture] triggering build #${NEXT_BUILD} for ${FAULT}"
curl -s -b "${COOKIES}" -u "${JUSER}:${JPASS}" -H "${CRUMB_FIELD}: ${CRUMB}" \
  -X POST "${JENKINS_URL}/job/${JOB}/build" -o /dev/null -w '%{http_code}\n'

BUILD_URL="${JENKINS_URL}/job/${JOB}/${NEXT_BUILD}"

echo "[capture-ci-fixture] waiting for build #${NEXT_BUILD} to finish"
RESULT=""
for ((i = 0; i < 60; i++)); do
  INFO="$(curl -s -u "${JUSER}:${JPASS}" "${BUILD_URL}/api/json" 2>/dev/null || true)"
  if [[ -n "${INFO}" ]]; then
    BUILDING="$(python3 -c "import json,sys;print(json.load(sys.stdin).get('building'))" <<<"${INFO}" 2>/dev/null || echo True)"
    if [[ "${BUILDING}" == "False" ]]; then
      RESULT="$(python3 -c "import json,sys;print(json.load(sys.stdin).get('result'))" <<<"${INFO}")"
      break
    fi
  fi
  sleep 5
done

if [[ -z "${RESULT}" ]]; then
  echo "[capture-ci-fixture] FAIL: build #${NEXT_BUILD} did not finish within timeout" >&2
  exit 1
fi

echo "[capture-ci-fixture] build #${NEXT_BUILD} finished: ${RESULT}"

CONSOLE_FILE="$(mktemp)"
curl -s -u "${JUSER}:${JPASS}" "${BUILD_URL}/consoleText" >"${CONSOLE_FILE}"

CAPTURED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

python3 - "${OUT_FILE}" "${FAULT}" "${TRIGGER_DESC}" "${CAPTURED_AT}" \
  "${NEXT_BUILD}" "${RESULT}" "${BUILD_URL}" "${CONSOLE_FILE}" <<'PYEOF'
import json
import sys

out_file, fault, trigger_desc, captured_at, build_number, result, build_url, console_path = sys.argv[1:9]

with open(console_path) as f:
    console = f.read()

fixture = {
    "fault": fault,
    "layer": "ci",
    "captured_at": captured_at,
    "trigger": trigger_desc,
    "jenkins_job": "payment-api",
    "jenkins_build_number": int(build_number),
    "jenkins_build_url": build_url,
    "jenkins_result": result,
    "evidence": {
        "console_output": console,
    },
}

with open(out_file, "w") as f:
    json.dump(fixture, f, indent=2)
    f.write("\n")

print(f"wrote {out_file}")
PYEOF

rm -f "${CONSOLE_FILE}"
