# Oak Creek Farms HOA Portal

A secure web portal for a 44-lot Pennsylvania planned community (governed by the PA Uniform Planned Community Act and the community's recorded Declaration). Owners log in to see their lot, documents, and (later) account and payments. Board members and the Architectural Review Committee (ARC) run reviews, records, meetings, and (later) financial reports.

This file is the source of truth for architecture and rules. If a task conflicts with it, stop and ask rather than deviating. Update this file when a decision changes. The reasoning behind each decision is recorded in `docs/decisions.md`.

## Scope

- In scope for the POC (Milestones 1-3): accounts and MFA, lots and ownership, role-scoped access, audit log, document library, architectural review workflow, announcements.
- Later (not now): owner ledger and Stripe payments, board financial reports, meetings and resolutions, violations, records requests, resale certificates.
- Out of scope: native mobile apps, multi-association tenancy, anything that stores card or bank numbers.

## Stack (decided)

- Python 3.12+ (image uses 3.13), Django 5.2 LTS, PostgreSQL 16+.
- Server-rendered templates with HTMX. No SPA, no separate JSON API unless a later milestone needs one.
- django-allauth for auth, with MFA (TOTP and WebAuthn passkeys). django-axes for login throttling. django-simple-history for model change history.
- File storage through django-storages to an S3-compatible bucket (Cloudflare R2 in production, SeaweedFS locally; MinIO no longer publishes images). Never write uploads to local disk.
- Postgres-backed background task queue, no Redis: Procrastinate.
- pytest + pytest-django, ruff, bandit, pip-audit. GitHub Actions CI.
- Docker. One image runs web, worker, and the one-shot migrate step. Docker Compose for local and homelab.
- Two Postgres roles: an owner role used only by migrations, and a runtime app role with no DDL, no TRUNCATE, and no UPDATE/DELETE on append-only tables. Web and worker never receive owner credentials.

## Architecture rules (non-negotiable)

1. Modular monolith. Apps: `accounts`, `community`, `documents`, `arc`, `comms`, `audit`; later `ledger`, `payments`, `governance`, `reports`. Apps call each other through service functions in `services.py`, never by importing another app's models for writes.
2. All configuration comes from environment variables (12-factor). No host-specific paths, hostnames, or secrets in code. Provide `.env.example`.
3. Portable by design: the same image must run on a Proxmox VM, a VPS, or a PaaS with only environment changes.
4. Single-tenant. No tenancy columns.
5. Anything financial, legal, or audit-related is append-only: ledger entries, ARC decisions, votes, audit events. Corrections are new reversing records, never edits or deletes. Enforce at the model layer (no update/delete paths), with a DB trigger, and by revoking UPDATE/DELETE/TRUNCATE from the runtime role (register the table with the append-only helper).
6. Rules that come from the Declaration (review deadlines, required submittals, voting thresholds, fee amounts) are data/configuration, not hard-coded, and each carries a citation field (e.g. "Declaration §X.Y").
7. Money is integer cents. Never floats.

## Authorization (critical)

- Owner access derives from Ownership. Board membership derives from an active `BoardSeat` (declarant_appointed or owner_elected, with term dates; ends only when `ended_on` is recorded). Offices (president, vice_president, treasurer, secretary) are separate `OfficeHolding` records and require an active seat. Other roles (arc_reviewer, auditor, sysadmin) are dated `RoleAssignment` records; ARC appointments record who appointed them.
- `Person` (community identity) is separate from `User` (login). Ownerships, seats, offices and roles attach to Person.
- Permissions live in one map in `core/policy.py`. For now every board member can perform every board action. Every action must remain performable by at least two people with MFA; the app warns when it is not.
- No self-grants. Granting sysadmin needs a nominator and a different approving board member; revoking needs one. Invitations grant nothing: they only link a login to an existing Person.
- Owner access derives from an active `Ownership` record (person to lot, with start and end dates). Ending an ownership removes access automatically; history stays on the lot.
- Every model that belongs to a lot or person must be reachable only through a scoped manager/queryset (e.g. `Model.objects.for_user(user)`). The default manager must not return unscoped data to owner-facing views. Views must never fetch by bare primary key without scoping.
- Every feature ships with cross-owner access tests: owner A must get 403/404 for owner B's lot, documents, requests, and files, on every URL and download path.
- ARC recusal: a reviewer who is the applicant, or whose lot is affected, cannot view deliberations or vote on that request. Enforced in the service layer and tested.
- Sysadmin actions are audited and visible to the Board. Sysadmins get operations pages only, no owner data by virtue of the role. No Django admin in production. Break-glass management commands require `--actor` and `--reason` and are audited.

## Security rules

- Invite-only accounts tied to a lot or board role. No open signup.
- Strong authentication is mandatory for every account, owners included: a passkey, or a password or emailed sign-in code followed by TOTP or a passkey. Never password-only. Email codes never count as a second factor.
- Lost authenticators: two-board-member assisted reset plus a single-use enrollment code handed over out of band. No self-service bypass.
- Passwords: 12+ characters, no composition rules, common-password and breached-password (HIBP k-anonymity, fail-open) checks.
- Secure cookies, CSRF, HSTS, CSP, strict `ALLOWED_HOSTS`, `DEBUG=False` outside local dev.
- File uploads: type allowlist, size limits, malware scan hook, stored in a private bucket, served only via short-lived signed URLs after an authorization check. Published document versions are immutable.
- No secrets, tokens, or real personal data in the repository, fixtures, logs, or error reports. Local and POC data is synthetic only.
- Log structured events without personal data. Do not log request bodies, exception messages, or URLs carrying tokens (gunicorn access logs are off).
- Proxy headers (client IP, scheme) are trusted only from `TRUSTED_PROXY_CIDRS`.
- The application never handles card or bank account numbers. (Later: Stripe hosted Checkout only.)
- Treat all user input and uploaded files as hostile.

## Audit

`audit.AuditEvent` records who did what to which object, when, and from where, for logins, MFA changes, role changes, document views and downloads, ARC actions, exports, and sysadmin access. Append-only. Provide a board-visible view of sysadmin activity.

- IP addresses live in a separate table purged after `AUDIT_IP_RETENTION_DAYS` (the purge is itself audited); the core event is permanent.
- Failed logins for unknown emails store a keyed hash (`AUDIT_HASH_KEY`), never the address. No user-agent strings. Event metadata is checked against a per-action field allowlist.

## ARC workflow (Milestone 3)

States: draft, submitted, completeness_check, in_review, decided (approved / approved_with_conditions / denied), appeal, completed. Required fields and deadlines are configuration derived from the Declaration, each with a section citation. Track the review clock (including any deemed-approval rule). Generate a decision letter PDF that cites the governing section. Keep every version of every submitted document.

## Testing and quality

- Every change includes tests. Authorization, ownership-transfer, recusal, and audit behaviors are mandatory test targets.
- CI must pass: ruff, tests, bandit, pip-audit.
- Prefer small, reviewable commits with clear messages. Do not commit generated secrets.
- Add a migration for every model change; never edit applied migrations.

## Commands

(Fill in as they are created; keep this section accurate.)

- First time: `cp .env.example .env`
- Run locally: `docker compose up` (site http://localhost:8000, mail http://localhost:8025)
- Tests (needs Postgres with the two roles from `docker/postgres/init-roles.sh`): `pytest`
- Lint: `ruff check . && ruff format --check .`
- Security: `bandit -q -c pyproject.toml -r .` and `pip-audit --strict -r requirements/base.txt -r requirements/dev.txt`
- Update dependencies: edit `requirements/*.in`, then `pip-compile --generate-hashes --allow-unsafe -o requirements/base.txt requirements/base.in` (and the same for dev)
- Re-apply runtime DB grants: `python manage.py apply_db_grants`
- Seed synthetic data: `python manage.py seed_demo` (planned, Milestone 1 step 9)

## Never

- Never store or request real owner personal data, real Stripe keys, or production secrets in this environment.
- Never disable authorization, MFA, or audit logging to make a test or demo pass.
- Never edit or delete financial, decision, vote, or audit records in place.
- Never weaken a security setting without explaining the trade-off and getting approval first.
- Never expose Postgres or the object store to the public internet.

## Deployment targets

- POC: a Proxmox VM on an isolated VLAN, exposed only through a Cloudflare Tunnel (cloudflared in compose, web publishes no port), with a dev subdomain behind Cloudflare Access. No LAN access mode.
- Production: managed host or hardened VPS, behind Cloudflare, with the HOA owning all accounts.
- A full restore-and-migrate drill to a scratch environment must be done before any real data or live payments.
