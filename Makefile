.PHONY: help unittest format lint typecheck test act build publish

# Default target
help:
	@echo "Available targets:"
	@echo "  make unittest       - Run unittests with pytest"
	@echo "  make format         - Reformat using ruff"
	@echo "  make lint           - Lint using ruff"
	@echo "  make typecheck      - Typecheck with pyright"
	@echo "  make test           - Run all tests with Docker-based MinIO"
	@echo "  make act            - Run GitHub Actions locally with act"
	@echo "  make build          - Build package for distribution"
	@echo "  make publish        - Publish package to PyPI"

# Check if .venv exists and is up to date
.venv: pyproject.toml
	@echo "==> Installing packages"
	@uv sync
	@touch $@

# Reformat using ruff
format: .venv
	@echo "==> Formatting all files"
	@uv run ruff format
	@uv run ruff check --fix

# Lint using ruff
lint: .venv
	@echo "==> Linting all files"
	@uv run ruff check

# Typecheck with pyright
typecheck: .venv
	@echo "==> Typechecking"
	@uv run pyright

# Run all tests with Docker-based MinIO
test: lint typecheck
	@echo "==> Detecting Docker context"
	$(eval DOCKER_HOST := $(shell docker context inspect --format '{{.Endpoints.docker.Host}}'))
	@echo "Using Docker context: $(DOCKER_HOST)"
	@echo "==> Running tests with Docker MinIO"
	@DOCKER_HOST=$(DOCKER_HOST) uv run pytest --sw

clean:
	rm -rf data/* metadata/*

act:
	@echo "==> Detecting Docker context"
	$(eval DOCKER_HOST := $(shell docker context inspect --format '{{.Endpoints.docker.Host}}'))
	@echo "Using Docker context: $(DOCKER_HOST)"
	@echo "==> Running GitHub Actions workflow locally with act"
	@DOCKER_HOST=$(DOCKER_HOST) act

build:
	@echo "==> Building package"
	@rm -rf dist
	@uv build
	@uv run twine check dist/*

publish: build
	@echo "==> Publishing to PyPI"
	@uv run twine upload dist/*
