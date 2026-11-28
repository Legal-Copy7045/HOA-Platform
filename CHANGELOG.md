# Changelog

One entry per release of the portal. The version is in `VERSION` and shows on
System status. Versions follow MAJOR.MINOR.PATCH: a new feature or a change
people notice raises MINOR, fixes only raise PATCH, and 1.0.0 is the go-live
with real owners (decision D37). Every merge to `main` bumps the version and
adds an entry here.

## 0.44.0 — 2026-11-28

- Keys and connections (D93): third-party keys are set up in the browser on
  System status → Keys and connections instead of on the server: email
  (SMTP2Go or an SMTP server), Stripe, the bank feed (Teller), USPS, the bounce
  mailbox, the AI key and the vendor documents key. Keys are tested before they
  are saved, stored encrypted, never shown again (only the last four
  characters), audited and emailed to the board. System administrators only,
  with a fresh passkey or authenticator check each time. Stripe and bank keys
  need a second board member's approval, and the Stripe account is locked:
  moving it needs two board members and 72 hours. Changes apply without a
  restart; the server scripts remain as a fallback. "Settings → Features
  (board)" replaces the wrong "Admin → Features" in the help and scripts.
- One-command install (D94): a fresh server is installed with one command that
  asks only for the Cloudflare tunnel token (and, optionally, the first
  officer's name and email for their sign-in link). It generates every server
  secret, hardens the host, starts the portal, checks it and is safe to run
  again.
- Fixes from the 0.44.0 full check: email and AI key changes also need a
  second board member, and every warning about a key change goes out through
  the settings in force before it; a changed mail server needs its password
  typed again and only public mail servers are accepted; removing keys needs
  the same approval; a waiting key change can be refused or cancelled until it
  takes effect; the vendor documents key can't be removed while vendor files
  need it; a seller's payment plan no longer stops the buyer's late fees or
  interest; SMTP2Go delivery records are kept when one message in a batch
  fails.

## 0.43.0 — 2026-11-27

- Email through SMTP2Go (D92): send through SMTP2Go's API (or keep plain
  SMTP); every 5 minutes the portal reads SMTP2Go's delivery reports, so
  bounces, spam complaints and unsubscribes mark the address without a bounce
  mailbox. ARC decisions now email the applicant, and the board sees
  Delivered / Bounced / Pending on each decision. System status has an Email
  card. `set-smtp2go.sh` sets it up; every set-up script now changes only its
  own lines in `.env` and puts back only those if its check fails.
- Server (D91): `docker compose` on the server always uses both compose files,
  cloudflared stays on its own network (updates repair it), a Tunnel network
  health check, and Restart on System status (system administrator, fresh
  sign-in check, audited). A minor release now needs the regression and
  walkthrough logs to be no more than one release behind.
- A shorter menu (D95): "My violations" replaces "Notices about my home";
  records requests are part of My requests; My reimbursements moved off My home
  to Finance overview, committee pages and My account.
- Setup wizard and screens: percentages entered and shown with %, including
  the card surcharge; example text in every wizard box instead of "DEMO -
  Replace"; common areas can be taken out of use and the step saves with none;
  Getting started spacing; the "+ File in a folder" box lines up; bigger
  checkboxes on phones; board dashboards show their work before Getting
  started; one bank status on the Finance overview; clearer names.
- Fixes from the 0.42.0 full check: forged bounce reports can no longer mark
  another owner's address; an approved expense claim can't be paid through the
  general invoice Pay button; a special assessment needs a vote that passed on
  it; collection costs and payment plans stay with the seller after a sale;
  special-assessment late fees respect a kept payment plan; buyers don't see
  the seller's instalments; nobody removes their own vendor conflict link;
  a seller on the board takes no step on their former account's refunds;
  stricter database-role and help-link checks; the receipt page error.
- Project standard 1.2.2: secret scan by pinned image with its allowlist read
  from main; findings with a clear fix go in the next release without asking.

## 0.42.0 — 2026-11-27

- Finance overview (D89): the first page of Finance, one card per part of the
  money side (money in, money out, books, planning, year end and tax), each
  with its status, its own waiting count and a link, and the funds' cash at
  the last closed month on top. It replaces "More finance"; every hub tile now
  shows its own count, so the menu bubble always matches what the page shows.
