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

# Run tests (excludes live_frappe, which needs a real Frappe site); extra args go to pytest
test *args:
    uv run pytest -m "not live_frappe" {{args}}

# Full static validation: lock, lint, typecheck, deps
check: lock-check lint typecheck deps
