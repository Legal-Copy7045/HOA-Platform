# Testing the portal with the demo data

The `oak_creek` demo dataset (`python manage.py seed_demo`, or Settings -> Demo
data on the POC, D53) is also test data: every area has records in each state a
person can see, at least one record waiting at each step someone can take next,
and spare "fresh" ones so a flow can be tried more than once before the next
reseed. Everything is fictional: names end in "(demo)", addresses are on two
invented roads, emails use the reserved example.com / example.org domains.

What is seeded, and by whom, is in `community/demo/oak_creek.py`,
`community/management/commands/seed_demo_operations.py` and the area modules in
`community/demo/states/` (their constants name the records below).
`community/tests/test_demo_coverage.py` checks that every state, persona and
role's My items listed here exist after seeding, and that every email address
and home address in this guide is one the seed creates.

## Signing in as a persona

Sign-in rules are not relaxed for the demo. Every persona below is a login with
no password and no authenticator, on the email of its Person record. To use
one:

1. Sign in with your own login. On the POC the reseed with "attach me"
   (Settings -> Demo data) gives your login a board seat and a share of the
   first demo home (`demo_attach_requester`), keeping your own passkey or app.
2. Board -> Association -> Homes and owners -> Sign-ups: find the persona and
   send a new setup link (D59). On the POC every email goes to the
   `EMAIL_REDIRECT_ALL_TO` test inbox.
3. Open the link in a private window, choose a password and set up an
   authenticator app (or passkey). That browser is now the persona.

Quicker on the POC: demo mode (D97). Signed in with your own login (a board
member or sysadmin, with your passkey or app), use the banner at the top of
every page: "Demo — you are viewing as: ..." with a list of the personas
below. Choose one and "View as": the portal is now exactly the persona's
(their menu, scope, recusals and own matters), with your login still signed
in underneath; "Back to me" returns. Keys and connections, Restart, server
tasks, Demo data, sign-in and security pages, Preview as owner and payment or
bank-feed set-up are refused while you view as a persona, and a persona can
never change a real login. A formal vote asks for your own password or
second factor. Every switch is in the audit log under your login, and what
you do as the persona is recorded as theirs with your id behind it. Only
logins the seed made are in the list; one you have set up with a password or
authenticator (steps 1-3 above) drops out of it.

Exceptions on purpose:

- demo007@example.com (Harper Birchfield, joint owner), demo028@example.com
  (Dana Elmsworth) and demo015@example.com (Parker Juniper) have a sign-in reset
  on record, so a setup link is refused ("Start a sign-in reset instead"). For
  Harper Birchfield one is waiting for a second board member: approve it as a
  different board member than the one who started it and give the one-time
  code to the tester (that is the test). Dana Elmsworth's was approved and its
  code is waiting; Parker Juniper's was cancelled.
- The break-glass enrollment code (`breakglass_enrollment_code`) only works while
  fewer than two other board members can sign in; it is not a demo shortcut.

## Personas

| Persona | Name | Email | Home | Who they are |
|---|---|---|---|---|
| president | Blake Hollyhock (demo) | demo001@example.com | 102 Fictional Meadow Lane | Board president, owns a home |
| vice_president | Emery Elmsworth (demo) | demo004@example.com | 108 Fictional Meadow Lane | Board vice president with own matters (own request, ARC request, refund, contact change) |
| treasurer | Logan Foxglove (demo) | demo011@example.com | 120 Fictional Meadow Lane | Board treasurer |
| secretary | Sage Gristmill (demo) | demo018@example.com | 134 Fictional Meadow Lane | Board secretary |
| director | Finley Gristmill (demo) | demo030@example.com | 163 Imaginary Orchard Way | Board member without an office |
| manager | Mel Manager (demo) | demo-manager@example.com | none | Community manager: data entry, never approves or decides |
| arc_reviewer | Oakley Cobblestone (demo) | demo014@example.com | 126 Fictional Meadow Lane | ARC reviewer, next door to 128 Fictional Meadow Lane (recused there) |
| arc_reviewer_2 | Parker Elmsworth (demo) | demo040@example.com | 181 Imaginary Orchard Way | ARC reviewer |
| auditor | Wren Foxglove (demo) | demo047@example.com | none | Auditor (read-only) |
| sysadmin | Sid Sysadmin (demo) | demo-sysadmin@example.com | none | System administrator (nominated by the president, approved by the treasurer) |
| owner | Casey Cobblestone (demo) | demo002@example.com | 104 Fictional Meadow Lane | Owner |
| owner_b | Morgan Applewood (demo) | demo012@example.com | 122 Fictional Meadow Lane | Owner of another home, for cross-owner checks |
| joint_a | Gray Gristmill (demo) | demo006@example.com | 112 Fictional Meadow Lane | Joint owner |
| joint_b | Harper Birchfield (demo) | demo007@example.com | 112 Fictional Meadow Lane | The other joint owner (sign-in reset waiting) |
| owner_arc | Parker Juniper (demo) | demo015@example.com | 128 Fictional Meadow Lane | Owner with an ARC request in review |
| owner_late | Dana Elmsworth (demo) | demo028@example.com | 159 Imaginary Orchard Way | Owner behind on assessments (open collection case) |
| authorized | Yael Applewood (demo) | demo048@example.com | 104 Fictional Meadow Lane | Authorized contact, not on the deed |
| view_only | Zion Hollyhock (demo) | demo049@example.com | 104 Fictional Meadow Lane | View-only contact |
| tenant | Avery Cobblestone (demo) | demo050@example.com | 185 Imaginary Orchard Way | Tenant |
| former_owner | Harper Ironwood (demo) | demo032@example.com | 167 Imaginary Orchard Way | Former owner (sold 90 days ago) |

