# Runbook (stub)

Operational procedures. Expanded through Milestone 1; complete before any real
data is loaded.

## Secrets

- Generate every secret on the host that uses it:
  `python3 -c "import secrets; print(secrets.token_urlsafe(50))"`
- Store them in the HOA password manager and the host's `.env` only. Never in
  the repository, chat, tickets or email.
- The Postgres owner and superuser passwords are needed only for migrations and
  restores. They must not be given to the web or worker containers.

## Starting and stopping

```bash
docker compose up -d
```

```bash
docker compose ps
```

```bash
docker compose down
```

`docker compose down -v` also deletes the database and object-store volumes.
Never run it on a host with data you need.

## Migrations

Migrations run automatically in the one-shot `migrate` service, as the owner
role, before web and worker start. Runtime grants are re-applied afterwards.
To re-apply grants by hand:

```bash
docker compose run --rm migrate manage apply_db_grants
```

## Direct database access

Only for restore drills and declared incidents. Record each use in a written
incident note: who, when, why, what was run. (Decision D9.)

## To be written in Milestone 1

- POC VM provisioning (Proxmox, VLAN, host firewall, automatic updates).
- Cloudflare: tunnel, Access application, DNS, Always HTTPS, minimum TLS 1.2.
- Email: dedicated Hostinger mailbox, SPF/DKIM/DMARC records.
- Bootstrap: `bootstrap_first_officer`.
- Assisted MFA reset procedure (decision D5).
- Backups: encrypted off-site Postgres dumps; encrypted VM backups (they contain `.env`).
- Restore-and-migrate drill (required before any real data or live payments).
- Production checklist: HSTS to 31536000, `EMAIL_REDIRECT_ALL_TO` empty,
  `ADMIN_ENABLED=false`, move the HOA domain and accounts to HOA ownership,
  pin container images by digest.
