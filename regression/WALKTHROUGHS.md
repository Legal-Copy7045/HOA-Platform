# Walkthroughs

One entry per independent walkthrough (newest first): date, commit, roles and
page types walked, automated checks, finding IDs by group, report link (the
report is republished to the same URL each time; take it from the newest entry
that has one). No real names, hosts or addresses. See `regression/README.md`
("Walkthrough") and `.claude/skills/walkthrough/SKILL.md`.

## 2026-11-27 — third full walkthrough (release 0.34.0, with the regression)

- Commit: d62bd1e (main after 0.34.0).
- Walked: 11 roles, desktop 1280x900 and phone 390x844.
- Checks: 4469 pass, 438 skip, 0 fail (second walkthrough: 46 fail). No sideways scroll, nothing off screen, no small tap targets, menu labels match page headings.
- Findings (54; WT-001..032 desktop, WT-033..054 phone, two duplicates merged):
  - merge: WT-001..005
  - rename: WT-006..012
  - move: WT-013, 014, 015, 045, 048
  - remove: WT-016..018
  - flow: WT-019..024, 044
  - mobile: WT-033..043, 046, 047, 049..053
  - keep_separate (do not merge): WT-025..032, 054
- Report: https://claude.ai/artifact/X7G3o44JDZ6mmM2XSRzsQ8 (version 4). Regression: https://claude.ai/artifact/J6ZtKwBbfVk7uFTqnyuqtM (version 6)

## 2026-11-27 — second full walkthrough (release 0.31.0)

- Commit: ee2007b (main 9ab1dec plus the walkthrough tool's download skip list).
- Walked: 11 roles, desktop 1280x900 and phone 390x844. Stage time 3.0 min.
- Checks: 4612 pass, 178 skip, 46 fail (first walkthrough: 138 fail). By kind: meeting packet .zip returns HTTP 500 for every role (33); two download links crawled as pages, a tooling artefact fixed in this commit (10); /board/community-setup/review/ spills out of its cards on phones (2); duplicate "Community settings" label in the auditor's menu (2, counted in the menu checks). No tap-target failures and no sideways scroll.
- Findings (42; WT-001..021 desktop, WT-022..042 phone):
  - flow: WT-001, 002, 003, 006, 012, 013, 033, 042
  - merge: WT-004, 007, 008, 038
  - rename: WT-005, 010, 011, 014
  - move: WT-009
  - mobile: WT-015, 022..032, 034..037, 039, 040
  - keep_separate (do not merge): WT-016..021, 041
- Report: https://claude.ai/artifact/X7G3o44JDZ6mmM2XSRzsQ8 (version 2).

## 2026-11-26 — full walkthrough with desktop and phone review

- Commit: 6b85d42 (release 0.8.0).
- Walked: 11 roles, desktop 1280x900 and phone 390x844; 456 page types. Stage time 2.3 min.
- Checks: 4014 pass, 138 fail. By kind: phone tap targets under 40x40 px 125; wider than its box or off screen 7 (meeting "Record a resolution" button and committee file input on a phone, collection case button on desktop); console 403 and an unreachable page 2 each (contacts follow My account's link to `/resale/` and get 403).
- Findings (51; WT-001..026 desktop, WT-027..051 phone):
  - flow: WT-001, 008, 010, 012, 013, 014, 015, 017, 019, 040, 043, 048
  - merge: WT-002, 011
  - rename: WT-003, 004, 005, 006, 007, 018
  - remove: WT-009, 016
  - mobile: WT-027..039, 041, 042, 044..047, 049, 050
  - keep_separate (do not merge): WT-020..026, 051
- Report: https://claude.ai/artifact/X7G3o44JDZ6mmM2XSRzsQ8 (version 1).

## 2026-11-26 — first run of the walkthrough stage (tooling check, no review)

- Commit: 8575638 plus the walkthrough tooling. Stage only: no reviewers, report rendered locally with an empty findings file, not published.
- Walked: 11 roles, desktop 1280x900 and phone 390x844; 456 page types in all (board and officer 91 each, manager 69, auditor 58, ARC reviewer 33, owner 30, authorized and view-only contacts 26, tenant 19, sysadmin 8, former owner 5). Stage time 2.3 min.
- Checks: 4014 pass, 138 fail. By kind: phone tap targets under 40x40 px 125; something wider than its box or off screen 7 (meeting page "Record a resolution adopted at this meeting" button on a phone, committee page file input on a phone, collection case button on desktop); console 403 and a reachable page that does not open 2 each (authorized and view-only contacts follow a link on My account to `/resale/` and get 403). Menu label = page heading, duplicate menu labels and the phone menu: all pass.
- Findings: none (no review in this run).
- Report: not published.
