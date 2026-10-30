set dotenv-load := false

# List available recipes
default:
    @just --list

# Install the virtualenv
install:
    uv sync

# Install dependencies exactly as locked (CI)
install-ci:
    uv sync --frozen

# Check lock file consistency
lock-check:
    uv lock --locked

# Lint (check-only, CI-safe)
lint:
    uv run ruff check --no-fix .
    uv run ruff format --check .

# Lint with auto-fix (local dev)
lint-fix:
    uv run ruff check --fix .
    uv run ruff format .

# Type check (src and tests only)
typecheck:
    uv run ty check src tests

# Dependency audit
deps:
    uv run deptry src

# Run tests (excludes live_frappe; those need a real Frappe site)
test:
    uv run pytest -m "not live_frappe"

# Run tests with JUnit output (CI)
test-ci:
    uv run pytest -m "not live_frappe" --junitxml=report.xml

# Full validation (local dev)
check: lock-check lint typecheck deps

# Full validation (CI)
check-ci: lock-check lint typecheck deps
