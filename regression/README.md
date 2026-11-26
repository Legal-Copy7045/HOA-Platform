# Regression suite

One command runs every check in the repo, gives each one a stable ID, maps them
to the portal's features and produces one report. Run it after a big feature
set, before showing the board something new, or when asked (`/regression` in a
Claude Code session runs the whole thing, reviews included; see
`.claude/skills/regression/SKILL.md`). The independent walkthrough of every
role's menus and pages on a desktop and a phone is a separate stage with its
own report (`/walkthrough`); `/full-check` runs regression and walkthrough
together as one command (see "Walkthrough" below). CI already runs lint and tests on every
push; this is the full health check on top.

The pattern (stages, stable IDs, catalogue, manual list, report, runs log)
follows the regression suites of the owner's other projects.

```
regression/
  run.py            runs the stages -> out/<stage>.json (and out/logs/)
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
`out/walkthrough-findings.json`; `walkthrough_report.py` renders
`out/walkthrough.html` from all three (only failing pages are embedded, as
small thumbnails; `--no-thumbs` embeds none). A walkthrough fixes nothing in
product code: the owner decides.

## Results and IDs

Each stage writes `out/<stage>.json` (the walkthrough: `out/walkthrough-checks.json`): `{stage, started, finished, environment,
results: [{id, check, status: pass|fail|skip|manual, detail, evidence}]}`. A
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

A full run (`/regression`) also has two independent reviews write
`out/findings-bugs.json` (`BUG-`) and `out/findings-security.json` (`SEC-`,
plus `claims`: every security claim in CLAUDE.md and docs checked against the
code), and `out/notes.json` (`{summary, observations}`). The run fixes nothing
in product code: findings go to the report and are fixed afterwards.
