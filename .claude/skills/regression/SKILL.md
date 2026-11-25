---
name: regression
description: Run the HOA portal's full regression suite (every stage in regression/: code quality and supply chain, the whole test suite, a browser crawl of every page per role, read-only live POC checks, manual checks), a fresh bug and security review, and publish the report. Use when asked to "run the full regression", "/regression", or for a health check after a big feature set. Not for ordinary changes (CI covers those).
---

# Full regression run

The suite lives in `regression/` (read `regression/README.md` first). Five
steps, in order. The owner usually reads the report later, so finish every step
and leave nothing waiting on him.

## 1. Bring the suite up to date

- Read `git log` since the commit of the newest entry in `regression/RUNS.md`
  (and `docs/milestone-*-summary.md`).
- Every new feature, page, workflow, rule or setting needs a catalogue entry in
  `regression/catalogue/<area>.yaml` whose `match` finds at least one automatic
  check. Write missing tests now (tests only, no product code).
- New pages must also be in `core/tests/test_url_matrix.py` (CLAUDE.md).
- Anything only a person can check (real phone, real mailbox, the VM) goes in
  `regression/manual.yaml` with steps.

## 2. Run the stages

In the background:

```bash
python3 regression/run.py
LIVE_HOST=<POC hostname from docs/runbook.md> python3 regression/run.py --stage live
```

Live is read-only (HTTPS reachability, Access in front, Cloudflare API GETs).
If the host or API isn't reachable from the session its checks skip; say so.
A `<PREFIX>-RUN` failure means a stage broke (Postgres down, port 8011 in use,
Node missing): fix the environment and re-run that stage with `--stage`.
Commit new IDs in `regression/ids/*.yaml`.

## 3. Review (two fresh agents, in parallel, while the stages run)

Brief two general-purpose agents with the code, not your conclusions. Both are
read-only, run no test suites (the stages use the databases), verify each
finding end to end, and include nothing that identifies a real person.

- **Bugs** -> `regression/out/findings-bugs.json`: a list of `{id: BUG-001..,
  severity critical|high|medium|low|info, area, title, file, line, description,
  scenario, recommendation, verified}`. Focus on what changed since the last
  run, then the risky paths: money (ledger, invoices, aging), votes, ARC clock
  and recusal, ownership transfer, append-only records, workflows.
- **Security** -> `regression/out/findings-security.json`: `{findings:
  [SEC-001..], claims: [{claim, where, held, note}]}`: every security claim in
  CLAUDE.md, `docs/decisions.md` and `docs/security-review-2026-11.md` checked
  against the code (cross-owner access, MFA everywhere, invitations, recusal,
  two-person rules, append-only, no personal data in logs, file handling,
  trusted proxy, sysadmin scope).

Severity follows `docs/security-review-2026-11.md`: high = account takeover or
a decision someone must not take; medium = data to the wrong person, quietly
wrong records, or an attacker can block the site.

## 4. Report

Write `regression/out/notes.json`: `{"summary": "two or three plain sentences",
"observations": ["..."]}` (coverage gaps, flaky checks, fragile passes, manual
checks overdue). Then:

```bash
REPORT_TZ=America/New_York python3 regression/report.py
```

Publish `regression/out/report.html` as a private artifact (Artifact tool;
first publish `icon: "check"`), then add an entry at the top of
`regression/RUNS.md` (date, commit, totals, stage counts, failing IDs, finding
IDs, the report link).

## 5. Commit

Commit the suite changes (catalogue, ids, manual.yaml, new tests, RUNS.md) and
push to the working branch. **Fix nothing in product code during the run**: the
report lists failures and findings, and the owner decides what is fixed next.
Tell him the totals, the failures, the high findings and the report link.
