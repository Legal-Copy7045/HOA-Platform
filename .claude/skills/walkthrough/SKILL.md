---
name: walkthrough
description: Run the HOA portal's independent walkthrough of every function, on desktop and phone - signs in as every role, records every menu, hub and page with screenshots, runs automated UX checks, has two fresh reviewers (desktop and phone) write a streamlining report (duplicates, confusing names, wrong placement, dead ends, long flows, what must stay separate, proposed menus) and publishes it. Use when asked for "/walkthrough", "the walkthrough", "walk through the portal" or a streamlining or usability review. For regression and walkthrough together use /full-check.
---

# Independent walkthrough

The walkthrough stage lives in `regression/` (read `regression/README.md`,
section "Walkthrough"). It shows what each role actually sees, on a desktop
(1280x900) and a phone (390x844): owner, authorized contact, view-only contact,
tenant, former owner, board member, officer, manager, ARC reviewer, auditor and
sysadmin. Four steps, in order. The owner usually reads the report later, so
finish every step and leave nothing waiting on him.

**A walkthrough fixes nothing in product code.** Findings go to the report and
the owner decides what changes.

## 1. Run the stage (in the background)

```bash
python3 regression/run.py --stage walkthrough
```

It seeds the fictional demo community into its own database (`hoa_reg_walk`,
override with `REGRESSION_WALK_DB`) and serves it on port 8012, so it can run
beside the page stage. About 2-3 minutes on four cores. Output:

- `regression/out/walkthrough.json`: the inventory, role -> `desktop|phone` ->
  `menu` (sections, items, badges, hub tiles, `screenshot` of the whole menu)
  and `pages` (final URL, HTTP status, `h1`, breadcrumbs, headings, buttons,
  links, forms (labels only), height, layout problems, console errors,
  `screenshot`).
- `regression/out/walkthrough/<role>/<desktop|phone>/<slug>.png`: full-page
  screenshots (`menu.png` is the menu with every group open).
- `regression/out/walkthrough-checks.json`: the `WALK-` checks (menu label =
  page `<h1>`, no sideways scroll, nothing off screen or out of its box, phone
  tap targets at least 40x40 px, no duplicate menu labels, no page that only
  redirects, no console or CSP errors).

A `WALK-RUN` or `setup:` failure means the stage broke (Postgres down, port
8012 in use, Node missing): fix the environment and run the stage again. New
check IDs land in `regression/ids/walkthrough.yaml`; commit them.

## 2. Two fresh reviewers, in parallel (once the stage has finished)

Launch two general-purpose agents at the same time. Brief them with the code
and the files, not your conclusions. Both are read-only: they read files and
screenshots (the Read tool shows images), run no test suites, nothing that
touches a database, no servers, and they ignore `.claude/`. They include
nothing that identifies a real person (the demo community is fictional).

Give each of them:
- `regression/out/walkthrough.json` and the screenshot folder
  `regression/out/walkthrough/` (their width's subfolders, plus `menu.png`);
- `regression/out/walkthrough-checks.json` (the automated failures);
- `CLAUDE.md`, `docs/glossary.md`, `docs/decisions.md`, `core/navigation.py`
  (menus, hubs, `SUBPAGES`), `core/policy.py` (who may do what) and
  `core/features.py`.

Ask each for one JSON file in this schema:

```json
{"summary": "two or three plain sentences",
 "roles": [{"role": "owner", "desktop_items": 9, "phone_items": 9, "notes": "..."}],
 "findings": [{"id": "WT-D01", "group": "merge|rename|move|remove|flow|mobile|keep_separate",
   "title": "...", "now": "what the person sees today", "proposal": "what to change",
   "who": "roles affected", "effort": "S|M|L", "risk": "what could go wrong, incl. legal",
   "evidence": ["regression/out/walkthrough/owner/desktop/home.png"]}],
 "proposed_menus": {"owner": [{"title": "My home", "items": ["..."]}]}}
```

- **Desktop reviewer** -> `regression/out/walkthrough-findings-desktop.json`
  (ids `WT-D01..`): walk each role's desktop menu and pages. Redundancy (two
  pages or menu entries doing one job), naming (labels that differ from the
  page heading or the glossary, jargon, the same thing called two names),
  placement (a page in the wrong section, hub or role), dead ends (pages with
  no way on or back, empty states without a next step), long flows (steps,
  pages or clicks that could be fewer) and the separations that must stay for
  legal or role reasons (binding votes vs surveys, secret ballots, recusal,
  manager never decides, board-only records, former owners read-only,
  executive sessions): list those as `keep_separate` so nobody merges them.
  Propose a sidebar for each role.
- **Phone reviewer** -> `regression/out/walkthrough-findings-phone.json` (ids
  `WT-P01..`): everything that is worse on a phone, from the phone
  screenshots and the `layout` data: layout that breaks or crowds, tap targets,
  the menu (length, groups, what is above the fold), very long pages (`height`),
  forms (field count, labels, buttons out of reach), tables that need sideways
  scrolling or should become cards, actions that are hard to reach. Group
  `mobile` unless another group fits better.

Then merge both into `regression/out/walkthrough-findings.json`: renumber
`WT-001..` (desktop first, then phone; keep the old id at the end of the
title in brackets), merge duplicates (keep both evidence lists), one combined
`summary`, `roles` with both item counts, and `proposed_menus` from the desktop
reviewer (phone notes go in findings).

## 3. Report

```bash
REPORT_TZ=America/New_York python3 regression/walkthrough_report.py
```

It writes `regression/out/walkthrough.html`: menus per role (desktop vs
phone), the reviewers' findings by group, the failing automated checks (with
links to screenshots and small embedded thumbnails of the failing pages, under
8 MB) and the proposed menus. If it is over 8 MB, run again with `--no-thumbs`.

Publish it as a private artifact (Artifact tool, first publish `icon:
"compass"`). On later runs republish to the same URL: take it from the
newest entry of `regression/WALKTHROUGHS.md` that has one (pass it as `url`). Then add an
entry at the top of `regression/WALKTHROUGHS.md`: date, commit, roles and page
types walked, automated checks (pass / fail, failing IDs or counts by kind),
finding IDs by group, the report link (with its version). No real names,
hosts or addresses.

## 4. Commit

Commit the suite changes only (`regression/ids/walkthrough.yaml`,
`regression/WALKTHROUGHS.md`, any tooling or test fix in `regression/`) and
push to the working branch. Never product code. Tell the owner the counts,
the findings by group (the most valuable three in a sentence each) and the
report link.
