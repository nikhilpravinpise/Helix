#!/usr/bin/env bash
# Runs every fault script in sequence, resetting the sandbox to known-good
# before and after each one, and reports a pass/fail table. This is the
# harness subphase 1.11 leans on to confirm every fault in the catalogue is
# genuinely reproducible, not just present as a script.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

FAULTS=(
  dependency-failure
  config-mismatch
  oom-killed
  missing-secret
  crashloop
  failed-rollout
  bad-deployment-config
)

declare -A RESULTS

for fault in "${FAULTS[@]}"; do
  echo
  echo "===== ${fault}: reset before ====="
  "${REPO_ROOT}/scripts/reset.sh"

  echo "===== ${fault}: trigger and verify ====="
  if "${SCRIPT_DIR}/${fault}.sh"; then
    RESULTS["${fault}"]="PASS"
  else
    RESULTS["${fault}"]="FAIL"
  fi

  echo "===== ${fault}: reset after ====="
  "${REPO_ROOT}/scripts/reset.sh"
done

echo
echo "fault injection catalogue - results"
printf '%-24s %s\n' "fault" "result"
printf '%-24s %s\n' "------------------------" "------"
overall=0
for fault in "${FAULTS[@]}"; do
  printf '%-24s %s\n' "${fault}" "${RESULTS[${fault}]}"
  [[ "${RESULTS[${fault}]}" == "PASS" ]] || overall=1
done

exit "${overall}"
