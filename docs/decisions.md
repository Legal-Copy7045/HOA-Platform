# Decision record

Decisions agreed during Milestone 1 planning, with the reasoning. CLAUDE.md
states the resulting rules; this file explains why. Add new entries rather
than rewriting old ones; mark superseded entries as such.

## D1. Board seats and offices are separate records (Q1)

A board seat (membership, declarant-appointed or owner-elected, with a term) is
different from an office (president, VP, treasurer, secretary). Modelling only
offices could not represent directors-at-large, and the declarant flag belongs
to the seat. PA UPCA (68 Pa.C.S. § 5303) governs the declarant-control
transition; thresholds come from the Declaration/bylaws and will be
configuration with citations. M1 records seat type only; it enforces nothing.

- Officers must hold an active seat (setting, can be relaxed with a bylaws citation).
- A seat ends only when `ended_on` is recorded; directors typically serve until
  a successor is elected. The board page warns on terms past `term_end`.
- ARC appointments are dated `RoleAssignment`s with `appointed_by` (board or
  declarant) and an optional citation.

## D2. Email (Q2)

- Local: Mailpit. Tests: in-memory backend. Production: Hostinger SMTP via env.
- POC: `EMAIL_REDIRECT_ALL_TO` (set only in the VM's `.env`) sends every message
  to one inbox; the app logs a warning while it is set. Seed data uses
  `@example.com` (reserved, never delivers).
- Invitation emails contain only the link and the community name.

## D3. Breached-password check (Q3)

HIBP range API with k-anonymity (5-character SHA-1 prefix leaves the server).
Fail-open with a warning log on API failure; 3-second timeout; disable with
`PASSWORD_BREACH_CHECK=false`; mocked in tests. Written in-house (~30 lines)
instead of adding a dependency.

## D4. Authority and two-person rules (Q4)

- The board has five members and no internal rules yet, so every board member
  can perform every board action. Permissions live in one map so offices can
  be given distinct powers later.
- Every action must remain performable by at least two people with MFA; the app
  warns (does not block) when that fails, so a resignation can still be recorded.
- Granting sysadmin: nominator plus a different approving board member, 7-day
  expiry, whole board emailed. Revoking: any one board member, immediately.
- No self-grants. Invitations grant nothing.
- Bootstrap (4c-i): `bootstrap_first_officer` may create one initial sysadmin
  without approval; audited, shown to the board, refused once one exists.

## D5. Strong authentication for every account (Q5, Q10)

Supersedes CLAUDE.md's original "optional for owners".

- Accepted sign-in: passkey; or password then TOTP/passkey; or emailed code
  (6 digits, 10 minutes, 3 tries, tied to the requesting browser) then
  TOTP/passkey. Never password-only. Email is never a second factor, because
  email already controls password reset.
- Code rather than magic link: corporate link scanners consume single-use
  links, and a code is bound to the browser that asked for it.
- Password minimum 12 (NIST SP 800-63B rev 4 allows shorter when MFA is always
  required; 12 is a moderate ask).
- Lost everything: board member A starts an assisted reset and records how
  identity was confirmed; a different board member B approves; all
  authenticators and sessions are removed; a single-use enrollment code (24h)
  is handed over out of band. This closes the gap in which someone with the
  password could enroll their own device.
- Cost accepted: more board support. Mitigations: a help page, spare hardware
  security keys, printable recovery codes.

## D6. Proxy trust and network exposure (Q6)

- Client IP and scheme headers (`CF-Connecting-IP`, `X-Forwarded-Proto`) are
  trusted only from `TRUSTED_PROXY_CIDRS`; otherwise ignored. Prevents lockout
  evasion and audit-log poisoning by spoofed headers.
- cloudflared runs as a compose service on the POC; the web container publishes
  no port there. Cloudflare Access gates the dev hostname.
- No LAN mode. Testing from other devices goes through the tunnel.
- Lockout: 5 failures per email+IP (30 min); 20 per email per hour (15 min).
  Passkey sign-in is not blocked by password lockout.

## D7. Personal data in audit (Q7)

- Unknown-email failed logins: keyed HMAC of the normalised address only.
- IPs in a separate table, purged after 365 days (configurable); core events permanent.
- No user-agent strings. Metadata field allowlist per action, enforced in tests.
- Open: confirm retention with the association's counsel.

## D8. Two database roles in M1 (Q8)

A trigger alone does not bind the role that owns the table (it can drop the
trigger or TRUNCATE). Migrations run as an owner role; web and worker run as an
app role with DML only, no TRUNCATE or DDL, and no UPDATE/DELETE on append-only
tables. Grants are re-applied after every migrate (`core/db_roles.py`). The
owner credentials never reach web or worker containers. Residual risk: anyone
holding owner or superuser credentials can still alter data.

## D9. Sysadmin scope (Q9)

No Django admin in production (it bypasses scoping, two-person rules and audit).
Sysadmins get invitation and system-status pages only. Data fixes use
break-glass management commands that require `--actor` and `--reason` and go
through the service layer. Every sysadmin portal request is audited. Direct
database access is restricted by runbook policy, not software.

## D10. SeaweedFS replaces MinIO locally

MinIO stopped publishing pre-built images in 2025, so the local S3-compatible
store is SeaweedFS (actively maintained, single container). Production remains
Cloudflare R2. The app sees only standard S3 settings.

Open for Milestone 2: signed URLs point at `S3_ENDPOINT_URL`, which browsers
cannot reach locally. Either add a separate public endpoint setting or stream
downloads through Django after the authorization check.

## D11. Custom user model before any user reference

The email-based `accounts.User` landed with the audit app (step 2) rather than
step 4, because `AuditEvent.actor` references users and Django cannot switch
user models after such a migration exists. Users are never deleted
(`on_delete=PROTECT` from audit events; `delete()` raises).

## D12. Dedicated Cloudflare tunnel for the POC

Reusing the existing home tunnel would need a firewall hole from the home
network into the isolated VLAN, would widen the set of hosts trusted to set
client-IP headers, and could not be handed to the HOA later. The dedicated
`hoa-portal-poc` tunnel's connector runs inside the VM.

## D13. cloudflared follows the latest release

cloudflared cannot update itself inside a container, and Cloudflare stops
supporting old releases. The tunnel overlay uses `cloudflare/cloudflared:latest`;
a systemd timer on the VM pulls daily and restarts only that container when
the image changed. Trade-off accepted: the edge connector updates without
review, as it is Cloudflare's own component already trusted with all traffic.
The portal image itself is still built from reviewed code.

## D14. Authorization mechanics (step 4)

- Roles are computed from dated records on every check, never cached on the
  login, so revocation applies on the next request.
- `RoleAssignment` lives in `community` with Person, seats and offices.
- The default manager of every scoped model raises on any unscoped query,
  including reverse relations (`lot.ownerships`). We chose loud failure over a
  silently filtered default so a forgotten scope shows up in tests. Django's
  own validation and forward foreign keys still work.
- Owners see current co-owners of their lots but not past owners. Sysadmin and
  ARC roles grant no owner data by themselves (D9).
- Services enforce permissions themselves rather than trusting views; an
  explicit `SYSTEM` actor covers bootstrap and seed data, and a missing actor
  is an error.
- Board members may end their own seat (resignation) without the manage
  permission; they cannot grant themselves anything.

## D15. Sign-in mechanics (step 5)

- allauth's MFA step applies after a password *or* an emailed code; only a
  passkey sign-in skips it (a passkey is itself multi-factor). "Trust this
  browser" is off.
- `RequireMFAMiddleware`: a signed-in account without TOTP or a passkey can
  reach only the enrollment pages and sign-out. Recovery codes alone do not
  count. Applies to every account, checked on every request.
- Lockout: django-axes, 5 failures per email+IP, 30 minutes, kept in the
  shared database cache (no per-attempt rows holding emails or user agents).
  The per-email ceiling (20 per hour, any IP) is allauth's rate limit; its
  block lasts until the hour window passes rather than exactly 15 minutes.
- Self-service email change and signup URLs return 404.
- Residual gap, accepted: a new account is password-only between accepting the
  invitation and enrolling (same session, enrollment forced immediately).
- Recovery codes are shown by allauth after enrollment; we do not force a
  "I saved them" confirmation in M1.
- Authenticator-app codes from the neighboring 30-second window are accepted
  (`MFA_TOTP_TOLERANCE = 1`, RFC 6238 s.5.2). allauth's default of 0 rejected
  codes typed as the window rolled over (found on the POC).

## D16. Milestone 2 build (2026-11-25, directed overnight)

- Scope and user decisions: `docs/milestone-2-plan.md`.
- One workflow engine (`core.workflow`) for ARC, violations, records
  requests, work orders, invoices, collections and resale certificates;
  every step is an append-only WorkflowEvent plus an audit event.
- Files attached to workflow objects are append-only `documents.Attachment`
  rows using the same validation and scanning as the library, served only
  after the owning object's scoped lookup. Downloads stream through Django;
  no signed storage URLs reach the browser.
- PDFs (decision letters, notices, statements, resale certificates) come
  from a ~90-line text PDF writer (`core.pdf`) rather than a dependency.
- Ledger: one account per lot, append-only scoped entries in cents;
  prepayment is a credit balance drawn down by monthly charges; aging
  applies credits oldest-first; corrections are reversals. New owners see a
  brought-forward balance, not the previous owner's history.
- Late fees cannot be posted (LEDGER_LATE_FEES_ENABLED=false) pending the
  PA law review. Collections are tracking only.
- Formal votes freeze quorum/pass rules and their citations when opened
  and are labelled not for binding use until counsel review. Ballots are
  append-only; secret ballots hide voter choices in the UI (the database
  still links ballot to lot to enforce one vote per lot).
- Invoices: threshold rule invoice.dual_approval_cents ($500), two
  distinct board approvers at or above it, the entrant never approves.
- Resale certificate content is a draft for counsel to check against
  68 Pa.C.S. § 5407.
- Manager role added (data entry and workflow steps only).

## D17. Security review changes (2026-11)

Full findings and fixes: `docs/security-review-2026-11.md`. Decisions that
change earlier ones:

- **Invitations (changes D4/D9):** only the board creates invitations, and
  only to the email on the person's record. Sysadmins may re-send or revoke.
  A printed invitation link exists only to finish the first-officer bootstrap.
- **Enrollment window (refines D5/D15):** a login with no authenticator can
  enroll one only in the session that accepted the invitation or redeemed a
  reset code, within `MFA_ENROLLMENT_WINDOW_SECONDS` (1 hour). This closes the
  "accepted residual gap" in D15. Re-authentication never accepts an emailed
  code.
- **Owner visibility of lot history:** owners see ARC requests, violations
  and resale records from their own ownership period (plus unresolved
  notices); violations only after notice. The board sees everything.
- **Frozen decisions:** vote rules once open, invoice amount/vendor/number
  once submitted, and the approvals an invoice needs are fixed (database
  triggers). Issued resale certificates are stored, not regenerated.
- **Deployment:** production needs `TRUSTED_PROXY_CIDRS` (or
  `NO_TRUSTED_PROXY=true`); demo data only with `DEPLOYMENT_STAGE=poc`;
  web/worker refuse to run as the database owner; cloudflared has its own
  network with only web.
- **Open for the board:** a second board member to approve new seats (A3);
  whether formal secret ballots must be anonymous in the database (D12).

## Dependencies

| Package | Why |
|---|---|
| Django 5.2 LTS | framework; 5.2 is the current LTS (6.2 LTS arrives April 2027) |
| psycopg 3 | Postgres driver (also used for safe SQL composition in grants) |
| django-environ | parse `DATABASE_URL`, booleans and lists from env |
| gunicorn | WSGI server |
| whitenoise | serve static files from the same image; no separate web server |
| django-storages[s3] | S3-compatible storage (R2 / SeaweedFS); brings boto3 |
| procrastinate[django] | Postgres-backed task queue (no Redis) |
| django-csp | Content-Security-Policy headers (built into Django only from 6.0) |
| django-simple-history | change history for lots, people, ownerships, seats, offices (CLAUDE.md stack); history tables are insert-only for the runtime role |

| django-allauth[mfa] | sign-in, emailed codes, TOTP, passkeys (WebAuthn via `fido2`), recovery codes, reauthentication |
| django-axes | per email+IP login lockout |

Development only: pytest, pytest-django, pytest-cov, factory-boy, ruff, bandit,
pip-audit, pip-tools.

## D18. Accounts, account contacts and former owners (2026-11-25)

The owner asked for accounts that hold several people, belong to a lot, and go dormant on sale, and for sellers to keep a read-only login for their history. An account is one ownership period of a lot (re-recording owners, such as adding a spouse, keeps the account; a sale opens a new one). Contacts not on the deed get access from their kind (authorized, view-only, tenant), never from self-service. Former owners see only their own era: statements cut at the sale date, and their own ARC requests, notices and resale requests. This relaxes the M1 rule "ending an ownership removes access" to "removes access to everything except one's own history", which the owner approved.

## D19. PA law guidance and statutory fallbacks for settings (2026-11-25)

The owner asked for each settings section to show an example from Pennsylvania law. `community/pa_law.py` records, per setting, where the value usually comes from (Declaration, Bylaws, Rules and Regulations, board resolution), a summary of the Uniform Planned Community Act (68 Pa.C.S. Chapter 54) and a worked example. Only where the Act itself supplies a value when the documents are silent is it used as a fallback until the board records its own: meeting notice 10 days (§ 5308), quorum 20% unless the Bylaws differ (§ 5309), resale certificate within 10 days (§ 5407). Everything else stays unset until the board records it. The summaries are orientation, not legal advice, and the page says so; counsel should confirm them before real use.

## D20. Board members are homeowners first (2026-11-25)

The owner pointed out that every board member also owns a home and must use the portal as a homeowner, with board rights added on top, and keep homeowner access after leaving the board. Roles were already additive (Ownership gives the homeowner side, BoardSeat the board side); what was missing was (a) a way to load the real register so board members' own ownerships exist, now `import_register` (check, then `--commit`; matches existing people by email, so the bootstrapped first officer gets their lot), and (b) a menu that keeps the two sides apart: a Homeowner section whose lists show only your own home (`?mine=1`) and a Board section with the review lists. A board member still cannot record their own ownership in the portal (no self-grants); the import runs on the server and is audited. The Homeowner view stays read-only, for seeing another owner's view.

## D21. Owner decisions on the open items (2026-11-25)

Interviewed one item at a time; answers recorded as given.

1. **Board seats need a second board member's approval in production**, not on the POC/test environment. Implemented as a setting that defaults on outside `DEPLOYMENT_STAGE=poc` (security review A3).
2. **Malware scanning on now**: a ClamAV container on the POC, `DOCUMENT_SCANNER=clamd`, clean scan required, existing files rescanned.
3. **Gemini**: free key while data is fictional; a paid key before real signed forms. Human confirmation stays the default (earlier decision).
4. **Late fees**: the board can define them and switch on automatic posting, but it starts manual (automatic posting off by default). This supersedes "never automatic until PA review"; the PA guidance on the settings page still recommends counsel review of amount, grace period and notice.
5. **Formal votes are binding now.** The owner accepted this without waiting for counsel; the "not yet binding" labelling is removed. Recommended follow-up: counsel confirms online voting, proxies, quorum and notice under the Bylaws and 68 Pa.C.S. § 5310.
6. **Secret ballots are separated now**: "who has voted" is stored apart from the choice, so the database itself cannot link a secret ballot to a lot (security review D12).
7. **Online payments: cards and bank transfer**, through Stripe's hosted pages only (the portal never sees card or bank numbers).
8. **The owner pays a convenience fee**, shown before paying. Card-network rules cap and require disclosure of card surcharges; counsel should confirm the fee wording before live payments.

## D22. Neighbor acknowledgements come before board review (2026-11-25)

The owner asked that neighbors acknowledge an ARC request before the board reviews it, with the wording "acknowledgement" rather than "sign-off". The lot's recorded neighbors are on every request from creation and only the board can remove them; the homeowner can add other homeowners (picked by street address and the owners' names on the deed, never tenants or email addresses) or outside addresses. "Request Neighbor Acknowledgements" moves the draft to a new `neighbors` state and emails review links (portal owners a link to the review page, others a single-use link). Neighbors respond "no objection" or "object", and an online objection needs a comment. When nothing is waiting, the request moves to submitted automatically (system actor, audited) and the applicant is emailed. A neighbor who never responds is handled by the board removing them, or by the paper form. Acknowledgements still never decide a request. The kind of project is always a list: a starter list is added by migration when the board has none. Planned start is required on the owner form.

