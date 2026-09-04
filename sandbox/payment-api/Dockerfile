# Sandbox demo service - deliberately broken later to exercise Helix's
# Docker-build-failure fault. Keep this simple and easy to read.

FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.7.13 /uv /uvx /bin/

WORKDIR /app

COPY pyproject.toml uv.lock* ./
RUN uv sync --no-dev --no-install-project

COPY payment_api ./payment_api
RUN uv sync --no-dev


FROM python:3.12-slim AS runtime

RUN useradd --create-home --shell /usr/sbin/nologin appuser

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/payment_api /app/payment_api

ENV PATH="/app/.venv/bin:$PATH"

USER appuser

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=2)" || exit 1

EXPOSE 8000

CMD ["uvicorn", "payment_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
