# Changelog

One entry per release of the portal. The version is in `VERSION` and shows on
System status. Versions follow MAJOR.MINOR.PATCH: a new feature or a change
people notice raises MINOR, fixes only raise PATCH, and 1.0.0 is the go-live
with real owners (decision D37). Every merge to `main` bumps the version and
adds an entry here.

## 0.10.0 — 2026-11-26

- My home → "Ask to add someone" asks for the person's email, which they sign
  in with. If they don't use email, a box keeps them on the account as a
  record only, without a sign-in. When a board member approves, the portal
  emails them an invitation to sign in. If it cannot (for example, the email
  already has a login), the approval still stands and the board is told to
  send the invitation from Invitations and sign-ups. Before approving, the
  board sees whether an invitation will go out (D41).
- Owners can ask to change the owners on the deed: add an owner (a joint
  owner, a trust or an entity) or remove one (for example after a death or a
  quitclaim). They attach the recorded deed and give its date. Only the board
  and that account's owners can open the deed. A board member who is not the
  requester, and not the person added or removed, approves. The account must
  keep at least one owner; a sale is still recorded by the board (D41).

## 0.9.0 — 2026-11-26

- Homes are named by their address everywhere, not by lot number (D40): menus,
  page titles, lists, forms, emails, My items, the dashboard, search results,
  payments (Stripe line item and receipts) and the Preview as owner banner.
  "Lots and owners" is now "Homes and owners", a lot's account page is the
  "Home account", and "Lot neighbors" is "Home neighbors". Lists of homes are
  in address order.
- A home's title is its address, with the lot number as a small grey line
  beneath it (My home, My account, Homes and owners, Home account, and the
  headers of requests, violations, collection cases and resale certificates).
- Formal documents name the home as "165 Imaginary Orchard Way (Lot 30)":
  resale certificate, statements, delinquency letters, ARC decision letters,
  violation notices and the printed neighbor acknowledgement form.
- CSV exports start with an "Address" column; the roster, the accounting
  journal, AR aging, the collections and owner transfer reports keep a
  "Lot number" column at the end. The board can still search by lot number.
- The board's My items warns about any home with no address recorded.

## 0.8.0 — 2026-11-26

- System status shows malware scanning at a glance: answering or not, ClamAV
  version, signature age, files scanned, infected and errored in the last 7
  days, and files waiting to be scanned.
- A "Server resources" card: memory really in use (without the disk cache),
  swap and processor load, read by the VM agent once a minute from /proc.
  A "Server memory" health check emails the sysadmins when less than 10% of
  memory stays free for 10 minutes (D39).
- For the team: `/walkthrough` (independent walkthrough of every role on
  desktop and phone) and `/full-check` (regression and walkthrough together).

## 0.7.0 — 2026-11-26

- Updates can wait for the night: System status offers "Install now" or
  "Install tonight at 4:00 am" (Eastern), with "Cancel tonight's update" until
  then. At that time the update agent installs the newest version (D38).

## 0.6.2 — 2026-11-26

- System status on a phone: cards stack in one column and their buttons fit;
  a health check's button sits below its text. The Backups check points to
  Server tasks instead of a server script.

## 0.6.1 — 2026-11-26

- Faster checks before each update: the automated tests run in four parallel
  shards, so a pull request is ready in about 3 minutes instead of 8. No
  change to the portal itself.

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
