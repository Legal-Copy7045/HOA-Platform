# Runbook

Operational procedures for the Oak Creek Farms HOA portal. Commands are run
on the POC VM from `/opt/hoa-portal`, through the VM console (Proxmox ->
VM 107 -> Console -> xterm.js), logged in as `hoa`.

Shorthand used below:

```bash
alias dc='docker compose -f docker-compose.yml -f docker-compose.tunnel.yml'
```

## Secrets

- Generated on the VM by `infra/poc/make-env.sh`; stored only in
  `/opt/hoa-portal/.env` (mode 600) and the HOA password manager.
- Never in the repository, chat, tickets or email.
- The Postgres owner and superuser passwords are used only by the one-shot
  `migrate` service and restores. Web and worker never receive them.

## First-time setup of the POC (once)

1. Infrastructure: `infra/poc/README.md` steps 1-4 (VLAN, VM, provisioning,
   `.env`, `start-portal.sh`).
2. Create the first officer (you), who is also the initial sysadmin. This
   prints a single-use link instead of emailing it:

   ```bash
   dc exec web python manage.py bootstrap_first_officer --name "Your Name" --email you@example.org --also-sysadmin
   ```

   Open the printed link, choose a password, then enroll a passkey or an
   authenticator app. Save the recovery codes it shows.
3. Optional demo data (fictional, POC only):

   ```bash
   dc exec web python manage.py seed_demo
   ```

4. Invite a second board member as soon as possible: the board page warns
   until at least two people with MFA can perform every action.

## Deploying an update

```bash
git pull --ff-only && sudo bash infra/poc/start-portal.sh
```

The `migrate` service applies migrations (as the owner role) before web and
worker start. The sysadmin **System status** page shows the deployed commit.

## Day-to-day checks

```bash
dc ps
```

```bash
dc logs --tail 50 web
```

```bash
dc exec web python manage.py check_coverage
```

```bash
systemctl list-timers hoa-cloudflared-update.timer
```

## Email (Hostinger)

1. Create a dedicated mailbox, e.g. `portal@oakcreekhoa.com`.
2. In `.env` set `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD` and
   `DEFAULT_FROM_EMAIL` (the other `EMAIL_*` values are pre-filled for
   Hostinger, port 465 SSL).
3. POC only: keep `EMAIL_REDIRECT_ALL_TO` set to your test inbox. Every
   message then goes there, with the intended recipient in the subject.
4. `sudo bash infra/poc/start-portal.sh` to apply.

SPF, DKIM and DMARC (`p=reject`) are already published for the domain.

## Granting sysadmin (two board members)

1. Board member A: Board -> People -> person -> "Nominate as sysadmin".
2. The whole board is emailed (no names in the email).
3. A different board member B: Board -> Approvals -> Approve. The nominee
   cannot approve their own nomination. Nominations expire after 7 days.

Revoking: any one board member ends the role on the person's page; the
board is emailed.

## Lost authenticator (assisted reset, two board members)

1. The person contacts the board by phone or in person. There is no
   self-service path, by design.
2. Board member A confirms identity (in person, or by calling the number on
   file), then Board -> People -> person -> "Start a sign-in reset", choosing
   how identity was confirmed.
3. A different board member B: Board -> Approvals -> "Approve and show
   code". This removes all of the person's authenticators and signs them out
   everywhere. B sees a one-time code **once**.
4. B gives the code to the person directly (in person or by phone, never
   email or text). It works once, within 24 hours, and is void after 5
   wrong attempts.
5. The person signs in with their password (or an emailed code), enters the
   enrollment code, then enrolls a new passkey or authenticator app.

## Monthly routine (board or manager)

1. **Dues**: Finance → Owner accounts → "Post monthly dues" on or after the
   1st. Safe to press twice (a lot is never charged twice for a month).
   To automate, set the rule "Post monthly dues automatically" to yes.
2. **Payments**: record each check on the lot's account page (check number
   only, never account numbers). Prepayments simply leave a credit.
3. **Invoices**: enter with the PDF attached, submit; board members other
   than the entrant approve (two at $500 and over); record payment.
4. **Collections**: Finance → Collections lists balances over 30 days.
   Reminders, payment plans and referrals are recorded by hand. Late fees
   are switched off until the PA law review.
5. **Reports**: Reports page; every report downloads as CSV.

## Filling in the governing rules

Board → Community settings. Every value needs a citation (Declaration,
bylaws, or the board resolution that set it). Demo data cites "DEMO -
replace": replace every one before real use.

## Break-glass (sysadmin, on the server)

For data fixes the portal cannot do. Each requires your sysadmin login email
and a reason (no personal data), and appears on the board's Sysadmin
activity page.

```bash
dc exec web python manage.py breakglass_end_ownership --ownership 123 --actor you@example.org --reason "duplicate record from import"
```

```bash
dc exec web python manage.py breakglass_end_role --assignment 45 --actor you@example.org --reason "auditor engagement finished"
```

Suspected compromised login (deactivates and signs out everywhere):

```bash
dc exec web python manage.py breakglass_lock_user --user 67 --actor you@example.org --reason "suspected account compromise"
```

## Direct database access

Only for restore drills and declared incidents. Record each use in a written
incident note (who, when, why, what was run). Software cannot enforce this
rule; the owner-role credentials make it possible (decision D9).

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

## Backups (to be completed before any real data)

- Database: nightly `pg_dump` as the owner role, encrypted (e.g. with `age`)
  and copied off-site. Never store unencrypted dumps.
- Object store: copy the bucket (empty in Milestone 1).
- Proxmox VM backups contain `.env`: they must be encrypted too.
- A restore-and-migrate drill to a scratch environment is required before
  any real data or live payments (CLAUDE.md).

## Production checklist (before real data)

- [ ] Restore-and-migrate drill completed and written up.
- [ ] HOA owns every account: Cloudflare (zone, tunnel, Access), Hostinger,
      domain registrar, GitHub, hosting.
- [ ] `SEED_DEMO_ALLOWED=false`; `EMAIL_REDIRECT_ALL_TO` empty;
      `ADMIN_ENABLED=false` (enforced); `SECURE_HSTS_SECONDS=31536000`.
- [ ] Fresh secrets generated for production (never reuse POC secrets).
- [ ] Cloudflare Access for the POC hostname removed or replaced.
- [ ] Container images pinned by digest; Dependabot PRs reviewed.
- [ ] Audit IP retention confirmed with the association's counsel.
- [ ] At least two board members with MFA for every action
      (`check_coverage` clean).
- [ ] Owner sign-in help page and spare hardware security keys ready.
