---
name: full-check
description: Run the HOA portal's full regression and the independent walkthrough together as one command - every regression stage plus the desktop and phone walkthrough in parallel, all five reviewers (bugs, two security audits, desktop and phone walkthrough) at once, both reports published and both run logs updated. Use when asked for "/full-check", "regression and walkthrough", "run everything" or "the full check". For one of the two alone use /regression or /walkthrough.
---

# Full check: regression and walkthrough together

This runs the `regression` skill (`.claude/skills/regression/SKILL.md`) and the
`walkthrough` skill (`.claude/skills/walkthrough/SKILL.md`) as one job. Read
both first: this file only says how they interleave and does not repeat their
steps. Neither fixes anything in product code; the owner decides from the two
reports.

## 1. Bring the suite up to date

Regression step 1 (catalogue, tests, URL matrix, manual list).

## 2. Start every stage at once (in the background)

```bash
python3 regression/run.py --with-walkthrough
LIVE_HOST=<POC hostname from docs/runbook.md> python3 regression/run.py --stage live
```

`--with-walkthrough` runs ci, unit, page and the walkthrough in four parallel
lanes (the walkthrough on its own database `hoa_reg_walk` and port 8012, the
page stage on `hoa_reg_pages` and 8011), then manual. Live is read-only and
quick; run it beside the rest as the regression skill does. Allow 6-10
minutes on four cores. A `<PREFIX>-RUN` or `setup:` failure: fix the
environment and re-run just that stage with `--stage`.

## 3. All five reviewers, in parallel

While the stages run, launch the regression skill's three reviewers (step 3
there: bugs, security claims and whole codebase, security new code and
deployment). They read code only, so they need not wait.

As soon as the walkthrough stage has written `regression/out/walkthrough.json`
(the run prints `walkthrough {...}` when it finishes), launch the walkthrough
skill's two reviewers (step 2 there: desktop and phone). All five are fresh,
independent, read-only agents briefed with the code, not your conclusions.

## 4. Both reports

When all of them are done:

- Regression step 4: merge `findings-review2.json` into `findings-bugs.json`,
  write `notes.json`, run `REPORT_TZ=America/New_York python3
  regression/report.py`, publish `report.html` (same artifact URL as the
  newest entry in `regression/RUNS.md`), add the `RUNS.md` entry.
- Walkthrough step 2's merge and step 3: write `walkthrough-findings.json`, run
  `REPORT_TZ=America/New_York python3 regression/walkthrough_report.py`,
  publish `walkthrough.html` (same URL as the newest entry in
  `regression/WALKTHROUGHS.md`), add the `WALKTHROUGHS.md` entry.

Each log entry names the other report's link, so the two runs can be found
together.

## 5. Commit and tell the owner

Commit the suite changes (catalogue, ids for every stage, manual.yaml, new
tests, RUNS.md, WALKTHROUGHS.md) and push to the working branch. Tell the
owner, in one message:

- the regression totals and the failing checks;
- the security audit's findings by severity (and claims held);
- the walkthrough's counts (roles, page types, failing UX checks by kind) and
  its findings by group, with the most valuable three in a sentence each;
- both report links.
