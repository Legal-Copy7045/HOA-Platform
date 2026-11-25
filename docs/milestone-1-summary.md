# Milestone 1 summary: secure foundation

Branch `claude/ecstatic-bell-cnrpzi`. 321 automated tests; ruff, bandit,
pip-audit and the missing-migration check run in CI and in
`scripts/check.sh`. The full sign-in journey was also verified in a real
browser (Chromium) against the Docker stack, with no Content-Security-Policy
or JavaScript errors.

## What exists

**Skeleton.** Django 5.2 LTS on Python 3.13; settings split into
base/local/production/test, all from environment variables; production
settings refuse to start when unsafe (debug on, weak secret, wildcard hosts,
insecure cookies, admin enabled, missing runtime role, catch-all proxy
trust). One Docker image runs web, worker and the one-shot migrate step as a
non-root user on a read-only filesystem. Compose for local use, plus a tunnel
overlay for the POC. GitHub Actions CI and Dependabot (held to Django 5.2).

**Database roles.** Migrations run as an owner role; web and worker connect as
an app role with no DDL and no TRUNCATE. The owner credentials never reach the
web or worker containers.

**Audit.** Append-only `AuditEvent`, enforced three ways: the model refuses
changes, a trigger rejects UPDATE/DELETE/TRUNCATE even for the owner role,
and the app role lacks those privileges (proven by tests that connect as
that role). History tables are insert-only too. IP addresses sit in a
separate table purged daily after 365 days; unknown login emails are stored
only as a keyed hash; metadata is restricted per action. Events cover
sign-in, sign-out, failures, lockouts, MFA changes, password changes,
invitations, every register change, the two-person flows, every sysadmin
request and break-glass commands.

**Community register.** Lots, people (separate from logins), ownership,
board seats (declarant-appointed or owner-elected, with terms), offices and
roles (ARC reviewer, auditor, sysadmin), all dated, never deleted, with
change history. Ending a record with today's date removes access on the next
request. Database constraints prevent overlapping ownership for the same
person and lot, two seats at once, and two holders of one office.

**Authorization.** One permission map (`core/policy.py`); roles are computed
from the records on every check. A reusable scoped-manager pattern
(`core/scoping.py`) makes every unscoped query on a lot- or person-owned
model raise an error, including reverse relations. Services check
permission themselves. No self-grants.

**Sign-in.** Invite-only. Every account needs a passkey or an authenticator
app; password-only and emailed-code-only sign-in are both impossible.
Emailed sign-in codes replace the password, never the second factor.
Lockout after 5 failures per email and address, plus a per-email ceiling.
Breached-password check (HIBP k-anonymity, fail-open). Assisted MFA reset
needs two board members and an out-of-band enrollment code.

**Two-person rules.** Granting sysadmin needs a nominator and a different
approving board member; revoking needs one. The board is emailed (without
names) at each step. The board page warns while fewer than two people with
MFA can perform any action.

**Pages.** My lot (owners), All lots and lot detail (board, auditor; owners
see only their own lot), board dashboard, people, person detail (ownership,
seats, offices, roles, invitations, resets), transfers, approvals, sysadmin
activity, system status (sysadmins: health only, no owner data).

**Commands.** `bootstrap_first_officer`, `seed_demo` (44 fictional lots, two
joint-owned lots, one transferred lot, board, ARC, auditor),
`check_coverage`, `apply_db_grants`, and break-glass commands that need a
sysadmin actor and a reason.

**POC infrastructure.** VLAN 50 (isolated), Proxmox VM 107 created and
hardened by script, Docker, dedicated Cloudflare tunnel with the connector
inside the VM, Cloudflare Access in front of `portal-dev.oakcreekhoa.com`,
cloudflared updated daily by a timer.

## Choices made along the way, and why

All recorded in `docs/decisions.md` (D1-D15). The ones that changed the
original brief or plan:

- **MFA mandatory for owners too** (D5), replacing "optional for owners".
- **SeaweedFS instead of MinIO** locally (D10): MinIO stopped publishing
  images.
- **Two database roles in Milestone 1** rather than later (D8).
- **Custom user model in step 2** rather than step 4 (D11): audit records
  reference users.
- **Tunnel work brought forward** so the POC could be proven early.
- **Sysadmins see no owner data** and there is no Django admin in
  production (D9).
- **Per-email lockout ceiling** uses allauth's rate limit; its block can last
  up to the rest of the hour rather than exactly 15 minutes (D15).

## Dependencies added

| Package | Why |
|---|---|
| Django 5.2 LTS | framework (current LTS) |
| psycopg 3 | Postgres driver; safe SQL composition for grants |
| django-environ | configuration from environment variables |
| gunicorn | WSGI server |
| whitenoise | static files from the same image |
| django-storages[s3] | S3-compatible storage (R2 / SeaweedFS) |
| procrastinate[django] | Postgres task queue (emails, nightly purge) |
| django-csp | Content-Security-Policy (Django 5.2 lacks it) |
| django-simple-history | change history for register records |
| django-allauth[mfa] | sign-in, emailed codes, TOTP, passkeys, recovery codes |
| django-axes | lockout per email and address |

Development: pytest, pytest-django, pytest-cov, factory-boy, ruff, bandit,
pip-audit, pip-tools. Requirements are pinned with hashes.

## Deferred, or not yet done

- **HTMX** is not vendored yet: no Milestone 1 page needed it.
- **Passkey registration** was checked only as far as the page loading
  cleanly in a browser; a full passkey enrollment has not been automated.
  Please try it on your phone (see below).
- **Recovery codes**: allauth shows them after enrollment, but we do not
  force an "I have saved these" confirmation.
- **Signed download URLs** point at an address browsers cannot reach
  locally; to be solved with the document library (Milestone 2).
- **Owner help page** for signing in, and a "your recent sign-ins" page.
- **Backups and the restore drill** are outlined in the runbook, not built.
- **Container images and GitHub Actions** are pinned by tag, not by digest.
- **The board dashboard** lists permission codes (e.g. `ownership.manage`)
  rather than plain-English descriptions.

## Security assumptions you should verify

1. **Only the tunnel reaches the VM.** VLAN 50 isolation is on; the VM's
   firewall denies all inbound traffic; the web container publishes no port.
2. **Cloudflare Access** allows only the intended emails on
   `portal-dev.oakcreekhoa.com`.
3. **The tunnel token and `.env` secrets** exist only on the VM and in the
   password manager. Proxmox backups of VM 107 are encrypted.
4. **The apex site's HTTPS** (Universal SSL) is fixed before "Always use
   HTTPS" is turned back on.
5. **Email**: the Hostinger mailbox exists, and `EMAIL_REDIRECT_ALL_TO` is
   set on the POC so fictional addresses never receive mail.
6. **Two board members with MFA** exist before relying on any two-person
   flow, since both flows deadlock with only one.
7. **The HIBP check** reaches `api.pwnedpasswords.com` from the VM, or
   accepts that it fails open.
8. **Audit IP retention** (365 days) is acceptable to the association's
   counsel.
9. **Owner-role credentials** stay off the web and worker containers (check
   `dc exec web env` shows no owner or superuser password).
10. **Dependabot and CI** stay green; Django stays on the 5.2 LTS line until
    6.2 LTS is deliberately adopted.
