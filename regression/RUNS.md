# Regression runs

One entry per full run (newest first): date, commit, totals, failing check IDs,
finding IDs, report link. No real names, hosts or addresses.

## 2026-11-25 — end of the security sprint (first run of the suite)

- Totals: 1267 pass, 0 fail, 7 manual, 1 skip (the POC is not reachable from the cloud session; its Cloudflare checks passed).
- Stages: ci 11 pass; unit 692 pass; page 560 pass (board, owner, manager, auditor; desktop and phone); live 4 pass, 1 skip; manual 7.
- Failing: none. Before this run the suite found and the sprint fixed two navigation bugs (manager and auditor shown links they cannot open).
- Findings open: SEC-001 (one member records a board seat; mitigated, board decision), SEC-002 (ballot secrecy in storage; counsel), SEC-003 (malware scanning off on the POC). Fixed findings: docs/security-review-2026-11.md.
- Report: https://claude.ai/artifact/UKCPQJYEm2FC41Vte4GUC7
