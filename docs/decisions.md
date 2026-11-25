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
- Authenticator-app codes from the neighbouring 30-second window are accepted
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