- Walkthrough fixes (D88): Board cues on board panels of Community pages (one
  Board actions panel on a meeting); My items split into Board work and Your
  home; the dashboard says when it shows part of the board queue; tab strips
  wrap on phones; Sysadmin activity, Reserve plan, Home neighbors and Owner
  accounts are shorter and clearer; breadcrumbs back from hub pages; no more
  duplicate settings tiles or sign-up filters; buttons no longer sit on the
  tiles below them.
- Project standard 1.1.0 (D90): one required CI check (ci-result), releases
  tagged and published from a merged version bump, the server installs the
  latest release tag, weekly Dependabot with auto-merge of small updates,
  weekly CodeQL, a privacy guard, and the standard regression commands. The
  project's instructions for Claude are split into short topic files.

## 0.41.0 — 2026-11-27

Fixes from the full check (D87):

- A board member who sold their home, or a co-owner removed from a deed, can
  no longer post, move, reverse or refund money on the account they were on.
- A co-owner removed from a deed no longer sees checks written after they left.
- Architectural review recusal now covers tenants and contacts of the
  neighbouring homes a request affects, and whoever owned them at the time.
- A candidate cannot open, extend or close their own election.
- Account recovery: "still waiting" emails at most once a day; statement and
  digest emails cannot be switched off during the lock afterwards.
- Online payments accept only a restricted Stripe key (rk_...): if the POC
  uses a full key, run set-stripe-key.sh again with a restricted one.
- The server refuses a database role with superuser rights; text messages
  refuse redirects; the sign-in picture is malware-scanned; the USPS password
  reaches only the worker; key scripts remove their .env backup copies.
- Rebuilding payment matching after "Move to account" now gives the same
  balances.

## 0.40.0 — 2026-11-27

The portal is now the association's books (self-managed, D69-D85):

- Books and automatic close (D69-D71): operating and reserve funds across the
  association's bank accounts, bank lines coded by rules (interest, fees,
  insurance, transfers...), and months that close themselves when everything
  balances; anything that does not becomes an exception for the treasurer on
  My items. Closed months are locked; reopening takes two board members. The
  year closes itself too, with financial statements, the annual accounting
  figure, a Form 1120-H worksheet, 1099 files (no tax ID numbers stored), PA
  filing deadlines and a signed year-end package with a treasurer handover
  sheet. Optional QuickBooks Online export.
- Special assessments, payment plans with schedules, expense reimbursements,
  vendor check printing and next year's budget pre-filled from this year
  (D75-D78).
- Optional features, all off until the board switches them on: interest on
  overdue balances, a 12-month cash-flow forecast, liens and legal steps in
  collections, capital projects, an owners' operating-vs-reserve and
  budget-vs-actual summary, insurance claims (D80-D85).
- Vendor onboarding (D79): a single-use link where a vendor fills in their
  details, W-9 and insurance certificate (encrypted in the portal; key set on
  the server with set-vendor-key.sh), reviewed by two board members before
  any invoice can be paid.

## 0.39.0 — 2026-11-27

- Renewals (D72, Maintenance → Renewals): the association's insurance
  policies, vendors' insurance certificates and contracts' renewal and
  cancellation-notice deadlines, with My items and one board email per
  reminder; the board decides contract renewals. The resale certificate reads
  the current liability policy.
- Records retention (D72, Settings → Records retention): keeping periods by
  document category, records past their period listed for the board, removed
  only when two board members agree and never under a legal hold; the record
  of what was disposed of stays. Each document shows who opened or downloaded
  it. Plain-text versions can be compared side by side.
- Monitoring (D74): System status now warns about failed or stuck background
  jobs, payment processing failures (and the board gets a to-do when an
  automatic payment fails), and the server's disk and database growth with
  a "disk full in N days" estimate; an incident timeline lists what happened.
  Optional: bounced emails read from a bounce mailbox (set up with
  `set-bounce-mailbox.sh`); a bounced official notice asks the board to send
  a paper copy.
- Owner help (D73): a getting-started checklist that follows what each owner
  has actually done, a welcome page after first sign-in, "How it works" pages
  for paying, architectural review, violations, voting and requests that show
  your current rules, help in portal search, and "Ask the board" on every page.

## 0.38.0 — 2026-11-27

