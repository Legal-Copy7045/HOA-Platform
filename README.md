# Oak Creek Farms HOA portal

Secure owner and board portal for a 44-lot Pennsylvania planned community
(PA Uniform Planned Community Act and the community's recorded Declaration).
Invite-only: every account signs in with a passkey, or a password or emailed
code followed by an authenticator app or passkey.

## What it does

- **Owners and their contacts** (My home): their home and account, monthly
  statements, online payment and automatic payment through Stripe's hosted
  pages (no card or bank numbers in the portal), requests to the board with
  threads and photos, home improvement (ARC) requests with neighbor
  acknowledgements, violations on their home, resale certificates, records
  requests, notices by email or post, My items to-dos, and a read-only
  history for former owners.
- **Community**: documents, calendar with meetings and event RSVPs,
  announcements, an opt-in directory, board and committees, FAQ, common-area
  reservations, voting (formal votes with secret ballots, elections,
  surveys), portal search, an installable app with optional push and text
  alerts.
- **Board, manager and committees** (Association, Maintenance, Finance,
  Admin): homes and owners, sign-ups and invitations, governing rules with
  citations, ARC review with recusal and decision letters, violations,
  vendors, work orders and invoice approval, the owner ledger (open-item
  accounting, monthly assessments, late fees, collections and letters),
  budgets and reserves, accounting export and bank reconciliation (optional
  bank feed), meetings, packets, minutes, resolutions and written consent,
  reports, access reviews and retention.
- **Sysadmins** (operations only, never owner data): System status (health
  checks, updates, malware scanning and the files waiting for a scan, server
  errors), Server tasks (backups, restore drills, host hardening), and the
  board-visible sysadmin activity log.

Everything financial, legal or audit-related is append-only; corrections are
new reversing records. Rules from the Declaration are data with citations.

## Documentation

- `CLAUDE.md`: architecture and rules (source of truth), how to run and test;
  the full rules and reference in `docs/claude/`
- `docs/decisions.md`: why each decision was made (D1 onwards)
- `docs/runbook.md`: operating the portal (setup, updates, backups, malware
  scanning, payments, bank feed, the go-live checklist)
- `docs/glossary.md`: the names people see
- `docs/security-review-2026-11.md`: security reviews and their findings
- `CHANGELOG.md` and `VERSION`: releases
- `infra/poc/README.md`: building the Proxmox POC
- `regression/README.md`: the full regression suite and walkthrough
- `docs/milestone-*-plan.md` and `-summary.md`: history of each milestone

## Stack

Python 3.13, Django 5.2 LTS, PostgreSQL 16, server-rendered templates,
django-allauth with MFA (TOTP and passkeys), django-axes, Procrastinate
(Postgres task queue, no Redis), S3-compatible file storage (Cloudflare R2 or
local SeaweedFS), ClamAV, Docker Compose, Cloudflare Tunnel.

## Local development

Requirements: Docker with the compose plugin.

```bash
cp .env.example .env
```

```bash
docker compose up --build
```

- Site: http://localhost:8000
- Mail inbox (Mailpit): http://localhost:8025

Create a first account and demo data (fictional only):

```bash
docker compose exec web python manage.py bootstrap_first_officer --name "Local Officer" --email officer@example.com --also-sysadmin
```

```bash
docker compose exec -e SEED_DEMO_ALLOWED=true web python manage.py seed_demo
```

Open the printed link, set a password, then enroll an authenticator app or
passkey (every account must). The `.env.example` values are local
placeholders. Every port binds to `127.0.0.1`; Postgres and the object store
publish no ports. There is no LAN mode: other devices use the Cloudflare
tunnel (see the runbook).

| Service | Purpose |
|---|---|
| postgres | database; creates the owner and app roles on first start |
| seaweedfs | local S3-compatible object store (private bucket; profile `local-store`) |
| create-bucket | one-shot: creates the bucket |
| migrate | one-shot: cache table, migrations as the owner role, runtime grants |
| web | gunicorn, as the app role |
| worker | Procrastinate worker (emails, scheduled jobs, malware-scan retries), as the app role |
| mailpit | catches all outgoing email (local only) |

The POC and production add `docker-compose.tunnel.yml`: production settings,
the `clamav` scanner, and `cloudflared` (the only way in; web publishes no
port). Infrastructure images are pinned by version and digest and updated by
Dependabot pull requests (runbook, "Container image updates").

## Tests and checks outside Docker

Tests need Postgres 16 with the two roles:

```bash
sudo -u postgres env HOA_OWNER_DB_USER=hoa_owner HOA_OWNER_DB_PASSWORD=hoa_owner HOA_APP_DB_USER=hoa_app HOA_APP_DB_PASSWORD=hoa_app bash docker/postgres/init-roles.sh
```

```bash
python3.13 -m venv .venv && . .venv/bin/activate
```

```bash
pip install --require-hashes -r requirements/base.txt -r requirements/dev.txt
```

```bash
scripts/check.sh
```

`scripts/check.sh` runs `python3 regression/run.py check` (the owner's
project standard, D90). CI (`ci-result` is the one required check) runs ruff,
bandit, the version check, the privacy guard and gitleaks on every ready pull
request, and the tests in two shards, pip-audit, the image build with a Trivy
scan and the regression page crawl when the pull request touches what they
test; everything weekly; CodeQL weekly. A merged version bump is the release
(tag `v<VERSION>`), and the server installs only releases.

## Project layout

```
core/        settings, policy (permissions), scoping, health, navigation, search, shared plumbing
accounts/    logins, invitations, sign-in (allauth/MFA), assisted MFA reset, access reviews
community/   homes, people, ownership and accounts, board seats, offices, roles, rules, reservations
audit/       append-only audit log, sysadmin activity
comms/       announcements, calendar, threads, notifications, push and texts
documents/   document library, attachments, malware scanning and the scan queue
governance/  meetings, packets, minutes, resolutions, voting, surveys, records requests
arc/         architectural review, neighbor acknowledgements
compliance/  violations, recurring deadlines
operations/  owner requests, vendors, work orders, invoices
ledger/      owner ledger, assessments, late fees, collections, budgets, reserves, accounting, bank feed
payments/    Stripe hosted Checkout, automatic payments
reports/     reports
docker/      entrypoint, Postgres role init, SeaweedFS and ClamAV config, healthcheck
infra/poc/   Proxmox VM scripts, update agent, backups and restore drill
regression/  full regression and walkthrough tooling
docs/        decisions, runbook, glossary, reviews, milestone plans and summaries
templates/, static/
```
