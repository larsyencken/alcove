.PHONY: help unittest format lint typecheck test ci build publish docs docs-build

# Default target
help:
	@echo "Available targets:"
	@echo "  make unittest       - Run unittests with pytest"
	@echo "  make format         - Reformat using ruff"
	@echo "  make lint           - Lint using ruff"
	@echo "  make typecheck      - Typecheck with pyright"
	@echo "  make test           - Lint, typecheck and run unittests"
	@echo "  make ci             - Run the CI workflow locally with act"
	@echo "  make build          - Build package for distribution"
	@echo "  make publish        - Publish package to PyPI"
	@echo "  make docs           - Serve docs locally"
	@echo "  make docs-build     - Build docs with strict checking"

# Reformat using ruff
format:
	@echo "==> Formatting all files"
	@uv run ruff format
	@uv run ruff check --fix

# Lint using ruff
lint:
	@echo "==> Linting all files"
	@uv run ruff check

# Typecheck with pyright
typecheck:
	@echo "==> Typechecking"
	@uv run pyright

# Run all checks
test: lint typecheck unittest

# Run unittests with Docker-based MinIO
unittest:
	@echo "==> Detecting Docker context"
	$(eval DOCKER_HOST := $(shell docker context inspect --format '{{.Endpoints.docker.Host}}'))
	@echo "Using Docker context: $(DOCKER_HOST)"
	@echo "==> Running tests with Docker MinIO"
	@DOCKER_HOST=$(DOCKER_HOST) uv run pytest --sw

clean:
	rm -rf data/* metadata/*

# Run the CI workflow locally with act, one matrix entry at a time, since each
# binds minio to port 9000 (act's --concurrent-jobs doesn't serialise a
# matrix); --pull=false uses cached images when present
CI_DEPS := $(shell sed -n 's/^ *- deps: *\([a-z_-]*\).*/\1/p' .github/workflows/ci.yml)
ci:
	@if [ -z "$(CI_DEPS)" ]; then echo "==> No '- deps:' matrix entries found in ci.yml"; exit 1; fi
	@echo "==> Detecting Docker context"
	$(eval DOCKER_HOST := $(shell docker context inspect --format '{{.Endpoints.docker.Host}}'))
	@echo "Using Docker context: $(DOCKER_HOST)"
	@failed=""; for deps in $(CI_DEPS); do \
		echo "==> Running CI workflow locally with act ($$deps deps)"; \
		DOCKER_HOST=$(DOCKER_HOST) act -W .github/workflows/ci.yml --matrix deps:$$deps --rm --pull=false \
			|| failed="$$failed $$deps"; \
	done; \
	if [ -n "$$failed" ]; then echo "==> CI failed for:$$failed deps"; exit 1; fi; \
	echo "==> CI passed for: $(CI_DEPS)"

build:
	@echo "==> Building package"
	@rm -rf dist
	@uv build
	@uv run twine check dist/*

publish: build
	@echo "==> Publishing to PyPI"
	@uv run twine upload dist/*

docs:
	@echo "==> Serving docs locally"
	@uv run zensical serve

docs-build:
	@echo "==> Building docs"
	@uv run zensical build --clean
