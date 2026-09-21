.PHONY: lint test test-integration test-all serve db-up db-down db-migrate kafka-up kafka-down benchmark benchmark-tolerance

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

# Subphase E2 Kafka backbone. Brings up the broker and declares its
# topics; not needed for any Phase 1 workflow.
kafka-up:
	bash scripts/kafka-up.sh

kafka-down:
	bash scripts/kafka-down.sh

# Subphase 1.11 controlled benchmark. Fixture scenarios run on arm A by
# default; BENCH_ARMS/BENCH_SCENARIOS override. Reports land in
# benchmark/reports/.
benchmark:
	bash scripts/benchmark.sh $(BENCH_ARGS)

# The 1.11 exit criterion's second half: three consecutive runs inside the
# documented bands (docs/benchmark/tolerance.md). Pass the reports
# oldest-first; non-zero exit on a gated breach.
#   make benchmark-tolerance REPORTS="r1.json r2.json r3.json"
REPORTS ?= benchmark/reports/latest.json
benchmark-tolerance:
	uv run python -m benchmark.tolerance $(REPORTS)
