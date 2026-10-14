#!/usr/bin/env bash
# Start Helix on a hosting platform (Dockerfile, render.yaml): migrate the
# database, then serve the API and dashboard on the platform's port.
set -euo pipefail

# Hosts hand out postgres:// or postgresql:// URLs; SQLAlchemy needs the
# driver named (Helix uses psycopg 3).
url="${HELIX_DATABASE_URL:-${DATABASE_URL:-}}"
if [[ -z "${url}" ]]; then
  echo "render-start: HELIX_DATABASE_URL (or DATABASE_URL) is not set" >&2
  exit 1
fi
url="${url/#postgres:\/\//postgresql+psycopg://}"
url="${url/#postgresql:\/\//postgresql+psycopg://}"
export HELIX_DATABASE_URL="${url}"

if [[ -z "${HELIX_UI_PASSWORD:-}" ]]; then
  echo "render-start: refusing to serve a public URL without HELIX_UI_PASSWORD" >&2
  exit 1
fi

alembic -c state/migrations/alembic.ini upgrade head
export HELIX_API_BIND=0.0.0.0 HELIX_API_PORT="${PORT:-8787}"
exec helix serve
