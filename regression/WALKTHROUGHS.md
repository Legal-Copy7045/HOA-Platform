# Walkthroughs

One entry per independent walkthrough (newest first): date, commit, roles and
page types walked, automated checks, finding IDs by group, report link (the
report is republished to the same URL each time; take it from the newest entry
that has one). No real names, hosts or addresses. See `regression/README.md`
("Walkthrough") and `.claude/skills/walkthrough/SKILL.md`.

## 2026-11-26 — first run of the walkthrough stage (tooling check, no review)

- Commit: 8575638 plus the walkthrough tooling. Stage only: no reviewers, report rendered locally with an empty findings file, not published.
- Walked: 11 roles, desktop 1280x900 and phone 390x844; 456 page types in all (board and officer 91 each, manager 69, auditor 58, ARC reviewer 33, owner 30, authorized and view-only contacts 26, tenant 19, sysadmin 8, former owner 5). Stage time 2.3 min.
- Checks: 4014 pass, 138 fail. By kind: phone tap targets under 40x40 px 125; something wider than its box or off screen 7 (meeting page "Record a resolution adopted at this meeting" button on a phone, committee page file input on a phone, collection case button on desktop); console 403 and a reachable page that does not open 2 each (authorized and view-only contacts follow a link on My account to `/resale/` and get 403). Menu label = page heading, duplicate menu labels and the phone menu: all pass.
- Findings: none (no review in this run).
- Report: not published.
