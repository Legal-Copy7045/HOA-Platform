#!/usr/bin/env bash
# Run every check CI runs, stopping at the first failure.
# One command per line: under `set -e`, a failure inside an `a && b` chain
# does not stop the script, so chains must not be used here.
set -euo pipefail
echo "== ruff check"
ruff check .
echo "== ruff format"
ruff format --check .
echo "== bandit"
bandit -q -c pyproject.toml -r .
echo "== migrations"
python manage.py makemigrations --check --dry-run --settings=core.settings.test
echo "== pytest"
pytest -q
echo "== all checks passed"
