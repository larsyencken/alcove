.PHONY: help unittest format lint typecheck test test-strict

# Default target
help:
	@echo "Available targets:"
	@echo "  make unittest       - Run unittests with pytest"
	@echo "  make format         - Reformat using ruff"
	@echo "  make lint           - Lint using ruff"
	@echo "  make typecheck      - Typecheck with pyright"
	@echo "  make test           - Run lint, typecheck, and unittest sequentially"
	@echo "  make test-strict    - Run tests requiring MinIO container (will fail if not available)"

# Check if .venv exists and is up to date
.venv: pyproject.toml
	@echo "==> Installing packages"
	@uv sync
	@touch $@

# Run unittests with pytest
unittest: .venv
	@echo "==> Running unit tests"
	@uv run pytest --sw

# Reformat using rye
format: .venv
	@echo "==> Formatting all files"
	@uv run ruff format
	@uv run ruff check --fix

# Lint using rye
lint: .venv
	@echo "==> Linting all files"
	@uv run ruff check

# Typecheck with pyright
typecheck: .venv
	@echo "==> Typechecking"
	@uv run pyright

# Run lint, typecheck, and unittest sequentially
test: lint typecheck unittest

# Run tests in strict mode - MinIO container is required
test-strict: lint typecheck
	@echo "==> Detecting Docker context"
	$(eval DOCKER_HOST := $(shell docker context inspect --format '{{.Endpoints.docker.Host}}'))
	@echo "Using Docker context: $(DOCKER_HOST)"
	@echo "==> Running unit tests with strict MinIO requirements"
	@DOCKER_HOST=$(DOCKER_HOST) REQUIRE_MINIO=1 uv run pytest --sw

clean:
	rm -rf data/* metadata/*
