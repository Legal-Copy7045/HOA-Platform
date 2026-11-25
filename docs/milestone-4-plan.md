# Milestone 4 plan: go-live essentials, governance year, money tools, extras

Directed 2026-11-25 (all four groups chosen). Each part ships with tests,
regression catalogue entries and a commit.

## 1. Go-live essentials
- **Backups**: nightly encrypted database dump and file-store export, kept
  on the VM (14 days) and copied off-site to an S3-compatible bucket the HOA
  owns; a restore script that loads a backup into a scratch stack and checks
  it (the restore drill, repeatable).
- **Monitoring**: health checks (site, worker heartbeat, disk, malware
  signatures, recent errors) shown on System status and emailed to the
  sysadmins when something fails (no personal data, repeat alerts throttled).
- **Email**: SPF/DKIM/DMARC setup steps; owners choose immediate emails or a
  weekly digest for updates (legal notices are always sent at once).
- **Bulk onboarding**: an onboarding page listing every owner's sign-up
  status, invite-all and remind-all buttons, and printable letters asking
  owners without email to give the board one (invitations still go only to
  the email on record).

## 2. Governance year
- Annual meeting and elections: nominations, proxies, live quorum,
  certification of results by someone who is not a candidate.
- Budget and dues notices to owners.
- Compliance calendar with reminders (insurance, Form 1120-H, PA annual
  report, reserve study, seasonal contracts).
- Board resolutions by unanimous written consent (counsel to confirm).

## 3. Money tools
- Accounting export (CSV for QuickBooks), deposit batches, bank
  reconciliation, year-end vendor totals for 1099s.
- Courtesy payment reminders and a collections letter sequence (generated
  and logged; nothing sent without the board).

## 4. Extras
- Common-area reservations; installable app (PWA) with push notifications;
  opt-in SMS for urgent notices; quarterly access review; data retention for
  former owners' contact details; accessibility (WCAG 2.2 AA) pass.