- The demo community now has records in every state of every section, with
  something waiting at each next step, and a test login for every role
  (including a board member with their own matters and a recused ARC
  reviewer). docs/demo-test-guide.md says what is seeded, where to find it and
  what to try, including what should be refused. Reseed from Settings → Demo
  data to get it on the POC.
- Fixed: an owner with more than four to-dos saw a "See all in My items" link
  on a phone that led to a refused page; owners now see all their to-dos on
  the dashboard.

## 0.37.0 — 2026-11-27

- Secret ballots are sealed while they wait to be mixed (D68). A secret
  vote's, election's or anonymous survey's choice is encrypted, padded to one
  size, before it is stored, and opened only when it is shuffled into the
  results. Someone with the database, a backup or the server's logs, but not
  the server's secret key, can no longer link a home to how it voted. Votes
  still count once per home and none is lost on an error.
- The server's secret key now also protects open secret ballots: keep
  `.env` apart from backups, and rotate the key only with the old one listed
  in `DJANGO_SECRET_KEY_FALLBACKS` until open secret votes close (runbook).

## 0.36.0 — 2026-11-27

- Surplus distributions follow the Declaration (D67). A surplus is split in
  proportion to the assessments paid in its fiscal year (or equally per
  home, as a setting) and posted as statement credits that pay the next
  assessments until used up.
- The credit stays with the home by default: what a seller paid towards
  goes to the home's account, so the buyer receives it. A setting makes it
  follow the owner who paid instead (a seller's share then waits on their
  old account to be refunded).
- Paying a surplus by check is off unless the board switches it on.
- Community setup has a new "Surplus and refunds" step with these choices
  and what the law says.

## 0.35.0 — 2026-11-27

- Refund checks are made out to the right people: today's owners while the
  home is still theirs, or the owners at the sale once it is sold. A co-owner
  taken off the deed is no longer named on the check or used as its address.
- Payments to board members are processed by another board member (D66).
  Nobody prints, writes, voids or clears a check to an account they are or
  were on the deed of, sellers included, and the manager never processes a
  check to a board member. "Mark as issued" writes the checks you may write
  and names the ones left for another board member.
- A board member who no longer owns a home here shows on the board's My items
  as "End <name>'s board seat": board members selling their home resign first.
- The start-up exception for board seats is one-time: once two seats have
  been recorded, a new seat always needs a second board member, even if seats
  later end. An owners' election can still seat its winners on a board of one.

## 0.34.0 — 2026-11-27

- Board members' own homes are handled by another board member (D65). Your
  requests, architectural requests, notices, bookings, account, collection
  case, resale certificate and refunds are homeowners' matters like anyone
  else's: every board member can read them, but you can't take the board's
  steps on your own (assign, start, decide, close, waive, post a charge or
  credit, record a payment, write a letter, approve a booking or a change to
  your account...). The page shows it as your home's page, without the Board
  label: "This is your own home's request: another board member handles it."
  You still reply, withdraw, respond, add photos, cancel or pay as the
  homeowner. Internal staff notes on your own matter are hidden from you, as
  for an ARC reviewer who is recused. Your own matters leave your board work
  in My items; if no other board member or the manager (with two-step
  sign-in) can act on one, My items says "Only you can act on … and it's
  your own — another board member is needed". Refused attempts are in the
  audit log.

## 0.33.0 — 2026-11-27

- Acting for the board is now easy to see (D64). Pages in the board's
  sections carry a "Board" label beside their heading and a coloured strip
  at the top; in the menu, Association, Maintenance, Finance and Admin sit
  under one "Board" heading in the same colour. Decision buttons say who is
  acting ("Approve for the board") and their confirmation ends "You're
  acting for the association."; where a decision is about your own home or
  your own request, the page says another board member must decide before
  you click. Board members with a home see their dashboard in two parts,
  "Board work" (first when something waits there) and "Your home". Board
  and staff notification emails start with "[Board]"; statements, notices
  and receipts don't. Same login, same menu, no extra sign-in.
- Settings opens on Community setup: where setup stands and every step,
  then "Change one thing" links. Features is one page (the setup step), and
  My items has one banner for setup and the rules still missing a citation.
- Shorter board menu: Reserve plan, Online payments, Disbursements and
  Resale certificates moved to More finance; only the busiest board section
  starts open. Server task history is on Sysadmin activity for the board.
  Committees open from Board and committees.