## D23. Secret ballot storage: what "unlinkable" does and does not cover (2026-11-25)

The regression review (SEC-004, BUG-005) found that Postgres itself can link a secret ballot's choice rows to its "this lot voted" row: rows written in the same transaction share a transaction id (`xmin`), and rows written one after another sit next to each other on disk and in the write-ahead log. The application never reads these, the runtime database role cannot change them, and the portal shows no way to join them, so no owner, board member, manager or sysadmin can see how a lot voted through the portal. Someone with direct database or backup access and Postgres knowledge could. D21's "stored unlinkable" therefore means "unlinkable through the application and its data model", not "unlinkable to a database administrator". Closing the gap fully needs a mixing step (choices held and written in shuffled batches by a separate process) or ballots counted by a third party; both are larger designs that need the owner's and counsel's view on whether the residual risk matters for a 44-lot community with board-held backups. Until then: database and backup access stays with the sysadmin and the two-person rules, and this limitation is disclosed to the board.

Also from the same review: outside the POC, a board seat always needs a second board member's approval regardless of the `BOARD_SEAT_SECOND_APPROVAL` environment variable; elections fix the ballot once voting opens (database trigger); a proxy counts only while its grantor still owns the lot; neighbors see an ARC request only after "Request Neighbor Acknowledgements" and only while it is open.

