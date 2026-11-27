# Regression suite

One command runs every check in the repo, gives each one a stable ID, maps them
to the portal's features and produces one report. Run it after a big feature
set, before showing the board something new, or when asked (`/regression` in a
Claude Code session runs the whole thing, reviews included). The independent
walkthrough of every role's menus and pages on a desktop and a phone is a
separate stage with its own report (`/walkthrough`); `/full-check` runs
regression and walkthrough together as one command (see "Walkthrough" below).
CI already runs lint and tests on every ready pull request; this is the full
health check on top.

The suite follows the owner's project standard (`~/.claude/standards/STANDARD.md`
section 3, decision D90): the global skills `/regression`, `/walkthrough` and
`/full-check` (in `~/.claude/skills/`, not in this repository) read
`profile.yaml` and this file, and run the standard commands below. This
project keeps its own, richer stage runners, IDs, catalogue and reports (listed
under `beyond:` in `.github/standard.yaml`); `standard.py` is vendored
unchanged from the standards repository for the briefs, summary, manual log and
run rows.

## Standard commands

```bash
python3 regression/run.py check                  # ci + unit, quiet: before a PR is marked ready (scripts/check.sh)
python3 regression/run.py                        # ci, unit, page, manual (not live, not walkthrough)
python3 regression/run.py --with-walkthrough     # every stage and the walkthrough, side by side
python3 regression/run.py --stage unit           # one stage (repeatable)
python3 regression/run.py brief [--full]         # reviewer briefs -> out/briefs/{bugs,claims,exposure}.md
python3 regression/run.py brief --walkthrough    # -> out/briefs/{desktop,phone}.md
python3 regression/run.py report [--walkthrough] # report.py / walkthrough_report.py (REPORT_TZ from profile.yaml)
python3 regression/run.py log [--walkthrough | --full-check]   # the RUNS.md / WALKTHROUGHS.md row
python3 regression/run.py summary                # <= 40 lines for the owner and for Claude
python3 regression/run.py manual KEY pass|fail --note "..."    # manual-log.yaml, for this VERSION
```

Each stage prints one line (counts) and its failing checks; full output goes to
`out/logs/`. Read the summary, and a log only when a failure needs it.

## This project in a /regression, /walkthrough or /full-check run

- **Databases and ports** (Postgres with the two roles from
  `docker/postgres/init-roles.sh`; the owner role creates databases): unit
  lanes `hoa_reg0..2` (from `REGRESSION_DATABASE_URL`, default
  `postgres://hoa_owner:hoa_owner@localhost:5432/hoa`, plus `_reg<lane>`); page
  stage `hoa_reg_pages` on port 8011 (`REGRESSION_PAGES_DB`,
  `REGRESSION_PAGE_PORT`); walkthrough `hoa_reg_walk` on port 8012
  (`REGRESSION_WALK_DB`, `REGRESSION_WALK_PORT`). Move them when another
  checkout runs at the same time. A `<PREFIX>-RUN` or `setup:` failure means a
  stage broke (Postgres down, a port in use, Node missing): fix the environment
  and re-run that stage with `--stage`.
- **Live**: `LIVE_HOST=<POC hostname from docs/runbook.md> python3
  regression/run.py --stage live`, beside the rest (read-only, quick). If the
  host or the Cloudflare API is not reachable from the session its checks skip;
  say so.
- **Suite up to date first**: read `git log` since the commit of the newest
  `RUNS.md` row (and `docs/milestone-*-summary.md`); every new feature, page,
  workflow, rule or setting gets a `catalogue/<area>.yaml` entry whose `match`
  finds an automatic check (write missing tests: tests only, no product code);
  new pages also go in `core/tests/test_url_matrix.py`; anything only a person
  can check (a real phone, a real mailbox, the VM) goes in `manual.yaml` with
  steps; new security claims go in `claims.yaml`. Commit new IDs in `ids/`.