Every persona has work waiting (the dashboard's "Needs your attention"; for
staff also the My items page) except the auditor, the sysadmin, the view-only
contact, the tenant and the former owner.

## Cross-person checks (try these first)

- As the owner of 104 Fictional Meadow Lane, open any request, ARC request,
  document or account page of 122 Fictional Meadow Lane by changing the number
  in the address bar: expect "Page not found" (404), never the record.
- The view-only contact of 104 Fictional Meadow Lane sees the account and
  requests but no buttons; the tenant of 185 Imaginary Orchard Way sees
  community pages and their own request, never a balance or a ballot.
- The former owner of 167 Imaginary Orchard Way sees only My history (their
  own statements and requests), nothing after the sale and no community pages.
- The manager never sees Approve, Decide, Mark cured, Refer or vote buttons.
- The auditor sees finance and records pages without any buttons.
- The sysadmin sees operations pages only (System status, Server tasks,
  Sysadmin activity, Invitations), no owner data.

## Own matters and recusal (segregation of duties)

As the vice president (108 Fictional Meadow Lane), open each of these: the
board buttons are replaced by a note saying another board member is needed,
and trying the step is refused and audited (D65). Then do the step as the
treasurer and it works.

| Where | Record |
|---|---|
| Association -> Owner requests | "Tree limb over my driveway (demo)" (submitted) |
| Association -> Architectural review | "Pergola over the patio (demo)" (submitted) |
| Finance -> Finance overview -> Disbursements | Refund to 108 Fictional Meadow Lane (waiting for approval) |
| Association -> Homes and owners -> 108 Fictional Meadow Lane | Add "Jordan Sitter (demo)" as an authorized contact (pending) |

The ARC reviewer at 126 Fictional Meadow Lane is next door to 128 Fictional
Meadow Lane: on "Screened porch (demo)" they see no deliberations and cannot
record a position or decide. The other reviewer (181 Imaginary Orchard Way)
has recorded "approve" and can decide it.

Other refusals worth checking: the board member who started a sign-in reset
cannot approve it; the president cannot approve the sysadmin nomination they
made; nobody approves an invoice they entered; the board member who approved
invoice GL-60 cannot give its second approval.

## What is seeded, by area

Dates are relative to the day of seeding.

### Homes, people and sign-in (Association -> Homes and owners, Sign-ups)

- Invitations: pending (138 Fictional Meadow Lane, 155 Imaginary Orchard Way),
  expired (140 Fictional Meadow Lane) and revoked (142 Fictional Meadow Lane).
  None is accepted (that would give a demo login a password). Try: resend the
  expired one, or accept a pending one in the test inbox.
- Account changes (My items -> Homes and owners): pending from 104 Fictional
  Meadow Lane (add "Robin Helper (demo)", authorized, with an email), 122
  Fictional Meadow Lane (add a tenant; add an owner: a trust, with the deed),
  112 Fictional Meadow Lane (remove one joint owner, with the deed) and 108
  Fictional Meadow Lane (own matter); approved, declined and withdrawn ones.
  Try: approve one and decline one (a reason is required).
- Account contacts: authorized and view-only on 104 Fictional Meadow Lane, a
  tenant on 185 Imaginary Orchard Way, a former tenant (ended) on 187
  Imaginary Orchard Way, a view-only accountant (invited on approval) on 122 Fictional
  Meadow Lane.