## D24. No anonymous access; personal vote links (2026-11-25)

The owner ruled that nothing in the portal is reachable without signing in: "anything that is emailed out, that drives the recipient to the portal (ARC review, voting, etc) must be authenticated - they don't have to reauthenticate if they have a valid session open, but there can't be anonymous access to any part of the portal." The neighbor acknowledgement single-use links (Milestone 3 phase 7) therefore no longer show anything to an anonymous visitor: neighbors with a login get a link to their signed-in review page; neighbors without one, and addresses outside the community, use the printed form. Links emailed earlier ask for sign-in and then send only the neighboring lot's owner on to their page. The only pages open without sign-in are the sign-in steps themselves (login, accepting an invitation, redeeming a reset code), the health probe and the login background image.

For votes: "if we're logging people's votes, I want to be able to ensure it's them actually voting. Those kinds of votes need to be single use, unique links, but also authenticated as well. I don't want to be able to forward a vote to someone else." Vote and election emails carry a personal link for that person and that ballot (only its hash is stored); it needs sign-in, opens only for the person it was issued to, and stops working once used or when voting closes. Casting a formal vote or an election ballot needs re-authentication within the last few minutes (password or passkey, never an emailed code).

## D25. Updates from System status, and a lighter, faster deploy (2026-11-25)