- Clearer names: the setup step for recovery waiting times is "Account
  recovery rules" ("Sign-in and security" is your own page); "Record payments
  received" (owners' payments) and "Write checks" (the association's
  checks); the My items group "Owner requests", with one line of links
  instead of count tiles; a former owner's menu section is "My history".
  The manager's Settings and the auditor's Community settings link straight
  to the page.

## 0.32.0 — 2026-11-27

- Walkthrough 2 fixes, part A (D63). The dashboard and Community settings no
  longer print a developer note. Board, executive and committee meetings no
  longer offer owners a proxy or show an owners' quorum (owners may attend and
  speak; they do not vote there), and the portal refuses such a proxy. The
  meeting packet .zip downloads for everyone again: a file that cannot be read
  is left out and listed in NOT-INCLUDED.txt. Delinquency notices are no
  longer offered on Paper mail for every home: the collection case's letter
  has "Also prepare a paper copy" for that one home. Voting's Open list no
  longer shows drafts; the board has a Drafts filter. Community setup shows
  each law once per step, with the law panel closed on phones.
- Phones: every table reads as cards, "Unsaved changes" stays hidden until
  something changes, amounts on the dashboard never break over two lines,
  Save bars sit above the tab bar, Community settings has "Jump to section",
  Sysadmin activity counts repeats on one row (100 rows a page), Record
  payments starts with three rows and adds rows without losing what was
  typed, the surplus form keeps the homes to pay by check in a closed panel,
  a home's account has one small Actions menu per entry, Calendar's New
  event and New meeting open in place, Home neighbors has one Save all, and
  People has a find box and sign-up filters.

## 0.31.0 — 2026-11-26

- Can't sign in? The sign-in page now has one help link instead of two. It
  asks what's wrong and takes you to the right step: forgot your password
  (the emailed reset, as before); lost your phone, passkey or app (type a
  recovery code or use another device, otherwise recover your sign-in after
  the waiting period); setup link expired or never finished (ask the board
  for a new setup link, which shows on the board's My items); no access to
  your email (the board helps, with who to contact); locked out (how long to
  wait, from the settings in effect, then Try again). None of these pages
  says whether an account exists. Old links to the password reset and
  account recovery still work.

## 0.30.0 — 2026-11-26

- Votes and elections are timeboxed (D61): opening one needs an opening and
  a closing time, at least the shortest voting period apart (a Community
  setting, 7 days by default). Voting closes by itself at the deadline and
  the results are counted then, secret ballots included. The board can
  extend a deadline once, with a reason, before it passes (owners are told),
  never shorten it, and close early only if Community settings allow it.
  Owners who have not voted get a reminder with their personal link 48 hours
  before the close, and everyone is told of an extension, straight away even
  if they chose the weekly summary. The deadline shows on the Voting list, the page, the
  email and My items ("Vote by ...").
- Turnout while voting is open: the board and the manager see "N of M homes
  have voted", the quorum needed and whether it is reached so far, a
  progress bar and which homes have voted (addresses only, with a CSV
  download); surveys show how many people have responded. Nobody sees how
  the votes are going until voting closes, for votes, elections and
  surveys alike. Owners see whether their own home has voted, and the
  turnout only if Community settings allow it. My items tells the board
  "Vote closes in 30 hours: 61% turnout, quorum met".
- Community setup: the voting period, early close and owner turnout
  settings are in the meetings and voting step; the server step now shows
  where uploaded files are kept; a check keeps every setting, feature and
  server script in the wizard as they are added.

## 0.29.0 — 2026-11-26

- USPS tracking for paper mail (optional, Admin → Features → USPS tracking):
  once a sysadmin enters the association's USPS Business Customer Gateway
  login on the server (`sudo bash infra/poc/set-usps.sh`), the portal reads
  USPS Informed Visibility scans of barcoded letters every 4 hours for 30
  days after mailing. The mailing page shows each letter's status (Mailed,
  Processed, Sorted for delivery on a date, No scans after 5 days) and the
  last scan; the meeting page says how many paper notices were sorted for
  delivery. Letters with no scan after 5 days go on My items to check the
  address. Paper mail settings shows whether tracking is connected; System
  status has a "USPS tracking" check, and the Server step of Community setup
  shows whether it is set up.

## 0.28.0 — 2026-11-26

