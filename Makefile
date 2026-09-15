.PHONY: lint test test-integration test-all serve db-up db-down db-migrate benchmark

lint:
	uv run ruff check .
	uv run ruff format --check .

test:
	uv run pytest

test-integration:
	uv run pytest -m integration

test-all:
	uv run pytest -m "integration or not integration"

serve:
	uv run helix serve

db-up:
	bash scripts/db-up.sh

db-down:
	bash scripts/db-down.sh

db-migrate:
	uv run alembic -c state/migrations/alembic.ini upgrade head

# Subphase 1.11 controlled benchmark. Fixture scenarios run on arm A by
# default; BENCH_ARMS/BENCH_SCENARIOS override. Reports land in
# benchmark/reports/.
benchmark:
	bash scripts/benchmark.sh $(BENCH_ARGS)
