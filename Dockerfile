# Helix API + dashboard, for a hosted demo (render.yaml). Runs on recorded
# evidence with the rules arm: no cluster, Kafka, Jenkins or LLM key here.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never

# Dependencies first, so a code change does not reinstall them.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY . .
RUN uv sync --frozen --no-dev && useradd --create-home helix && chown -R helix /app
USER helix

# No .env is ever read in the container; settings come from the host.
ENV HELIX_ENV_FILE="" PATH="/app/.venv/bin:$PATH"
CMD ["bash", "scripts/render-start.sh"]