- Forgot your password? The sign-in page links straight to it. The emailed
  link (valid an hour, works once) still asks for your passkey, authenticator
  app or a recovery code before you choose a new password, never signs you in
  by itself, and every address on file is told "Your password was changed".
- Fewer lockouts: My items asks you to add a backup sign-in method (a second
  passkey, or a passkey and an app) and to make new recovery codes when two or
  fewer are left. Recovery codes are shown once, with a printable page and a
  text-file download.
- Lost your phone and your recovery codes? Homeowners, account contacts and
  tenants can recover their sign-in themselves from Sign-in help: a 48-hour
  wait, alerts to every email address on file, your phone (if you get texts)
  and the board, Cancel for you and the board (signing in normally cancels it
  too), then a new passkey or app. For 48 hours afterwards votes, proxies,
  contact details, notice and text choices, automatic payments and account
  change requests are locked, with a banner saying until when. Board members,
  officers and staff keep the two-board-member reset.
- The waiting period and the lock are Community settings ("Sign-in and
  security", also a step of Community setup): at least 48 and 24 hours, at
  most 14 days.
- A board "new setup link" is refused for a login with any account recovery
  on record; that person needs the two-board-member reset.

## 0.27.0 — 2026-11-26

- Someone who accepted their invitation but closed the browser before adding
  a passkey or app is no longer stuck. The board sees them on Sign-ups as
  "Signed in, no passkey or app yet" and in My items after a week, and can
  send a new setup link to the address on record. Anyone who ever had a
  passkey or app still needs the two-board-member sign-in reset.
- The "Your setup link expired" page says what happened and what to do next.
- An invitation link stops working as soon as the person's email on record
  changes.
- Being signed out for having no passkey or app is recorded in the audit
  log. New tests check that every page in the portal sends a login without
  a passkey or app to setup (D59).

## 0.26.0 — 2026-11-26

Hardening after an external review (D57).

- Files waiting for a malware scan: System status lists each one (where it
  belongs, when it was uploaded, how long it has waited, size, scan attempts
  and what the last attempt said, and "Stuck" after 15 minutes), never its
  name or whose it is. "Scan waiting files now" queues them. Files the
  scanner could not check at upload are now retried by themselves, two
  minutes later and every 10 minutes while the scanner answers; before, they
  stayed blocked until someone ran a command. A health check fails when a
  file has waited more than 30 minutes.
- Production refuses to start without ClamAV and clean-scan-only downloads
  (the POC too), with mail redirected to one inbox, or with HSTS under a
  year.
- Backups health: in production, a missing or day-and-a-half-old off-site
  copy, or no passed restore drill in 90 days, is a failure; restore drills
  record themselves and Server tasks shows the last one. The POC says plainly
  when it is not production-ready.
- django-allauth 65.23.0 (security release for rate limits).
- CI: secret scanning (gitleaks), a vulnerability scan of the image (Trivy),
  CodeQL, and the page crawl of every role.
- Infrastructure images (cloudflared, ClamAV, Postgres, SeaweedFS, Mailpit)
  are pinned and updated through tested Dependabot pull requests; the VM no
  longer pulls the newest cloudflared every day.
- The meeting packet page's crumb goes straight to the meetings list.
- Documentation: README, late fees, the security review's scope, and a
  go-live checklist in the runbook.

## 0.25.0 — 2026-11-26

- Secret ballots, election ballots and anonymous survey answers can no
  longer be matched to who cast them, even by someone reading the database
  afterwards (D56). The choice waits in a mixing pool with no name on it and
  is written later, shuffled with others, in a separate step. While a vote is
  open the board's results say how many ballots are still "waiting to be
  mixed"; closing the vote, election or survey counts every one of them.
- Votes cast before this release keep the old layout (demo data only): run
  real secret votes only on this release or later.

## 0.24.0 — 2026-11-26

- Paper mail (Association → Paper mail, board and manager): letters for
  owners who get official notices by post, one per home and mailing
  address, from a meeting notice, a budget notice, the Letters and
  certificates wording or a blank letter, with placeholders and a preview.
  One PDF of letters laid out for #10 window envelopes (single or double
  window, positions adjustable with a test page), #10 envelopes, or a sheet
  of 30 address labels. "Mark as mailed" is recorded and shown on the
  meeting page; My items reminds until then.
- Optional USPS tracking barcode (Intelligent Mail) on window letters and
  envelopes, once a Mailer ID is entered on Paper mail settings.

