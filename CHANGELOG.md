# Changelog

One entry per release of the portal. The version is in `VERSION` and shows on
System status. Versions follow MAJOR.MINOR.PATCH: a new feature or a change
people notice raises MINOR, fixes only raise PATCH, and 1.0.0 is the go-live
with real owners (decision D37). Every merge to `main` bumps the version and
adds an entry here.

## 0.6.0 — 2026-11-26

- Server tasks (Admin → Server tasks): take a backup, restore drills, off-site
  lock check, re-apply host hardening and health checks, started in the portal
  and run by the VM agent from a fixed list (D36). "Take a backup now" on
  System status.
- Version numbers: System status shows the release number and the number of
  an available update (D37).

## 0.5.1 — 2026-11-26

- The board can move a payment or credit to another ownership account of the
  lot (Move to account, D35).

## 0.5.0 — 2026-11-26

- Online payments through Stripe (test mode), open-item accounting with money
  kept per ownership account, bank feed (Teller free tier or statement files),
  reserve plan, portal search, surveys, event RSVPs and volunteer sign-ups,
  meeting packets.
- Streamlined menus and pages: one Voting page, Lots and owners, Invitations
  and sign-ups, Budget and reserves, Board and committees, meetings on the
  calendar, the meeting page as the hub.
- Every finding of the full regression and security audit fixed.

## 0.4.0 — 2026-11-25

- Milestone 4: backups and restore drills, off-site locked backups, health
  monitoring, email digest, onboarding, annual meeting and elections, budget
  notices, compliance calendar, written consent, accounting export and
  reconciliation, collections letters, reservations, installable app, texts,
  access reviews, retention, host hardening, updates from System status.

## 0.3.0

- Milestone 3: accounts and contacts, owner requests and threads, committees,
  folders, contracts, roster, FAQ, ARC neighbor acknowledgements.

## 0.2.0

- Milestone 2: community rules, directory, calendar, announcements, documents,
  meetings, voting, architectural review, violations, vendors and invoices,
  owner ledger, budgets, collections, resale certificates, reports.

## 0.1.0

- Milestone 1: accounts with mandatory MFA, lots and ownership, role-scoped
  access, audit log.
