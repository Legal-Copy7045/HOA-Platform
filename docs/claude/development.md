# Development: tests, commands, CI and releases

Moved from CLAUDE.md when it was cut to its essentials (D90): the same rules and reference, unchanged, read when a task touches this area. A rule here binds exactly as one in CLAUDE.md, which stays the source of truth (if a task conflicts with either, stop and ask).

## Testing and quality

- Full regression (`/regression`, `regression/README.md`): every few features or when the owner asks. Every run includes a full security audit (claims and whole codebase, plus the new code and the deployment surface). Every feature change keeps it current: tests, a `regression/catalogue/*.yaml` entry whose `match` finds them, `regression/manual.yaml` for anything only a person can check, and new IDs in `regression/ids/` committed. A regression run fixes nothing in product code; findings go to the report.

- Every change includes tests. Authorization, ownership-transfer, recusal, and audit behaviors are mandatory test targets.
- CI must pass: ruff, tests, bandit, pip-audit.
- Prefer small, reviewable commits with clear messages. Do not commit generated secrets.
- Add a migration for every model change; never edit applied migrations.

- Agreed work not yet scheduled: `docs/backlog.md` (add to it when a review or the owner agrees something for later; remove a line when it ships).

## Commands

(Fill in as they are created; keep this section accurate.)

- First time: `cp .env.example .env`
- Run locally: `docker compose up` (site http://localhost:8000, mail http://localhost:8025)
- Every check before a pull request is marked ready (the owner's standard, D90): `python3 regression/run.py check` (ci and unit stages, quiet; `scripts/check.sh` runs it; it used to run the checks itself, stopping at the first failure)
- CI security gates (D57), each its own job in `ci.yml` beside the familiar `test` line, all needed by `ci-result` (D90): gitleaks through the standard's reusable `gitleaks.yml` (it was `secrets.yml`; `.gitleaks.toml` allows only marked throwaway values and exact fixtures), the privacy guard (`.github/scripts/privacy_guard.py`, the private terms on push to main), `image-scan` (Trivy, fails on fixable HIGH/CRITICAL in the portal image; compose images report only), `codeql.yml` (weekly, reusable; actions, Python, JavaScript), `pages` (the regression page crawl). Actions and reusable workflows pinned by commit with a `# vX` comment
- Economical CI (D86, Actions minutes are limited; kept by the standard, D90): pull requests only for tests (never on drafts; open as a draft, mark ready once `python3 regression/run.py check` passes), no re-test on merge to main (a push to main runs only the private-terms check and, when `VERSION` has no tag, the release), a full weekly run on main (Sundays 03:17 UTC) and `workflow_dispatch`; `changes` (`.github/changes.yaml`) decides per pull request whether the tests, `image-scan` (the one image build), `pages` and the dependency audit run; two test shards; CodeQL weekly only; Dependabot weekly on Sunday, grouped minor/patch, merging itself when `ci-result` passes. `ci-result` is the only required check. Batch features into few releases, one pull request each, pushed once (`core/tests/test_image_pins.py::test_ci_is_economical`)
- Full regression (stages, stable IDs, report): `python3 regression/run.py` then `python3 regression/run.py report` (runs `python3 regression/report.py`; the global `/regression` skill does the whole run, `regression/profile.yaml`)
- Walkthrough: /walkthrough; regression and walkthrough together: /full-check (`python3 regression/run.py --stage walkthrough` or `--with-walkthrough`, report `regression/walkthrough_report.py`)
- Tests (needs Postgres with the two roles from `docker/postgres/init-roles.sh`): `pytest` (CI runs two shards in parallel with `pytest --shard K/2`, conftest.py; a summary job keeps the single `test` check; locally use four with separate databases)
- Lint: `ruff check . && ruff format --check .`
- Security: `bandit -q -c pyproject.toml -r .` and `pip-audit --strict -r requirements/base.txt -r requirements/dev.txt`
- Update dependencies: edit `requirements/*.in`, then `pip-compile --generate-hashes --allow-unsafe --strip-extras -o requirements/base.txt requirements/base.in` (and the same for dev)
- Releases (D37, D90): `VERSION` holds MAJOR.MINOR.PATCH (`settings.APP_RELEASE`; `APP_VERSION` stays the commit, shown as the build); every merge to `main` bumps it (MINOR for features or changes people notice, PATCH for fixes only; 1.0.0 is the go-live with real owners) and adds a `CHANGELOG.md` entry, newest first (a test checks both; `.github/scripts/check_versions.py` in CI); the merged bump is the release: CI tags `v<VERSION>` and creates the GitHub Release from that section, and nothing deploys from an unreleased commit; System status shows the release and the newer code's number
- Regression logs keep up with releases (D91, `regression/release_logs.py`, run by `python3 regression/run.py check` as CI-014 and by `regression/tests/test_release_logs.py` in the suite): a MINOR or MAJOR release (`VERSION` X.Y.0) fails unless the newest row of the standard table in both `regression/RUNS.md` and `regression/WALKTHROUGHS.md` is for the previous release line (the newest MAJOR.MINOR in `CHANGELOG.md` below X.Y) or newer, so the logs are never more than one release line behind. Before bumping to the next minor, run `/full-check` on the current line and `python3 regression/run.py log --full-check`. Patch releases and the first release line are exempt
