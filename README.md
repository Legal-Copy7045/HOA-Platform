# Oak Creek Farms HOA portal

Secure owner and board portal for a 44-lot Pennsylvania planned community.

- `CLAUDE.md`: architecture and rules (source of truth)
- `docs/decisions.md`: why each decision was made
- `docs/runbook.md`: operating the portal (bootstrap, updates, resets, break-glass)
- `docs/milestone-1-summary.md`: what Milestone 1 delivered and what to verify
- `infra/poc/README.md`: building the Proxmox POC

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

Create a first account and demo data:

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
| seaweedfs | local S3-compatible object store (private bucket) |
| create-bucket | one-shot: creates the bucket |
| migrate | one-shot: cache table, migrations as the owner role, runtime grants |
| web | gunicorn, as the app role |
| worker | Procrastinate worker (emails, daily IP purge), as the app role |
| mailpit | catches all outgoing email |

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

## Project layout

```
core/          settings, policy (permissions), scoping, proxy trust, email, shared plumbing
accounts/      logins, invitations, sign-in (allauth/MFA), assisted MFA reset
community/     lots, people, ownership, board seats, offices, roles, sysadmin nominations
audit/         append-only audit log, sysadmin activity page
docker/        entrypoint, Postgres role init, SeaweedFS scripts, healthcheck
infra/poc/     Proxmox VM scripts, .env generator, start script
docs/          decisions, runbook, milestone summary
templates/, static/
```