The owner asked for "update available" on System status and a button to install it without terminal access. The web app gets no access to the VM: a request row in the database (`core.UpdateStatus`, set only by a sysadmin with MFA, audited) is the whole interface. A host-side agent (`infra/poc/portal-agent.sh`, systemd timer every minute, installed by `start-portal.sh`) checks GitHub every 15 minutes or on "Check now", records how many commits the tracked branch is behind, and on "Install update" runs `update-portal.sh` and records the result. Values it writes back (commit subjects) go through psql variables, never shell-built SQL. Only the tracked branch is ever installed (on the POC that should be `main`, which only receives CI-green merges), so the button takes no input.

From the optimization review, accepted without a decision because they change no rule: roles remembered per request (cleared whenever anything that grants a role changes); the two-person coverage check in four queries; gunicorn 2 processes x 4 threads with preload; bytecode compiled into the image; finished background jobs removed after two weeks and worker polling once a minute; container logs rotated; memory caps on web, worker and the file store; Postgres sized for a small VM; updates no longer stop every container (Postgres, the file store, the scanner and the tunnel keep running), base images refreshed weekly instead of every update, documentation-only changes skip the restart, migrations skipped when none are pending; CI runs once per PR commit with cached dependencies and a non-durable throwaway database. Left for the owner: ClamAV memory (on-demand or off-box scanning conflicts with D21's "scanning on"), building images in CI and pulling them on the VM, and R2 instead of SeaweedFS on the POC.

## D26. Malware scanner reloads signatures in place (2026-11-25)

ClamAV holds its signature database in memory (~1.2 GB) and, by default, loads a fresh copy alongside the old one at each signature update (~2.4 GB for about a minute), close to the 4 GB the POC VM has. The owner chose "reload in place": `ConcurrentDatabaseReload no` (`docker/clamav/clamd.conf`, the image's own defaults plus this one line, checked with `clamconf`). Peak memory stays near one copy; scanning pauses for about a minute during each reload, and files uploaded in that window wait as "not yet scanned" until it is back (DOCUMENT_REQUIRE_CLEAN_SCAN keeps them from being downloaded meanwhile). Scanning stays on (D21).

## D27. Owner's answers on the open items (2026-11-25)

1. Secret ballots: the residual risk in D23 (linkable only with direct database or backup access) is accepted; no mixing step.
2. Online payments stay a placeholder until the HOA's Stripe account and live keys exist.
3. Counsel review (online voting, proxies, quorum, collection letters, resale certificate, convenience-fee wording): the owner is arranging it.
4. SMS stays dormant until a provider account exists.
5. Former owners' contact details: cleared two years after their last ownership or contact role ends (a board setting).
6. Common-area reservations: dormant (feature switch off) until areas and rules are set.
7. Production hosting and HOA-owned accounts: to be decided.
8. Building images in CI and pulling them on the VM: only if faster overall (see the note below); R2 for the POC's files: under consideration.

Note on 8: with the layer cache the VM rebuilds a code-only change in well under a minute and a docs-only change in about a second, and a dependency change in two to three minutes. Pulling a CI-built image would save at most a minute or two on dependency changes, add the CI build time before an update is available, and need a registry token stored on the VM. Not faster overall, so not done.

## D28. Retention clears current contact details; the change history persists (2026-11-25)

The retention rule (two years after someone's last ownership or contact role ends) clears the current phone, mailing address, extra contact methods and directory choices. The owner decided the change history persists: earlier values remain in the model history tables, which are part of the audit trail (D8), and no purge command exists. The retention page says so. Also accepted as proposed: managers may confirm and decline common-area bookings (a booking is not a governance decision); photo thumbnails are logged as downloads but marked `view: thumbnail`; the sign-in help contact field stays empty unless filled with an association contact; adding a passkey also enables passkey-only sign-in (a passkey is itself strong authentication); the neighbor response period (`arc.signoff_respond_days`) is set by the board, suggested 14 days.

## D29. POC files stay on the local store (2026-11-26)

The owner decided to keep the POC's uploaded files on the local SeaweedFS store for now rather than move them to Cloudflare R2. Nothing changes on the VM. `infra/poc/move-files-to-r2.sh` stays available for a later move; production still plans R2 (CLAUDE.md stack).

## D30. Prepaid owners: future months show unpaid, prepayment drawn down monthly (2026-11-26)

The owner ruled that a prepaid owner's future months are listed as not yet paid, and the prepayment is drawn down as each month's dues post (the existing drawdown). The regression finding BUG-005 is fixed only for its other half: a month whose dues were posted early is not also counted as an upcoming charge.

## D31. Off-site backups in a locked R2 bucket (2026-11-26)

The owner chose Cloudflare R2 for immutable off-site backups. The nightly backups are already encrypted and carry a keyed checksum before they leave the VM. They go to a separate R2 bucket (not the files bucket) whose bucket lock rule makes everything under `backups/` undeletable and unoverwritable for 90 days, even with valid keys, so a compromised VM or a stolen token cannot destroy them. A lifecycle rule removes backups after 365 days, which keeps cost low and keeps cleared contact details (D28) from living on much longer in backups than in the portal. R2 tokens cannot be write-only, so the lock is what makes the VM's read-and-write token safe. `infra/poc/setup-offsite-backups.sh` proves the lock by trying to delete a probe file (`check_offsite_lock`, audited). The health check alerts when no local or off-site backup has arrived for 36 hours, and `restore-drill.sh --from-offsite` proves the off-site copy restores. The backup key stays offline and outside the Cloudflare account.

## D32-packets. Meeting packets: who builds them, what owners see (2026-11-26)

The board and the manager may build packets (`meeting.packet_build`), following the existing policy for meeting preparation (`meeting.manage` and `document.manage` both include the manager); building approves nothing. The board packet is filed in a board-only folder ("Meeting packets (board)", visible to board, manager and auditor like other board documents). The owner packet is filed in the owner-visible "Meeting packets" folder, so building it publishes it at once (the button says so); executive sessions get a board packet only. Owner packets leave out receivables aging entirely (even totals), and every collection, violation or delinquency figure. "Approved minutes" means the minutes the board attached to the previous held meeting of the same kind, when owners may see that document; the board packet's draft minutes are chosen from the Minutes documents. Attachments are library documents chosen when building (owner-wide only for the owner packet); minutes and attachments are listed in the index and bundled in the .zip, never copied into the PDF. The reserve figure is the board's own entry on the community profile (the one used on resale certificates), and operating cash is the cash book balance. Owners get the latest build only; the board keeps every version.
## D32-surveys. Surveys: informal, frozen once open, anonymous like secret ballots (2026-11-26)

Surveys sit on the Voting hub next to polls and votes and are never binding. The board drafts them; opening fixes the questions and the survey's details (service, and database triggers as the backstop). Only "share the totals with owners after it closes" can still change, because it is a display choice; written answers are never shown to owners. One response per person (owners, or on a "residents" survey also authorized and view-only account contacts and tenants; former owners never). On an anonymous survey the answers sit under a response row with no person, no time and a random id, and a separate "has responded" row names the person with the day only; the D23 limitation (database-level linkage through transaction ids and row order) applies here as it does to secret ballots. Who has responded is recorded on every survey (as with secret ballots); the pages show only the count. Owner decision welcome on whether view-only contacts should be asked on residents surveys (they are today, since answering is an opinion, not an action on the account).
## D32-search. Portal search shows only what each person can already open (2026-11-26)

One search box covers the portal. Search decides nothing about access itself: each area's provider (`<app>/search.py`) checks the same permission as that area's list page and builds its rows from the same scoped query, so owner-era limits, ARC recusal, internal thread notes, tenant and view-only limits and switched-off features carry over unchanged. Deliberately not searched for now: ARC deliberations (reviewers' positions), owners' written responses to notices, uploaded file contents, votes and elections, committees, contracts, resale certificates and board tasks; each can be added later as a provider with its own cross-owner test. Former owners find their own-era ARC requests and notices as links to My history (their only page). The query text is never logged, audited or stored, and the results page is not cached.
## D32-rsvp. Event RSVPs: guests per person, answers close at the deadline, names for the board only (2026-11-26)

Proposed, for the owner to confirm. RSVPs and volunteer sign-ups are not legal records, so answers are changed in place with history (no append-only). The guest limit is per person answering, not per household: a household is not a single record (owners, contacts and tenants each answer for themselves), and per person is simpler to explain. The headcount cap counts people going plus their guests. At the deadline (default: when the event starts) every change closes, including switching to Not going, so the board's final list is stable. Only the board sees names, guests and the CSV export (`event.rsvp_names`); the manager sets events up and sees the same counts owners see. Former owners, view-only contacts and the manager role cannot answer.
## D32-bankfeeds. Bank feed through Teller's free tier, with statement files as the fallback (2026-11-26)

The owner wanted the portal connected to the HOA's bank only if it costs nothing. Options: Plaid (the best bank coverage, but paid per connected account per month after a limited trial), SimpleFIN Bridge (cheap at about $15 a year, but not free and a third party in between), and Teller (developer tier free up to 100 live connections; the HOA needs one to three). We built a **Teller** provider plus a universal free fallback: **statement files** (OFX/QFX, and CSV with a column-mapping step) that every US bank offers, so a bank Teller does not cover still works. Both feed the same append-only `BankTransaction` table (unique per account by the bank's id, or a hash for CSV, so re-imports add nothing twice) and the same matching, which only suggests: a person confirms each match, confirmed matches are pre-ticked on the existing monthly reconciliation, and nothing is posted to the ledger automatically.

Safeguards: the portal never stores full account or routing numbers (last four digits only, also enforced by a database check); Teller's access token is encrypted at rest with Fernet under a key derived from `BANK_FEED_TOKEN_KEY` (connecting is refused while it is unset) and is deleted on disconnect; Teller is called only at `api.teller.io`, with mutual TLS from the dashboard certificate (written to a private temporary file only long enough for Python's TLS library to load it), timeouts, no redirects, and errors reported by status code only. Teller Connect's script and frames are allowed by the Content-Security-Policy on the one "Connect a bank" page only (`csp_update` on that view), never site-wide; connecting and disconnecting are board-only and need a fresh password or passkey check; the manager can sync, import files and match. Audit events carry ids and counts only, never descriptions. The feature is off by default.
## D32-payments. Open-item accounting and Stripe online payments (2026-11-26)

Owner decisions of 2026-11-26, superseding the fee part of D21:

1. **Online payments by Stripe**, built and tested in Stripe test mode now; live keys come later from the HOA's own Stripe account. Only the server's `.env` holds a key (entered at a hidden prompt, `infra/poc/set-stripe-key.sh`); tests use a fake; no key is ever in the repository.
2. **A surcharge on credit cards only.** Bank (ACH) and debit or prepaid card payments carry no fee; the HOA absorbs their cost (card-network rules forbid surcharging debit and prepaid cards and cap credit surcharges at 3%). The rule `payments.credit_card_surcharge_bp` (default 300, hard cap 300 in code and in the rule) replaces `payments.card_fee_bp` and `payments.bank_fee_cents`, whose recorded values stay in the rule history and are no longer used.
3. **The portal is the HOA's accounting system for owner accounts**: every charge (dues, late fees, other charges, resale fees) has its own number ("Invoice 2026-0142"; dues read "Dues – March 2026") and open balance, and payments are applied to specific charges.

How it was built (choices the board should know):

- **Applications are append-only** (`ledger.PaymentApplication`, model + trigger + runtime role); undoing one is a negative application. The order is the governing rule `ledger.assessments_before_fees` (off = oldest charge first; on = dues first). A payer may direct a payment to chosen charges; the rest follows the rule; leftover money stays as credit and is applied as the next charge posts, so prepaid owners' future months show unpaid and the credit draws down monthly (D30 unchanged). A reversal undoes the applications of what it reverses. Balances are unchanged: open charges minus unapplied credit equals the old balance for every lot (tested on the seeded community). Migration `ledger 0009` rebuilt applications for existing entries by replaying history in posting order.
- **Pull, never push**: no Stripe webhook (D24 forbids anonymous endpoints). The return to the success page is only a hint; the server always asks api.stripe.com. Every checkout is bound to a `PendingCheckout` created before the redirect (random 32-byte nonce, lot, account, person, charges, amounts, test/live mode, 24-hour expiry). A payment is recorded only when the fetched session, PaymentIntent and charge all match (ids, metadata, amount, currency, livemode, succeeded and captured); otherwise nothing is recorded, `payments.mismatch` is audited and the board gets a My item. Recording is idempotent (unique PaymentIntent id; the checkout is consumed under a row lock). Online payments are created only by the Stripe sync: `method="online"` is refused on every manual path.
- **Mode safety**: test keys are refused when `DEPLOYMENT_STAGE=production` (else test cards could "pay" real accounts); live keys are refused unless `DEPLOYMENT_STAGE=production` and `PAYMENTS_LIVE_ALLOWED=true`; every fetched object's `livemode` is checked.
- **Least-privilege restricted key** (runbook): Checkout Sessions write; PaymentIntents, Charges, Balance transactions, Payouts, Disputes and Refunds read only. The portal therefore cannot refund. If a non-credit card paid a surcharge (Checkout's funding filter, `payment_method_options[card][restrictions][funding_types_blocked]` in API version 2026-08-26.dahlia, normally prevents it), the board gets "Refund $X surcharge to lot N (debit card)" and the poller closes it when the refund appears. A credit card on a no-fee session is absorbed and only counted. Link is hidden on card sessions; Apple Pay and Google Pay cannot be turned off per session (account setting), and the same safety net covers them.
- **Accounting**: the owner's payment is the base amount, applied to the chosen charges; the surcharge is not an owner charge but card surcharge income in the journal export, Stripe's fee an expense, payouts move Stripe clearing to the bank. Payouts are copied daily (append-only) and offered for bank matching through `payments.sync.payouts_for_matching(start, end)`.
- **Three-way reconciliation** daily: ledger online payments against Stripe's balance lines and payouts (and, once wired, the bank deposits: hook `payments.sync.check_payouts_against_bank`). Any difference fails the "Online payments" health check, emails the board and sysadmins, and gives the treasurer a My item.
- Still needed before live payments: the HOA-owned Stripe account (EIN, bank account), Visa/Mastercard surcharge registration 30 days ahead, counsel-approved disclosure wording, and the go-live checklist in the runbook.

## D32-commstream. Community pages streamlined (2026-11-26)

Owner-approved streamlining. Choices the board should know:

- **Board and committees** is a new page for everyone who sees the community pages (new permission `board_committees.view`; former owners and sysadmin-only logins do not). It shows board members' names and offices and each active committee's name, purpose and members' names and roles. Committee names and members were board-and-member only before; they are now community-wide by this decision. Topics, tasks, files and every contact detail stay private, whatever the directory settings are.
- **View-only contacts** see the account's requests read-only and can never post, withdraw, add photos or raise one, even when they own another home (the thread rule now checks the request's own lot).
- **Former owners** no longer have "My requests" (`request.view_own` is residents only); their requests from their time are listed in My history and open read-only.
- **Budget and reserves** is on while either the "Budget for owners" or the "Reserve plan" feature is on and shows only the parts switched on.
- **Common areas** are edited together with their booking rules (both board only, saved together or not at all); Community settings keeps the "Add a common area" form so areas exist even with bookings off.
- **Urgent texts** can go out from the announcement form (board, with the "sms" feature on); the text is checked before anything is published, and it links to the new announcement.
## D33-acctfix. Fixes from the accounting QA pass (2026-11-26)

An independent QA pass (`ledger/tests/test_accounting_scenarios.py`, `payments/tests/test_payment_scenarios.py`) found six bugs; each is fixed in the product code, and the scenarios are ordinary tests again. Choices the board should know:

**D33-acctfix-1. Money belongs to the ownership account it was posted under.** The ledger was per lot, so after a sale a seller's unapplied credit paid the buyer's dues. Every ledger entry now carries its `community.Account` (one ownership period), set when it is posted from its posting date: the account whose period covers that date (the sale date belongs to the buyers), a reversal or recorded refund follows the entry it corrects, an entry dated before the lot's first account belongs to that first account, one dated between two owners to the next owner's, and a lot that never had an owner keeps its entries without an account. A payment or credit is applied only to charges of the same account (`ledger/allocation.py`, `ledger/accounts.py`). After a sale the seller's credit stays as credit on the old, dormant account (the lot page shows "Credit owed to former owner"; the former owner sees it read-only on My history as "Credit owed to you", with any later entry on that dormant account, such as the board's refund), and a seller's unpaid charges stay theirs ("Still owed by former owner"); the buyers start clean. This replaces the earlier "balance brought forward" from the previous owner. Lot-level views add up the accounts (lot balance = sum of its accounts; open charges - unapplied credit == balance per account and per lot, checked by the QA invariants). Owner pages, the owner's statement, online payment amounts, the resale certificate, delinquency letters and courtesy reminders use the current owners' account; the board's lot page and the delinquency list show the whole lot. When a lot has had more than one owner, the board's payment, charge and credit forms offer the account (for example a refund check for a former owner's credit, posted as a charge on the old account, or a seller's arrears paid at settlement). Migration `ledger.0015_entry_accounts` assigns existing entries by date (only the new column is written, with the append-only trigger lifted for that backfill) and undoes any money already applied across two accounts with new append-only application rows. For the board and counsel: an unpaid assessment is a lien on the unit (68 Pa.C.S. § 5315), but the portal never moves a seller's debt to the buyer by itself; collecting it from the buyer would be a board decision recorded as a new charge.

**D33-acctfix-2. A late fee needs that month's dues still open.** `late_fee_candidates` tested the whole lot balance, so a lot that paid the month's dues on time but owed something else (even a charge not yet due) got a late fee. It now checks the open amount of the month's own assessment on the day the fees are posted, after the grace period of the late-fee rule; a prepayment that paid the dues means they are not late. The late list shows what is still open of that month's dues.

**D33-acctfix-3. Views as of a past date use only what had happened by then.** Each payment application now records `effective_on`, the posting date of the entry whose posting made it (the local date when credit is applied by hand). Aging and past-due amounts as of a date count only entries posted by then and applications in effect by then, so a check that bounced later still shows as paid on an earlier date. Migration `ledger.0012_application_effective_on` dates existing rows from the history: an undo row by the reversal that made it, any other row by the later of its two entries (an application made when a reversal freed money is dated by its own two entries, the closest the old rows allow).

**D33-acctfix-4. Every delinquency notice states the past-due amount.** Past due is the open amount of charges due before the day (open items, as of that day), not the balance netted against later charges or payments. The `{amount}` placeholder in every letter (the case letter sequence, the lot page's notices and the statement message) is that amount; the attached statement still shows the full balance under "Pay this amount".

**D33-acctfix-5. Refunds: the surcharge part in proportion, the payment part on the account.** A refund at Stripe of R from an online payment of T (base B + surcharge S) is split: the surcharge part is in proportion (card-network rules require a partial refund to return the surcharge pro rata), computed on the running total so the parts add up exactly: after refunds totalling R, round(R x S / T) of the surcharge has been refunded, all of it once R reaches T. The one exception is the debit-card safety net (D32-payments): a refund of exactly the surcharge owed back is all surcharge. The surcharge part is booked against surcharge income in the journal by itself (`payments.services.refund_portions`). The payment part reaches the books from the owner's account: either the board reverses the payment (a full refund; it also reopens the charges it paid), or presses "Record on the account" on the refund item, which posts a refund entry (`EntryKind.REFUND`, positive: the owner has the money back) booked against Stripe clearing. The portal still cannot refund (its key has no refund rights). A payment with a recorded refund cannot also be reversed, and the other way round, so a refund never counts twice. Surcharge-first was not chosen: it would return the whole surcharge on a small partial refund, which the card rules do not ask and which understates the surcharge income kept.

**D33-acctfix-6. Stripe payouts count in the bank reconciliation.** Online payments reach the bank only in Stripe payouts, never in deposit batches, so the cash book ignored them. A payout now counts as a deposit on its arrival date (`book_balance`, `uncleared`), is suggested for a bank-feed line of its exact amount within three days, and is ticked on the reconciliation page when Stripe reports it arrived or it is matched to a bank line; the board unticks one the statement does not show yet. Recorded as a cleared item of kind "payout" (migration `ledger.0013`, choices only). Online payments are never in deposit batches, so nothing is counted twice.

## D34-fixinfra. Fixes from the November regression: bank feed, outbound calls, host and backups (2026-11-26)

**D34-fixinfra-1. Account numbers are masked in bank-line descriptions, also in lines already stored.** Banks often put the other party's full account and routing numbers in a wire or ACH memo, and the portal must never hold bank account numbers (CLAUDE.md, D32-bankfeeds). Every bank-line description (Teller, OFX/QFX, CSV, the CSV preview, and again when lines are stored) now has each run of six or more digits (spaces or dashes allowed between them) replaced by "••••" and the last four digits (`ledger.bankfeeds.base.redact_numbers`). Kept as written: amounts (followed by cents, or after "$"), dates (6 or 8 digits that read as a date) and short numbers such as check numbers (five digits or fewer). When unsure it masks: losing part of a reference is better than keeping a number. Bank lines are append-only, but a stored account number would otherwise stay forever, in backups too. Removing data the app must never hold outranks the immutability of a free-text memo, so migration `ledger.0016_redact_bank_descriptions` masks existing descriptions in place: one UPDATE, run by the owner role that applies migrations, with the append-only row trigger lifted for that statement only (the pattern of governance 0005 and ledger 0012). Only `description` changes, and only on rows where masking changes it; amounts, dates and bank ids stay. One consequence: a CSV file (or an OFX file without FITIDs) imported before this fix whose lines carried such numbers would import those lines once more if the same file were imported again, because the line's id is computed from the masked text; ids are never computed from the unmasked number, since a hash of a description whose last four digits are shown could be reversed. Lines with a bank id (Teller, OFX with FITID) are not affected.

**D34-fixinfra-2. Teller's Connect script stays unpinned: accepted residual risk.** The regression (SEC-005, R2-008) asked for the script to be pinned with a version and a Subresource Integrity hash. Teller documents only the unversioned `https://cdn.teller.io/connect/connect.js` and publishes no hash (checked 2026-11-26: Teller's Connect guide at teller.io/docs/guides/connect and a web search found no versioned path, npm package or hash; teller.io itself could not be fetched from the build environment). A hash computed from today's file would break the page whenever Teller updates it, and we do not invent one. So the one-page exception stays, narrowed: the CSP admits scripts from `https://cdn.teller.io/connect/` only (not the whole CDN host), the script tag sends no referrer, and the page still needs a fresh password or passkey check and is board-only. The remaining risk (a compromised Teller CDN could act as the board member on that page) is accepted as vendor trust, like Teller's handling of the bank sign-in itself; the runbook says how to limit it and to pin the script if Teller ever publishes a versioned URL or hash. Not done now: moving Connect into a separate origin or sandboxed popup with no session, which needs a second hostname and more set-up than the POC justifies; worth revisiting before production.

**D34-fixinfra-3. Host alerts must reach someone before a change is accepted; Falco's key is pinned.** The nightly integrity check accepts the new AIDE baseline, and the audit checkpoint and Falco offset move on, only after `host_alert` delivered the report to at least one sysadmin; `host_alert` now exits with an error when it reached nobody (no sysadmin email, or every send failed), and the timer then shows as failed on System status and sends the same report again next run (R2-006). Falco's apt key is trusted only with the fingerprint Falco publishes on its packages page (`478B2FBBC75F4237B731DA4365106822B35B1B1F`, checked 2026-11-26), and only that key goes into the keyring; a rotation needs a person to confirm the new fingerprint (`HOA_FALCO_KEY_FPR`) (R2-009). The off-site restore drill refuses a backup whose signed stamp is not the one in its name or lies in the future (R2-002). Not done: giving the app containers a write-only off-site token, which needs a change to the R2 token set-up.