- Sign-in resets (Second approvals): one waiting for a second board member
  (112 Fictional Meadow Lane's Harper Birchfield), one approved, one cancelled.
- "Ask the board" for a new setup link: from the tenant (My items).
- Sysadmin nominations: approved (Sid Sysadmin), waiting ("Ira Volunteer
  (demo)", demo-it-volunteer@example.com) and declined. Try: approve the
  waiting one as a board member other than the president.
- Board seat nominations, where a new seat needs a second board member
  (`BOARD_SEAT_SECOND_APPROVAL`: on in production and tests, off by default on a
  POC, where a proposed seat is recorded at once and none of these exist): one
  waiting (owner of 173 Imaginary Orchard Way), one declined, and the certified
  election's winner.
- Official notice choices: the owners of 104 and 122 Fictional Meadow Lane, 112
  Fictional Meadow Lane's Gray Gristmill and the president chose email; 128
  Fictional Meadow Lane chose post; everyone else gets paper.
- The quarterly access review is due (Admin -> Settings): record it.

### Owner requests (Association -> Owner requests; owners: My home -> requests)

Submitted (fresh: "Loose paver by the mailbox (demo)" from the authorized
contact, "Where do I park a moving truck? (demo)" from the tenant, plus the
first part's two), in progress and assigned to the manager with a reply and an
internal note ("Streetlight out at the corner (demo)"; the owner never sees the
note), waiting on the owner ("Water pooling by the curb (demo)": reply as the
owner of 122 Fictional Meadow Lane and it goes back to the board), resolved
("Snow plowing schedule? (demo)": the owner of 104 Fictional Meadow Lane can
reopen it), closed, withdrawn, turned into a work order ("Cracked sidewalk slab
(demo)") and the vice president's own.

### Architectural review (Association -> Architectural review; owners: Mine / Asked of me)

Drafts ("Garden shed (demo)", "Front door color (demo)"), awaiting neighbour
acknowledgements ("Back patio (demo)", one answered, one waiting), submitted for
the completeness check ("Solar panels on the rear roof (demo)", "Replace the
rear deck (demo)": accept one, return the other as incomplete), in review
("Screened porch (demo)" with a recorded position, "Split-rail fence (demo)"),
approved, approved with conditions ("Driveway widening (demo)"), denied and
still appealable ("Front-yard fence (demo)": appeal it as 112 Fictional Meadow
Lane's Gray Gristmill), under appeal ("Detached garage (demo)": decide it as
the board), appeal decided ("Hot tub (demo)"), completed, withdrawn, deemed
approved when the review clock ran out ("Rear fence (demo)") and the vice
president's own. Decisions are the ARC reviewers'; appeals are the board's.

### Violations (Association -> Violations)

Reported, no notice yet (140 and 138 Fictional Meadow Lane: send the notice),
notice sent (122 Fictional Meadow Lane), owner responded (112 Fictional Meadow
Lane: schedule a hearing or mark cured), hearing scheduled (128 Fictional
Meadow Lane: uphold or mark cured as the board, not the manager), cured,
upheld, dismissed and closed.

### Maintenance (Maintenance -> Work orders, Vendors, Contracts)

Work orders open, assigned, in progress, completed, closed, cancelled and one
overdue with a follow-up date in the past. Vendors include "Snowbelt Plowing
(demo)", whose insurance lapsed and has no W-9. Contracts: active (mowing),
waiting for its second approval ("Snow plowing, winter (demo)"), discarded and
ended last year.

### Invoices (Finance -> Invoices)

Draft (GL-56), waiting for the first approval (PP-102, PP-103), waiting for a
second approval (GL-60: approved once by the president), approved (GL-55),
partly paid (PP-104), paid (PP-101), rejected (FI-9), void (GL-57) and
approved in advance under the mowing contract (GL-M1).

### Meetings and governance (Community -> Calendar, Meetings, Resolutions and policies, Voting)

- Meetings: the annual meeting (notice sent; a proxy from 104 Fictional Meadow
  Lane, one revoked), next month's board meeting with both packets, an
  executive session (board only), a special owners' meeting, a cancelled
  workshop, last month's held meeting with minutes not yet approved (approve
  them as the secretary) and a meeting two months ago with attendance and
  approved minutes.
- Resolutions: a board resolution, a policy and its revision (supersedes it).
- Written consents: waiting for signatures (signed by the president; the rest
  of the board can sign), adopted, failed and withdrawn.
- Votes: a draft for the annual meeting, open ("Repave the walking path
  (demo)": the owner of 104 Fictional Meadow Lane has not voted), open secret
  ballot ("Install speed humps (demo)"), extended once ("Allow backyard
  chickens (demo)") and closed with results ("Adopt the 2026 reserve study
  (demo)"). No per-option tallies show while a vote is open.
- Elections: taking nominations (one candidate accepted, one nominated and not
  yet accepted), voting open ("Special election: one board seat (demo)": the
  owner of 104 Fictional Meadow Lane has not voted), closed and waiting to be
  certified, and certified (its winner is proposed for a board seat: a
  nomination where seats need a second approval, otherwise a sixth seat).
- Surveys: draft, open (the fountain poll, unanswered by 104 Fictional Meadow
  Lane; the anonymous pool survey), closed with shared results.
- Records requests (Association -> Records requests): submitted, due in three
  days, overdue (from 159 Imaginary Orchard Way), in progress, fulfilled,
  denied and withdrawn.
- Committees: Social (with a topic and a done task), Landscaping, and Welcome
  (inactive).

### Money (Finance)

- Owner accounts: assessments all year, a prepaid year (116 Fictional Meadow
  Lane), late fees (128 Fictional Meadow Lane, 159 Imaginary Orchard Way), a
  goodwill credit (171 Imaginary Orchard Way), a reversed charge (173
  Imaginary Orchard Way). 167 Imaginary Orchard Way has two accounts (the
  seller's and the buyer's): try Move to account on one of its entries.
- Collections: a case at every step: open with the first letter ready (159
  Imaginary Orchard Way), first reminder sent (128 Fictional Meadow Lane),
  second reminder (130 Fictional Meadow Lane), final notice (132 Fictional
  Meadow Lane), payment plan (136 Fictional Meadow Lane), referred (140
  Fictional Meadow Lane, board only) and closed (142 Fictional Meadow Lane).
- Resale certificates: submitted (122 Fictional Meadow Lane), in progress (112
  Fictional Meadow Lane), issued (104 Fictional Meadow Lane) and cancelled.
- Accounting: one deposit and a bank reconciliation; next year's budget is a
  draft (adopt it), this year's adopted; the reserve plan has an adopted
  snapshot.
- Disbursements (Finance overview): refunds waiting for approval (108 Fictional
  Meadow Lane, own matter; 169 Imaginary Orchard Way, fresh), approved and
  waiting for its check (116 Fictional Meadow Lane), check issued and waiting
  to clear (138 Fictional Meadow Lane), cleared (153 Imaginary Orchard Way),
  voided and ready for a new check (155 Imaginary Orchard Way), declined (161
  Imaginary Orchard Way), and a surplus distribution waiting for approval. 165
  Imaginary Orchard Way has a credit nobody has proposed to refund yet.
- Bank feed (Accounting and the Finance overview; switched on for the demo): a statement-file account
  with one line matched and the rest to match.

### Community (Community menu)

- Announcements, including an urgent one and one for the board only.
- Calendar: the picnic (room left, free volunteer slots), the wine and cheese
  social (full: a third RSVP is refused; its one volunteer slot is taken), a
  past cleanup day and a cancelled food truck night.
- Common area bookings (switched on for the demo): the Clubhouse needs approval
  (one waiting from 104 Fictional Meadow Lane, one confirmed, one declined, one
  cancelled); the Pavilion confirms at once (one booking by the tenant).
- Documents: the Declaration with two versions, a board-only attorney letter,
  clubhouse rules in a folder, a plot plan only the owners of 104 Fictional
  Meadow Lane see, an archived document, and the meeting packets.
- FAQ: three published answers and one draft.
- Paper mail (Association -> Paper mail): the annual meeting notice mailed today
  to every home that gets paper, with synthetic USPS scans on three letters
  (processed, sorted for delivery, delivered; no USPS call is made, and the
  tracking feature stays off until set up), a delinquency letter prepared and
  not yet mailed (159 Imaginary Orchard Way) and one marked not sent. The demo
  barcode uses Mailer ID 000000, which is not a real one.

## Not seeded

- Online payments and automatic payments (checkouts, Stripe payments, payment
  problems, autopay set-ups, attempts and reviews): they need Stripe, which the
  demo has no key for; the services pull from Stripe and nothing may write
  those tables directly. With a Stripe test-mode key on a POC, pay as an owner
  to create them.
- Self-service account recovery requests: they start only in the browser
  session of an emailed link (by design). Try it from "Can't sign in?" as a
  persona that has set up an authenticator.
- Past common-area bookings: bookings can only be made for the future.
- An entry already moved to another account of a home (Move to account): the
  allocation rebuild used by `ledger/tests/test_allocation.py` does not replay
  a move, so the demo leaves it for the tester.
- Text alerts and push notifications: optional features that need outside
  services.
