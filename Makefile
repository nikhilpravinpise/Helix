.PHONY: lint test test-integration test-all

lint:
	uv run ruff check .
	uv run ruff format --check .

test:
	uv run pytest

test-integration:
	uv run pytest -m integration

test-all:
	uv run pytest -m "integration or not integration"
