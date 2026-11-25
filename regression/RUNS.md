# Regression runs

One entry per full run (newest first): date, commit, totals, failing check IDs,
finding IDs, report link. No real names, hosts or addresses.

## 2026-11-25 — Milestone 4 so far, with review fixes

- Commit: 89fd079. Totals: 1931 pass, 0 fail, 12 manual, 15 skip (14 retired check IDs removed after the run; the POC is not reachable from the cloud session).
- Stages: ci 11 pass; unit 1108 pass; page 808 pass (board, owner, manager, auditor; desktop and phone); live 4 pass, 1 skip; manual 12.
- Failing: none. Before the fixes the run found 9 page failures (the crawler opening a CSV download as a page; suite fixed).
- Findings: 28 (bugs BUG-001..017, security SEC-001..011; 1 high: SEC-001). Fixed with tests: all but SEC-004/BUG-005 (secret-ballot storage linkable by a database administrator), documented in D23 for a decision.
- Also in this run, on the owner's instructions: no anonymous access (D24), personal single-use ballot links with re-authentication (D24), updates from System status (D25), optimization review items (D25). UX/UI review suggestions in regression/out/ux-review.md.
- Report: https://claude.ai/artifact/J6ZtKwBbfVk7uFTqnyuqtM

## 2026-11-25 — end of the security sprint (first run of the suite)

- Totals: 1267 pass, 0 fail, 7 manual, 1 skip (the POC is not reachable from the cloud session; its Cloudflare checks passed).
- Stages: ci 11 pass; unit 692 pass; page 560 pass (board, owner, manager, auditor; desktop and phone); live 4 pass, 1 skip; manual 7.
- Failing: none. Before this run the suite found and the sprint fixed two navigation bugs (manager and auditor shown links they cannot open).
- Findings open: SEC-001 (one member records a board seat; mitigated, board decision), SEC-002 (ballot secrecy in storage; counsel), SEC-003 (malware scanning off on the POC). Fixed findings: docs/security-review-2026-11.md.
- Report: https://claude.ai/artifact/UKCPQJYEm2FC41Vte4GUC7