- **The five reviewers** (fresh, read-only, briefed only by `run.py brief`):
  `bugs`, `claims` (the security audit of every claim in `claims.yaml` and the
  whole codebase on a full audit, with bandit and pip-audit) and `exposure`
  (new code and the deployment surface) for the regression; `desktop` and
  `phone` (model sonnet) for the walkthrough. Their focus for this project is in
  `profile.yaml` (`reviewers:`). Every run includes the security audit (the
  owner's standing instruction, 2026-11-25). Findings go to
  `out/findings/<reviewer>.json` (`python3 regression/standard.py schema
  findings|walkthrough`); `report.py` and `walkthrough_report.py` read them.
  Severity follows `docs/security-review-2026-11.md`.
- **Notes**: `out/notes.yaml` (or `notes.json`) with `summary` (two or three
  plain sentences) and `observations` (coverage gaps, flaky checks, fragile
  passes, manual checks overdue) goes into the report.
- **Reports**: private artifacts, republished every run to the URLs in
  `published.yaml` (regression report, first published with icon `check`;
  walkthrough report, icon `compass`). If `walkthrough.html` is over 8 MB, run
  `walkthrough_report.py --no-thumbs`.
- **Logs**: `run.py log` adds the row to `RUNS.md` / `WALKTHROUGHS.md`
  (`--full-check` writes both, each naming the other report's link). IDs and
  counts only: no finding details, real names, hosts or addresses.
- **Fix nothing in product code** during a run: commit only the suite records
  (catalogue, ids, claims, manual, new tests, RUNS.md, WALKTHROUGHS.md,
  published.yaml) and tell the owner the totals, the failures, the findings by
  severity (claims not held) and the report links. The owner decides what is
  fixed, and is asked before any finding is fixed.

```
regression/
  run.py            runs the stages -> out/<stage>.json (and out/logs/); the standard commands
  standard.py       the standard's shared tooling (vendored, never edited here)
  profile.yaml      the project's profile for the global skills (STANDARD.md 3.1)
  claims.yaml       every security and privacy claim, for the claims reviewer
  published.yaml    the report URLs each run republishes to
  manual-log.yaml   manual results per version (run.py manual; created on first use)
  schemas/          the reviewers' JSON schemas (from the standards repository)
  report.py         out/*.json + catalogue + findings -> out/report.html and report.md
  catalogue.py      reads catalogue/*.yaml
  catalogue/*.yaml  every feature and the checks that cover it
  ids/<stage>.yaml  stable check IDs (append-only, committed)
  manual.yaml       checks that need a person (phone, mailbox, the VM)
  pages.js          the browser crawl used by the page stage
  page_settings.py  settings for the page stage's throwaway demo database
  page_setup.py     resets that database and signs in one user per role
  walkthrough.js    the walkthrough stage's browser walk (every role, desktop and phone)
  walkthrough_report.py  out/walkthrough*.json -> out/walkthrough.html
  RUNS.md           one entry per full run (no names, hosts or addresses)
  WALKTHROUGHS.md   one entry per walkthrough (same rules)
  tests/            tests of this tooling (part of the unit stage)
  out/              results, logs, report (git-ignored)
```

## Running it

```bash
python3 regression/run.py                                   # ci, unit, page, manual
LIVE_HOST=<the POC hostname> python3 regression/run.py --stage live
REPORT_TZ=America/New_York python3 regression/report.py

python3 regression/run.py --stage walkthrough               # the walkthrough alone
python3 regression/run.py --with-walkthrough                # regression + walkthrough in parallel
REPORT_TZ=America/New_York python3 regression/walkthrough_report.py
```

Needs the project venv (`.venv`), Postgres with the two roles from
`docker/postgres/init-roles.sh` (the owner role must be able to create
databases), and Node with Playwright and Chromium for the page stage
(preinstalled in cloud sessions). `--stage NAME` (repeatable) runs some stages.

Stages run side by side in three lanes: ci, unit (itself three pytest lanes, the
cross-owner URL matrix in a lane of its own, each with its own test database)
and page (plus the walkthrough as a fourth lane with `--with-walkthrough`).
Live runs last and only when asked. A run takes about 4-6 minutes on four
cores.

| Stage | Prefix | What runs |
|---|---|---|
| ci | `CI-` | ruff lint and format, bandit, migration check, pip-audit, `check --deploy` with production settings, repo hygiene (no `.env` tracked, CI actions pinned, permissions, no `pull_request_target`) |
| unit | `UNIT-` | the whole pytest suite; one result per test |
| page | `PAGE-` | seeds the fictional demo community into a fresh database, then crawls the portal in Chromium as board, owner, manager, auditor, tenant and former owner: every page type loads, has no console or CSP errors, fits a phone; every board page the owner did not reach refuses the owner |
| live | `LIVE-` | read-only: the POC answers over HTTPS behind Cloudflare Access, plain HTTP does not serve it, DNS is proxied, TLS 1.2+, the tunnel is healthy, an Access application covers the host. Host from `LIVE_HOST`, never the repo; Cloudflare API reads only |
| manual | `MAN-` | `manual.yaml`, listed as "needs a person" |
| walkthrough | `WALK-` | not a default stage (see below): every role's menu and pages on a desktop and a phone, with screenshots and UX checks |

## Walkthrough

An independent walk through every function, as each role sees it, on a desktop
(1280x900) and a phone (390x844, touch, menu opened). It is not part of a plain
`run.py`: run it alone with `--stage walkthrough`, or beside the regression
stages with `--with-walkthrough` (a fourth lane). It seeds its own database
(`hoa_reg_walk`, or `REGRESSION_WALK_DB`) and serves on port 8012, so it never
collides with the page stage (`hoa_reg_pages`, 8011). About 2-3 minutes on four
cores (`WALK_CONCURRENCY`, default 3 roles at a time; `WALK_MAX_PAGES` per role,
default 160).

Roles (`page_setup.py walk-sessions`): owner, authorized contact, view-only
contact, tenant, former owner, board member (seat, no office), officer,
manager, ARC reviewer, auditor, sysadmin. For each role and width it records
the menu (sections, items, badges, hub tiles), then opens every menu and hub
page and every page type reached from them (GET only, no downloads or sign-out,
numbers in the path are one page type as in the page stage).

- `out/walkthrough.json`: the inventory, role -> `desktop|phone` -> `menu` and
  `pages` (final URL, status, `<h1>`, breadcrumbs, headings, buttons, links,
  forms (labels only), height, layout problems, console errors, screenshot).
- `out/walkthrough/<role>/<desktop|phone>/<slug>.png`: full-page screenshots;
  `menu.png` is the whole menu with every group open.
- `out/walkthrough-checks.json`: the `WALK-` results (the stage's results file;
  `walkthrough.json` is taken by the inventory): menu entry or hub tile label =
  the page's `<h1>` (the Dashboard's greeting excepted), no sideways scroll,
  nothing wider than its container or off the screen, phone tap targets at
  least 40x40 px outside running text, no duplicate labels in a role's menu,
  every reachable page opens (HTTP 200, not only a redirect), no console or CSP
  errors.

`/walkthrough` then has two fresh reviewers (desktop and phone) read the
inventory and screenshots and write findings (merge, rename, move, remove,
flow, mobile, keep separate for legal or role reasons, proposed menus) to
`out/findings/walkthrough-desktop.json` and `walkthrough-phone.json` (the
standard schema; ids `WD-`/`WP-`, groups mapped to this report's: duplicate ->
merge, naming -> rename, placement -> move, dead-end and long-flow -> flow,
layout -> mobile, keep-separate -> keep separate). A run may still merge them
by hand into `out/walkthrough-findings.json` (the older format in
`walkthrough_report.py`, ids `WT-001..`), which then wins. `walkthrough_report.py` renders
`out/walkthrough.html` from all three (only failing pages are embedded, as
small thumbnails; `--no-thumbs` embeds none). A walkthrough fixes nothing in
product code: the owner decides.

## Results and IDs

Each stage writes `out/<stage>.json` (the walkthrough: `out/walkthrough-checks.json`): `{stage, prefix, started, finished, environment,
results: [{id, key, check (= key, kept for older tooling), status: pass|fail|skip|manual, detail, evidence}]}`. A
stage that crashes adds `<PREFIX>-RUN` (fail).

**IDs are stable.** A check's key is its name (`accounts/tests/test_signin.py::test_x`,
`board: /arc/{id}/ loads`); the first run of a new key gives it the next number
in `ids/<stage>.yaml`. Commit that file. Never renumber or reuse an ID; to rename
a check, edit its key there. A key a run no longer produces shows as skipped
until its line is deleted.

## The catalogue

`catalogue/<area>.yaml` lists every feature with regexes over
`<stage>:<check>`:

```yaml
- feature: "Invoice approval: one or two board approvals, never the entrant; payments"
  since: M2
  match: ["^unit:operations/tests/test_invoices"]
```

A feature is **pass** when its checks pass, **fail** when any fails, **not run**
when nothing matched, **uncovered** when it lists no checks. The report also
lists checks that belong to no feature.

**Every feature change** keeps this current: tests for it, a catalogue entry
whose `match` finds them, a `manual.yaml` entry for anything only a person can
check, and the new IDs committed.

## Review findings

A full run (`/regression`) also has three independent reviewers write
`out/findings/bugs.json` (`BUG-`), `out/findings/claims.json` (`SEC-`, plus
`claims`: each entry of `claims.yaml` held or not) and
`out/findings/exposure.json` (`EXP-`), and the run writes `out/notes.yaml`
(`{summary, observations}`). Runs before the standard (D90) wrote
`out/findings-bugs.json` and `out/findings-security.json` (`R2-` merged into
bugs); `report.py` still reads those. The run fixes nothing in product code:
findings go to the report and are fixed afterwards.
