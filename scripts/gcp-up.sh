#!/usr/bin/env bash
# GCP/GKE target - STUB. Refuses to run (D23, infra/gcp/README.md).
#
# Helix runs on kind. DJS23OLOE501 requires a cloud deployment, which is
# deferred rather than dropped, and this script exists so that target has
# a named entry point instead of living only in someone's memory - the
# original GCP question was raised in 1.2 and went unanswered through two
# whole phases because nothing in the repo pointed at it.
#
# It fails closed on purpose. A stub that half-creates cloud resources is
# worse than one that creates none: it bills, and it leaves state nobody
# knows about. `scripts/jenkins-up.sh` fails the same way when the kind
# network is missing, rather than bringing up something broken.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

cat >&2 <<'EOF'
gcp-up: not implemented - this is a stub (decision D23).

Helix currently targets the local kind sandbox. The GCP path is scoped in
infra/gcp/README.md, including the full kind-to-GKE delta and the two
things worth getting right (pin the cluster version; keep the benchmark
local).

Prerequisites, none of which can be automated from here:

  1. Install the gcloud SDK
  2. gcloud auth login
     gcloud auth application-default login      (both interactive)
  3. Create or name a project and attach billing
  4. Choose a region and zone

Until then:

  bash scripts/up.sh          # the kind sandbox, free and reproducible
EOF

# Report what is actually present, so the gap is a fact rather than a
# claim this script makes about the machine.
echo >&2
if command -v gcloud >/dev/null 2>&1; then
  echo "note: gcloud IS installed ($(gcloud --version 2>/dev/null | head -1))." >&2
  echo "      The blocker is this script, not the toolchain." >&2
else
  echo "note: gcloud is not installed on this machine." >&2
fi
echo "      See ${REPO_ROOT}/infra/gcp/README.md" >&2

exit 2