## 0.23.0 — 2026-11-26

- Import homes and owners (Homes and owners → Import homes and owners, also
  from Community setup): download an Excel or CSV template, fill in the
  homes, addresses and owners, upload it, see what would change and any rows
  to fix, then import. The file is checked for malware, kept privately for
  the board and deleted after the import or after 30 days. Homes that
  already have owners are left alone and importing the same file twice adds
  nothing. The server command `import_register` now reads Excel files too
  (D54).

## 0.22.0 — 2026-11-26

- A second demo dataset, "Fresh start": a community the declarant has just
  handed over (44 homes and owners, a declarant-appointed board of three, a
  sysadmin, a placeholder Declaration, nothing else yet). `seed_demo
  --dataset fresh_start`; the full Oak Creek demo stays the default (D53).
- Demo and test portals only: Settings → Demo data lets a sysadmin replace
  everything with either dataset (type RESEED to confirm). The server takes
  a backup first, keeps real logins with their passkeys, app codes and
  sysadmin role, and is back in about a minute. The page does not exist on
  a real deployment, and the server refuses it there too. A box (ticked by
  default) makes the person reseeding a board member and the owner of a
  demo home in the new data, so one login keeps working as sysadmin, board
  member and owner.
- A loud warning (startup check and health check) if demo data loading were
  ever switched on in production.

## 0.21.0 — 2026-11-26

