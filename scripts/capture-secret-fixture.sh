#!/usr/bin/env bash
# Capture the Secret-object evidence fixture for subphase 1.3 (D4): a real
# `kubectl get secret payment-api-secret -o yaml` payload wrapped in the
# same envelope _build_fixture.py produces for cluster faults, so a real
# API response carrying base64 data flows through normalization and
# redaction in tests. Requires the live kind sandbox - run when the
# cluster is up (scripts/up.sh), not during the fault itself.
#
# Usage: scripts/capture-secret-fixture.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
source "${SCRIPT_DIR}/common.sh"

OUT_DIR="${REPO_ROOT}/fixtures/faults/cluster"
mkdir -p "${OUT_DIR}"
OUT_FILE="${OUT_DIR}/secret-object.json"

SECRET_FILE="$(mktemp)"
trap 'rm -f "${SECRET_FILE}"' EXIT

kctl -n "${NAMESPACE}" get secret payment-api-secret -o json >"${SECRET_FILE}"

CAPTURED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

python3 - "${OUT_FILE}" "${CAPTURED_AT}" "${SECRET_FILE}" <<'PYEOF'
import json
import sys

out_file, captured_at, secret_path = sys.argv[1:4]

with open(secret_path) as f:
    secret = json.load(f)

fixture = {
    "fault": "secret-object",
    "layer": "cluster",
    "captured_at": captured_at,
    "trigger": "kubectl get secret payment-api-secret -o yaml against the healthy sandbox - the real API payload carrying base64 secret data, captured so the redaction path is proven on a real object (D4)",
    "evidence": {
        "secret_payment_api": secret,
    },
}

with open(out_file, "w") as f:
    json.dump(fixture, f, indent=2, sort_keys=False)
    f.write("\n")

print(f"wrote {out_file}")
PYEOF
