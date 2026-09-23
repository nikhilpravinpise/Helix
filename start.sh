#!/usr/bin/env bash
# Bring up everything the demo needs, and optionally run it.
#
#   ./start.sh          bring up Postgres + the operator UI, print the URLs
#   ./start.sh --run    the above, then drive one incident to a real PR
#   ./start.sh --stop   stop the UI and Postgres (volumes persist)
#
# Idempotent: safe to run twice. See DEMO.md for what to say over it.

set -euo pipefail
cd "$(dirname "$0")"

PORT="${HELIX_API_PORT:-8787}"
UI="http://127.0.0.1:${PORT}/ui/"
PIDFILE=".helix-serve.pid"
SIGNAL=".helix-demo-signal.json"

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }

stop() {
    if [[ -f "$PIDFILE" ]]; then
        kill "$(cat "$PIDFILE")" 2>/dev/null || true
        rm -f "$PIDFILE"
        echo "operator UI stopped"
    fi
    make db-down
    exit 0
}

[[ "${1:-}" == "--stop" ]] && stop

# --- 1. Postgres -------------------------------------------------------------
say "1/3  Postgres"
if docker ps --format '{{.Names}}' | grep -qx helix-postgres; then
    echo "already up"
else
    make db-up
fi
make db-migrate >/dev/null 2>&1
echo "schema current"

# --- 2. Operator UI ----------------------------------------------------------
say "2/3  Operator UI"
if curl -fsS -o /dev/null "http://127.0.0.1:${PORT}/incidents" 2>/dev/null; then
    echo "already serving"
else
    rm -f "$PIDFILE"
    uv run helix serve >.helix-serve.log 2>&1 &
    echo $! >"$PIDFILE"
    for _ in $(seq 20); do
        curl -fsS -o /dev/null "http://127.0.0.1:${PORT}/incidents" 2>/dev/null && break
        sleep 0.5
    done
    curl -fsS -o /dev/null "http://127.0.0.1:${PORT}/incidents" 2>/dev/null \
        || { echo "UI failed to start, see .helix-serve.log"; exit 1; }
    echo "started (pid $(cat "$PIDFILE"), log .helix-serve.log)"
fi

# --- 3. Ready ----------------------------------------------------------------
say "3/3  Ready"
REPO="$(grep -E '^HELIX_GITHUB_REPO=' .env | cut -d= -f2- || echo '?')"
TOKEN_SET=$(grep -qE '^HELIX_GITHUB_TOKEN=.+' .env && echo yes || echo NO)

echo "  operator UI : ${UI}"
echo "  pull requests: https://github.com/${REPO}/pulls"
echo "  github token : ${TOKEN_SET}"

if [[ "$TOKEN_SET" == "NO" ]]; then
    echo
    echo "  HELIX_GITHUB_TOKEN is empty, so runs will record writes instead of"
    echo "  opening real PRs. Everything else works."
fi

if [[ "${1:-}" != "--run" ]]; then
    echo
    echo "Run the demo incident with:  ./start.sh --run"
    exit 0
fi

# --- 4. Drive one incident ---------------------------------------------------
say "Running one incident"
uv run python -c "
import yaml, json
scenario = yaml.safe_load(open('benchmark/scenarios/ci-test-regression.yaml'))
json.dump(scenario['signal'], open('$SIGNAL', 'w'), indent=1)
"
echo "signal:"
sed 's/^/  /' "$SIGNAL"
echo

# Arm A for live PR demos: it is deterministic, and the LLM arms have
# produced fixes against paths that do not exist. HELIX_EVIDENCE_SOURCE
# gates writes as well as reads, so live is what makes the PR real.
HELIX_ARM=A HELIX_EVIDENCE_SOURCE=live uv run helix intake --file "$SIGNAL" --run

say "Result"
uv run python - <<'PY'
from sqlalchemy import create_engine, text
from helix.config import get_settings

engine = create_engine(get_settings().database_url)
with engine.connect() as conn:
    row = conn.execute(
        text("select action, result, origin, output from executions order by id desc limit 1")
    ).first()

if row is None:
    print("  no execution recorded")
else:
    print(f"  {row.action}: {row.result} (origin {row.origin})")
    url = (row.output or {}).get("pr_url")
    print(f"  {url}" if url else "  no PR: writes were recorded, not live")
PY
echo
echo "Evidence chain: ${UI}"