- Disbursements (Finance, optional feature, on by default): the board can pay
  money back to owners. A surplus distribution splits a total equally across
  every home (the odd cents in address order) as statement credits or checks,
  under a board resolution; an owner refund pays a former owner's credit by
  check after a sale (My items says "Refund $X to the former owner of
  <address>"). A board member other than the one who proposed it approves
  (two at $500 and over); statement credits post on approval and refunds when
  the check is recorded. Checks come from a check register (CSV or PDF) for
  the bank's bill pay, or print on the association's pre-printed check stock
  with an alignment test; then the board records the check numbers, marks
  them cleared (suggested from the bank feed) or voids them (the credit comes
  back). Uncashed checks show up after 90 days. The accounting export,
  reconciliation and bank feed include them. When reserves are above the
  adopted plan's target, Disbursements and My items say so.
- Owners see a surplus credit on My account; former owners see their refund
  check in My history.

## 0.20.0 — 2026-11-26

- Community setup (Settings → Community setup): a step-by-step walk through
  every set-up decision for the board: the community's details and
  letterhead, what the monthly assessment is called and how much it is, late
  fees, online and automatic payments, notices, architectural review,
  violations, common areas, owner requests, meetings and resale, privacy,
  features, then where people and access and the server stand, and a review
  that flags anything still at its default or without a citation. Each step
  shows what Pennsylvania and federal law say about the choice. It saves
  through the same settings and confirmations as the existing pages, and can
  be run again at any time; "Finish community setup" is on My items until
  done (D51).

## 0.19.0 — 2026-11-26

- Automatic payments can now be once a year: the payer chooses "Once a
  year" and a month, and on that month's due day the next 12 months'
  assessments are paid in one payment, less any credit on the account. The
  money becomes prepaid credit that pays each month's assessment as it is
  charged. Months before the first yearly payment, or between the end of
  the prepaid months and the next yearly payment after a change of month,
  are paid automatically month by month; no month is ever charged twice. The set-up page and Stripe's page
  say which schedule and roughly what amount; the schedule can be changed on
  the automatic payments page and takes effect from the next charge.
- Automatic payments above $500 (a new Community setting, surcharge
  included; it replaces the old percent ceiling) are no longer charged
  straight away: they wait for the board's review. The board approves or
  declines from My items or Online payments (never for a home they pay for,
  and never the manager); a declined or expired (14 days) payment is not
  charged and the owners are emailed (D47).
- Online payments lists homes by address in the automatic payments table.

## 0.18.0 — 2026-11-26

- Phones: every button and link is at least a thumb's size, buttons wrap
  instead of running off their card, and money tables (My account, home
  accounts, Owner accounts, Invoices, Collections, Invitations and more)
  become one card per row. A bar at the bottom of the screen opens the
  Dashboard, My account or My items, Voting or Search, and the Menu.
- Filters and tabs (My requests, Voting, Homes and owners, Owner accounts,
  Invoices, Invitations, Resolutions) change the list in place, without
  reloading or jumping to the top; Back and Forward work.
- One button per form: "Ask the board a question", "Ask for approval of a
  project" and the Voting and Resolutions "New ..." buttons open their form
  where they are and hide while it is open.
- Main actions first: Pay online and the statement under the balance on My
  account; Record a payment, Post a charge and Post a credit at the top of a
  home account; the deposit total and Record deposit at the top of
  Accounting.
- Find a home by number, address or owner on Homes and owners; Owner
  accounts can show only homes with a balance; Invitations filter by status.
- The month to charge is a list of months in words with already-posted
  months marked. With automatic posting on, the card shows when the month
  was posted and the next run; if a month is missed, the board is emailed
  once, it appears in My items and on System status, and the card offers
  the button for that month.
- File inputs look like the portal's buttons; date fields match the other
  inputs; committee forms have visible labels; Community settings and ARC
  request types fold into sections.
- Contacts no longer see a resale certificate link they cannot use, and a
  refused or missing page keeps the portal's layout with a way home (D48).

## 0.17.0 — 2026-11-26

- Homes and owners has a Sign-ups view for the board (who has signed up,
  how homes get official notices, invite everyone, reminders and every
  invitation); the separate "Invitations and sign-ups" menu entry is gone and
  its old address leads there. The sysadmin's page is now called Invitations.
- One name per thing: "My home improvement requests (ARC)" on My home and
  My history, "Work order #3", "Invoice PP-102 · vendor", My items groups named
  after the page they open (Voting, Invoices, Second approvals, People...),
  plainer report names, and a tenant's Voting is called Surveys.
- Community settings: a section index at the top, sections that fold away,
  the card surcharge typed as a percent (3 or 2.5; still at most 3%), and
  plain wording for reading neighbor acknowledgements from scanned forms.
  ARC request types fold to one line each with their citation.
- Former owners: My history shows a running balance, a statement PDF for
  their period, and whom to ask about the balance.
- My account links to the people on the account on My home instead of
  listing them twice.
- Ways back: breadcrumbs on every Settings page, Access review and Neighbor
  acknowledgements.
- Sysadmin activity shows 30 days at a time with Older and Newer, by day,
  with filters and a CSV export (recorded in the audit log).
- ARC reviewers choose their position from an empty list (nothing is
  recorded by accident), and steps they can never take are no longer shown.
- Auditors and sysadmins see their usual pages on the dashboard instead of
  an always-empty to-do list; Quick actions moved up under the to-dos.
- Documents: "Ask to inspect it" opens the request form; meeting packets are
  listed under "Meeting packets".
- Phone menu: staff see their work sections open when something waits there,
  and Sign out is at the top of the drawer.
- Accounting setup has its own tab, grouped under plain headings with a line
  on what each account is for, the whole account visible, and the opening
  balance and its date required together; Accounting shows a one-line
  summary.

## 0.16.0 — 2026-11-26

- Meeting notices now include the join link itself (owner decision), and the
  board can mark a meeting held at any time.
- Meetings are now part of the Calendar: one menu entry, with All /
  Meetings / Events and Past meetings (approved minutes are linked there).
  Board members and the manager schedule meetings with "New meeting" next
  to "New event". The old Meetings address leads to the calendar's
  Meetings list. Executive sessions show only in the board's view (D49).
- Meetings say where they are: a place, a link to join online, or both,
  with an optional end time. The join link is shown only on the portal to
  people who can open the meeting, never in emails. Events can have a join
  link too.
- "Add to calendar" on every event and meeting: Google Calendar, Outlook.com,
  Microsoft 365, or a calendar file for Apple Calendar and Outlook desktop.
- Clicking an event anywhere (calendar, dashboard, search) opens its page;
  a meeting opens its meeting page.
- The meeting page shows the board's steps in order and only those that
  apply now, in one panel that starts closed on phones.
- Marking a violation cured is now a board decision; the manager can no
  longer do it.

## 0.15.1 — 2026-11-26

- Bank feed: a guided one-time script sets up Teller on the server
  (`sudo bash infra/poc/set-teller.sh`): it checks the certificate and key
  belong together and have not expired, saves them, creates the token key,
  restarts and checks the result. The Bank feed page now says this in plain
  words instead of pointing at a developer document.
- The Stripe key script now checks the key with Stripe after saving it
  (test or live mode, and whether the read rights are in place).
- Server tasks: "Send a test email" has a Start button.
- Sign-in: the "Use my passkey instead" button is styled like the other
  buttons (it had no padding or rounded corners).

## 0.15.0 — 2026-11-26

- Automatic payments (optional feature, off by default; needs Online
  payments): owners and authorized contacts can have each monthly assessment
  paid on its due day from a bank account (no fee) or a card (credit cards
  with the surcharge), saved on Stripe's page. Credit on the account is used
  first; late fees and other charges are never paid automatically. A payment
  that doesn't go through is emailed and shown as a to-do, soft declines are
  retried once after three days, and automatic payments pause after a hard
  decline or a second failure. Turn it off any time on My account; it ends
  when the home is sold. The board sees statuses and failures on Online
  payments (D46). Stripe test mode only until the HOA's own Stripe
  account is set up.

## 0.14.0 — 2026-11-26

- The health alert email now says what the server errors were, in plain
  words: for each of the five most common kinds in the last hour, which page
  or background job failed, why, whether people saw an error page and what
  to do, with a "Technical:" line underneath for the portal's maintainer
  (error type, where in the code, route, release, times in site time), and
  "and N more kinds" when there were more. System status has a new "Recent
  server errors" card with every kind from the last 24 hours. The health
  probe's and the "email unavailable" page's deliberate 503s are named as
  such. No web addresses, messages, names or IP addresses are recorded;
  counts and error kinds are kept 30 days (D45).

## 0.13.0 — 2026-11-26

- Sign-up asks owners how they want official notices: after setting up a
  passkey or authenticator app, a one-time "Official notices" page offers
  "Yes, email me official notices" or "No, keep sending them by post",
  explaining that email saves the association postage and printing. Skipping
  leaves a My items to-do, which owners who never answered also get. The
  choice and the wording version are recorded and audited (D44).
- Invitations and sign-ups shows how many homes get notices by email, by
  paper, or have not answered, and the wording owners agree to.
- My profile has one "How we reach you" card: official notices, emails (with
  "Also send official notices here"), phones with Mobile/Home/Work, and one
  mailing address for post ("Same as my home's address" or a different
  address in separate fields). Addresses typed the old way show as "Address
  on file (please check)" with a to-do. Letters use the new address first.
- Text alerts are opt-in under Phone: off by default, a texted code still
  required, unticking stops them at once. Adding a phone never signs you up.
- Routine updates: "One email per update" or "A weekly summary, only sent in
  weeks when there is something new". The summary skips updates you can no
  longer open.

## 0.12.0 — 2026-11-26

- Fixed: the installable-app update check (`/sw.js`) failed with a server
  error on the live server for every signed-in visitor (it asked for the web
  address of an empty static file name, which the production file store
  refuses). That was the cause of the "server errors" health alerts.
- The account statement is easier to read: a box at the top says the amount
  due and the date to pay by ("Past due since ..." when late), or "You have a
  credit of $X. Nothing to pay.", or "Nothing to pay. Thank you."; then what
  the amount is made of (each unpaid charge, partly paid ones marked), months
  already paid ahead by a credit, how to pay, and the account activity with
  payments in their own column and a "Balance brought forward" line. The
  tear-off slip stays at the foot; a long history moves the activity to the
  next page so the summary and slip fit on page 1.
- When online payments are on, the statement (and the statement attached to
  delinquency notices) carries a "Pay online" QR code. It holds only the
  portal's address of the pay page: whoever scans it signs in first and
  then sees their own home's charges, never anyone else's. The card
  surcharge is stated from Community settings (D43).

## 0.11.0 — 2026-11-26

- The monthly charge is called what the covenants call it: "Assessment" by
  default, or the board's word from Community settings ("What the regular
  monthly charge is called", with its citation). Pages, emails, letters, rule
  labels and new charges use it; every new monthly charge reads
  "Assessment – December 2026". Charges already posted keep their wording (D42).
- A statement by email when the month's assessment is posted: the amount due,
  the pay-by date (or "nothing to pay" / covered by prepaid credit), links to My
  account and Pay online, and the statement PDF attached. On by default for
  owners and authorized or view-only contacts; untick it on My profile to stop.
  Sent once per account per month, never to tenants or former owners.

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
