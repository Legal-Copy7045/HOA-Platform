# Regression runs

One entry per full run (newest first): date, commit, totals, failing check IDs,
finding IDs, report link. No real names, hosts or addresses.

From the standard (D90), `python3 regression/run.py log` adds one row to this
table per run; the older entries below it keep their own format.

| Date (UTC) | Commit | Version | Audit | Pass | Fail | Skip | Manual due | Failing IDs | Findings | Report |
|---|---|---|---|---|---|---|---|---|---|---|
| 2026-11-28 04:56 | `a0ab016` | 0.45.0 | full (full check) | 6225 | 4 | 417 | 121 | CI-014, UNIT-1690, UNIT-4060, UNIT-4556 | 2 high, 2 medium, 5 low, 1 info | [regression](https://claude.ai/artifact/J6ZtKwBbfVk7uFTqnyuqtM) · [walkthrough](https://claude.ai/artifact/X7G3o44JDZ6mmM2XSRzsQ8) |
| 2026-11-28 00:38 | `b471995` | 0.44.0 | full (full check) | 5955 | 2 | 397 | 113 | CI-014, UNIT-4556 | 1 high, 7 medium, 9 low, 4 info | [regression](https://claude.ai/artifact/J6ZtKwBbfVk7uFTqnyuqtM) · [walkthrough](https://claude.ai/artifact/X7G3o44JDZ6mmM2XSRzsQ8) |
| 2026-11-27 21:44 | `7a8d8e9` | 0.42.0 | full (full check) | 5656 | 0 | 394 | 99 | none | 7 medium, 8 low, 1 info | [regression](https://claude.ai/artifact/J6ZtKwBbfVk7uFTqnyuqtM) · [walkthrough](https://claude.ai/artifact/X7G3o44JDZ6mmM2XSRzsQ8) |
| 2026-11-27 | `d62bd1e` | 0.34.0 | full (full check) | 4615 | 0 | 215 | 84 | none | 16 (none critical or high) | [regression](https://claude.ai/artifact/J6ZtKwBbfVk7uFTqnyuqtM) · [walkthrough](https://claude.ai/artifact/X7G3o44JDZ6mmM2XSRzsQ8) |

## 2026-11-28 — full check on release 0.44.0 (with the walkthrough)

- Commit: b471995 (0.44.0 candidate: Keys and connections, one-command install). Totals: 5955 pass, 2 fail (CI-014 and UNIT-4556: the release-logs rule, cleared by this entry), 113 manual, 397 skip.
- Stages: ci 13 of 14; unit 4657 of 4658; page 1281 pass; walkthrough 8040 pass, 25 fail (fresh-MFA pages now skipped, demo receipt files, one small tap target; see WALKTHROUGHS.md).
- Findings (21): BUG-001..009, SEC-001..004, EXP-001..008; 1 high, 7 medium, 9 low, 4 info. Claims: 45 of 48 held (CLM-033, CLM-045, CLM-046 not held). bandit and pip-audit clean. All with a clear fix go into 0.44.0 before release.
- Report: https://claude.ai/artifact/J6ZtKwBbfVk7uFTqnyuqtM (version 8). Walkthrough: https://claude.ai/artifact/X7G3o44JDZ6mmM2XSRzsQ8 (version 6)

## 2026-11-27 (evening) — full check on release 0.42.0 (with the walkthrough)

- Commit: 7a8d8e9 (main after 0.42.0). Totals: 5656 pass, 0 fail, 99 manual, 394 skip (retired IDs; two live checks not reachable from the cloud session).
- Stages: ci 13 pass; unit 4358 pass; page 1281 pass; live 4 pass, 2 skip; manual 99. Walkthrough beside it: 7910 pass, 67 fail (see WALKTHROUGHS.md).
- Findings (16, none critical or high): BUG-001..007, SEC-001..003, EXP-001..006; 7 medium (BUG-001..005, EXP-001, EXP-002), 8 low, 1 info. Security audit: 43 of 44 claims held (CLM-015 not held); bandit and pip-audit clean.
- Report: https://claude.ai/artifact/J6ZtKwBbfVk7uFTqnyuqtM (version 7). Walkthrough: https://claude.ai/artifact/X7G3o44JDZ6mmM2XSRzsQ8 (version 5)

## 2026-11-27 — full check on release 0.34.0 (with the walkthrough)

- Commit: d62bd1e (main after 0.34.0, own matters). Totals: 4615 pass, 0 fail, 84 manual, 215 skip (retired IDs; LIVE-001 HTTPS and one live check not reachable from the cloud session).
- Stages: ci 11 pass; unit 3615 pass; page 985 pass; live 4 pass, 2 skip; manual 84. Walkthrough beside it: 4469 pass, 0 fail.
- Findings (16, none critical or high): BUG-001..003, R2-001..007, SEC-001..006. Medium: BUG-001/SEC-001 (own-matter check on ledger writes and Move to account misses a board member's former account for a home they sold), BUG-002 (refund check issue/void/decline/cleared on one's own former account; R2-006 overlaps), R2-001 (removed co-owner's My history lists later checks), R2-002 (refund payee and address include a removed owner), R2-003 (ARC recusal for affected homes counts only current owners). Security audit: 43 of 47 claims held; bandit and pip-audit clean.
- Report: https://claude.ai/artifact/J6ZtKwBbfVk7uFTqnyuqtM (version 6). Walkthrough: https://claude.ai/artifact/X7G3o44JDZ6mmM2XSRzsQ8 (version 4)

## 2026-11-26 (late night) — verification after fixing every finding

- Commit: d919d44. Totals: 3238 pass, 0 fail, 50 manual, 117 skip (retired IDs from the streamlining merges; live not run: no LIVE_HOST).
- Stages: ci 11 pass; unit 2335 pass; page 892 pass; manual 50.
- Fixed: all 26 findings of the previous run (BUG-001..010, R2-001..009, SEC-001..007) and the 17 residuals found by the verification reviews (fixmoney, fixgov, fixinfra, fixres). Security audit: 25 of 25 claims held; bandit and pip-audit clean.
- Accepted: SEC-004 (info) Teller Connect script cannot be pinned (D34-fixinfra-2).
- Report: https://claude.ai/artifact/J6ZtKwBbfVk7uFTqnyuqtM (version 5)

## 2026-11-26 (night) — M5 features, streamlining and accounting fixes

- Commit: 5b7fb4e. Totals: 3063 pass, 1 fail, 50 manual, 96 skip (78 page and 12 unit IDs retired by the streamlining merges; live not run: no LIVE_HOST in the cloud session).
- Stages: ci 11 pass; unit 2158 pass; page 894 pass, 1 fail; live 6 skip; manual 50.
- Failing: PAGE-946 (a tenant's Voting menu entry links to /surveys/, which now redirects to Voting).
- Findings (26, none critical or high): BUG-001..010, R2-001..009, SEC-001..007. Medium: BUG-001/002/003, SEC-001, R2-001 (ledger entries take the account open on the posting day, not the ownership period the money belongs to: late sale recording, late fees after a sale, online payments confirmed after a sale), SEC-002/R2-003 (meeting-packet zip built in memory), SEC-003 (a manager can mark minutes as approved). bandit and pip-audit clean; 22 of 26 security claims held.
- Report: https://claude.ai/artifact/J6ZtKwBbfVk7uFTqnyuqtM (version 4)

## 2026-11-26 — after fixing the 23 findings

- Commit: 3d40fcc. Stages only (verification of the fixes): ci 11 pass; unit 1529 pass; page 878 pass; manual 34. Failing: none.
- Fixed: BUG-001..007, SEC-001..004, R2-001..012 (BUG-008 fixed earlier), each with tests; owner rulings D30 (prepaid display) applied to BUG-005.

## 2026-11-26 — first run with the standing security audit

- Commit: 7ef4c17 (run on 29f89cb/cdf21b8 code; the date fix 7ef4c17 came after). Totals: 2334 pass, 2 fail, 28 manual, 2 skip (stale ID removed; live HTTPS not reachable from the cloud session).
- Stages: ci 11 pass; unit 1441 pass, 2 fail; page 878 pass; live 4 pass, 1 skip; manual 28. Postgres stopped once in the cloud session; unit and page re-run.
- Failing: UNIT-1071, UNIT-310 (UTC dates in the Eastern evening) — fixed after the run at the owner's request (7ef4c17, BUG-008).
- Findings (24, none high): BUG-001..008, SEC-001..004, R2-001..012. Medium: BUG-001/SEC-003/R2-001 (new plans on a returned ARC draft keep neighbors' answers), BUG-002 (proxy holder who sold can vote), BUG-003/R2-002 (restore drill uses a live R2 bucket), SEC-001 (buyer sees a neighbor lot's old acknowledgements), SEC-002 (contract pre-approval bypasses two-approver invoices), BUG-008 (fixed). bandit and pip-audit clean.
- Report: https://claude.ai/artifact/J6ZtKwBbfVk7uFTqnyuqtM (version 3)

## 2026-11-25 (late) — UX/UI build and the last Milestone 4 features

- Commit: f6339b1. Totals: 2336 pass, 0 fail, 28 manual, 26 skip (25 retired IDs removed after the run; the POC is not reachable from the cloud session).
- Stages: ci 11 pass; unit 1443 pass; page 878 pass; live 4 pass, 1 skip; manual 28.
- Built: the UX review (four builders: sign-in and profile; ARC, requests, notices, documents; navigation, dashboard, accessibility, voting; finance and board admin), reservations (off), access review, contact retention, installable app with push (off), opt-in texts (off).
- Findings: second review R2-001..006 (2 medium), all fixed with tests. Earlier SEC-004/BUG-005 accepted (D27).
- Decisions: D26 (scanner reloads in place), D27 (open items), D28 (change history persists).
- Report: https://claude.ai/artifact/J6ZtKwBbfVk7uFTqnyuqtM (version 2)

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
