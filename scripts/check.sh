#!/usr/bin/env bash
# Run every check CI runs, stopping at the first failure.
set -euo pipefail
echo "== ruff";        ruff check . && ruff format --check .
echo "== bandit";      bandit -q -c pyproject.toml -r .
echo "== migrations";  python manage.py makemigrations --check --dry-run --settings=core.settings.test
echo "== pytest";      pytest -q
echo "== all checks passed"
