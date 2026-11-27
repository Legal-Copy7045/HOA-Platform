#!/usr/bin/env bash
# Every check to run before a pull request is marked ready (the owner's
# standard, D90): the regression suite's ci and unit stages, quietly - ruff
# (lint and format), bandit, the migration check, pip-audit, check --deploy,
# versions, the privacy guard, repository hygiene, and the whole pytest suite
# (three lanes with their own databases). One line per stage and the failing
# checks; full output in regression/out/logs/. Needs Postgres with the two
# roles (docker/postgres/init-roles.sh); REGRESSION_DATABASE_URL moves the
# test databases when another checkout runs at the same time.
set -euo pipefail
cd "$(dirname "$0")/.."
exec python3 regression/run.py check
