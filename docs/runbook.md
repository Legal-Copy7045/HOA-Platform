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

## POC VM operations

Everything runs from `/opt/hoa-portal` with both compose files:

```bash
docker compose -f docker-compose.yml -f docker-compose.tunnel.yml ps
```

```bash
docker compose -f docker-compose.yml -f docker-compose.tunnel.yml logs --tail 50 web
```

Deploy new code:

```bash
git pull --ff-only && sudo bash infra/poc/start-portal.sh
```

cloudflared updates itself daily via `hoa-cloudflared-update.timer`:

```bash
systemctl list-timers hoa-cloudflared-update.timer
```

## Direct database access

Only for restore drills and declared incidents. Record each use in a written
incident note: who, when, why, what was run. (Decision D9.)

## Cloudflare (POC) - current state

Configured 2026-11-25. No secrets here; the tunnel token lives only in the
password manager and the POC VM's `.env`.

- Tunnel `hoa-portal-poc` (dedicated; its connector runs inside the VM's
  compose stack, so the isolated VLAN needs no inbound exceptions).
  Route: `portal-dev.oakcreekhoa.com` -> `http://web:8000`; everything else 404.
- DNS: `portal-dev` CNAME to the tunnel, proxied.
- Access application "HOA portal (POC)" on `portal-dev.oakcreekhoa.com`:
  allow listed tester emails only, 24h session, hidden from the app launcher.
  Remove it when the portal goes to production.
- Zone `oakcreekhoa.com`: minimum TLS 1.2. "Always use HTTPS" is OFF because
  HTTPS on the apex site was failing (edge certificate to be checked under
  SSL/TLS -> Edge Certificates). Turn it on once https://oakcreekhoa.com
  loads cleanly. SSL mode is "Full"; switch to "Full (strict)" after
  confirming the website origin has a valid certificate.
- The zone and Zero Trust organisation are in a personal Cloudflare account.
  Move them to an HOA-owned account before production.

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
