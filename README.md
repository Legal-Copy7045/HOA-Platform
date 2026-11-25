# Oak Creek Farms HOA portal

Secure owner and board portal for a 44-lot Pennsylvania planned community.
Read `CLAUDE.md` for the architecture and rules, and `docs/decisions.md` for
the reasoning behind them.

## Local setup

Requirements: Docker with the compose plugin.

```bash
cp .env.example .env
docker compose up --build
```

- Site: http://localhost:8000
- Mail inbox (Mailpit): http://localhost:8025

The `.env.example` values are local placeholders only. Every port binds to
`127.0.0.1`; Postgres and the object store publish no ports. There is no LAN
mode: to test from other devices, use the Cloudflare Tunnel setup in
`docs/runbook.md`.

What starts:

| Service | Purpose |
|---|---|
| postgres | database; creates the owner and app roles on first start |
| seaweedfs | local S3-compatible object store (private bucket) |
| create-bucket | one-shot: creates the bucket |
| migrate | one-shot: migrations as the owner role, then runtime grants |
| web | gunicorn, as the app role |
| worker | Procrastinate background worker, as the app role |
| mailpit | catches all outgoing email |

## Running tests and checks outside Docker

Tests need Postgres 16 with the two roles. With a local Postgres running:

```bash
sudo -u postgres env HOA_OWNER_DB_USER=hoa_owner HOA_OWNER_DB_PASSWORD=hoa_owner \
  HOA_APP_DB_USER=hoa_app HOA_APP_DB_PASSWORD=hoa_app bash docker/postgres/init-roles.sh
```

```bash
python3.13 -m venv .venv && . .venv/bin/activate
```

```bash
pip install --require-hashes -r requirements/base.txt -r requirements/dev.txt
```

```bash
pytest
```

```bash
ruff check . && ruff format --check . && bandit -q -c pyproject.toml -r .
```

The test settings default to `postgres://hoa_owner:hoa_owner@localhost:5432/hoa`
and app role `hoa_app`; override with `DATABASE_URL` and `DB_APP_ROLE`.

## Project layout

```
core/        settings (base/local/production/test), URLs, shared infrastructure
docker/      entrypoint, Postgres role init, SeaweedFS scripts, healthcheck
docs/        decisions record and runbook
requirements/  pinned, hashed dependencies (*.in are the sources)
templates/, static/
```

Apps `accounts`, `community` and `audit` arrive in the following Milestone 1 steps.
