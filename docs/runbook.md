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
3. Load the lot and owner register (real deployments). Every board member,
   you included, is in it as a homeowner; the board seat is added on top.
   Use the email you bootstrapped with so your own ownership attaches to
   your login. Start from `docs/examples/register-template.csv`, keep the
   real file off the repository, check first, then write:

   ```bash
   dc exec web python manage.py import_register /tmp/register.csv
   dc exec web python manage.py import_register /tmp/register.csv --commit
   ```

   Delete the file from the server afterwards. Later changes of owner go
   through a transfer on the lot page; a board member's own ownership is
   recorded by another board member (no self-grants).

   On a POC seeded with fictional data instead, attach yourself to a demo lot
   to test the homeowner side with your own login:
   `dc exec web python manage.py demo_make_me_owner --email you@example.org --lot 5`

4. Optional demo data (fictional, POC only, instead of step 3):

   ```bash
   dc exec web python manage.py seed_demo
   ```

   It ends with "Demo data created: 44 lots ...". If it prints an error
   instead, nothing is saved (it runs as one transaction). Check what the
   database holds with `dc exec web python manage.py data_status`.

5. Invite a second board member as soon as possible: the board page warns
   until at least two people with MFA can perform every action.

## Deploying an update

```bash
git pull --ff-only && sudo bash infra/poc/start-portal.sh
```

The script stops the containers first (data volumes are kept), so expect a
few seconds of downtime. The `migrate` service applies migrations (as the
owner role) before web and worker start; web and worker then check they are
connected as the app role and refuse to start otherwise. The sysadmin
**System status** page shows the deployed commit.

POC only: to (re)load demo data the `.env` must also contain
`DEPLOYMENT_STAGE=poc` (added by `make-env.sh` since the 2026-11 security
review; add it by hand to an older `.env`).

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

File store (writes, reads and deletes a small probe file; prints the store's
error code if it fails):

```bash
dc exec web python manage.py check_storage
```

When something fails, the error log names the exception and, for the file
store or other APIs, its short error code (`exc_code`), never the message:

```bash
dc logs --since 2h web | grep '"level": "ERROR"' | tail -3
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

## Malware scanning

Off on the POC (`DOCUMENT_SCANNER=none`; System status shows it). To enable:
add a ClamAV container, set `DOCUMENT_SCANNER=clamd`,
`DOCUMENT_CLAMD_ADDRESS=clamav:3310` and `DOCUMENT_REQUIRE_CLEAN_SCAN=true`,
restart, then scan what is already stored:

```bash
dc exec web python manage.py rescan_files --actor you@example.org --reason "scanner enabled"
```

Run it again whenever the scanner's signatures update; a file found infected
later is blocked from download immediately.

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
- [ ] Malware scanning on (`DOCUMENT_SCANNER=clamd`,
      `DOCUMENT_REQUIRE_CLEAN_SCAN=true`) and `rescan_files` run.
- [ ] HSTS one year with subdomains (`SECURE_HSTS_SECONDS=31536000`,
      `SECURE_HSTS_INCLUDE_SUBDOMAINS=true`) once every subdomain serves HTTPS.
- [ ] Board decisions in `docs/security-review-2026-11.md` section 3 made.


## ARC neighbour sign-off and Gemini

1. Record each lot's neighbours once (Board > Lot neighbours), from the
   development plan. They are pre-selected on every architectural request.
2. On a request, the applicant (or the board or manager) adds the
   neighbours, adds any other addresses, and sends the requests. Owners with
   a login answer in the portal; everyone else gets a single-use link that
   expires after `SIGNOFF_LINK_DAYS`. Anyone can also print the prefilled
   form, sign it on paper, scan it and upload it.
3. To have scanned forms read automatically, put a Gemini API key in the
   VM's `.env` (never in chat or the repository), then restart:

   ```bash
   sudo nano /opt/hoa-portal/.env        # set GEMINI_API_KEY=...
   sudo hoa-update --force
   ```

   A board member or the manager confirms each signature it reads. To accept
   confident readings automatically, switch on "Accept automatically read
   sign-offs" in Community settings. The free tier may use submitted forms to
   improve Google's products: use a paid key once forms carry real names
   (decision D21).


## Backups and the restore drill

Run once to install the nightly backup (02:30) and take a first backup:

```bash
sudo bash /opt/hoa-portal/infra/poc/backup.sh --install
```

The first run creates `/root/hoa-backup.key`. Copy it somewhere safe and
offline (a password manager, or printed and locked away): without it the
backups cannot be restored. Backups land in `/var/backups/hoa-portal/<date>/`
(14 days kept). For an off-site copy, create a bucket the HOA owns (for
example Cloudflare R2), put its details in `BACKUP_S3_*` in `.env`, and the
nightly run uploads the already-encrypted files.

Prove a backup restores, without touching the live portal:

```bash
sudo bash /opt/hoa-portal/infra/poc/restore-drill.sh
```

It loads the newest backup into a throwaway copy, brings it up to the current
code, checks the file store and prints the record counts. Do this before any
real data goes in, and every few months after.

## Health alerts

Install the 10-minute health check, which emails the sysadmins when the site,
the background worker, the disk, the file store, the malware scanner or the
error rate has a problem (and again when it recovers):

```bash
sudo bash /opt/hoa-portal/infra/poc/health-timer.sh
```

The same checks are on Admin > System status.

## Email deliverability (SPF, DKIM, DMARC)

Notices are only useful if they arrive. For the domain the portal sends from
(`DEFAULT_FROM_EMAIL`), add these DNS records at your DNS provider (Cloudflare):

1. **SPF**: one TXT record on the domain, listing your mail provider, e.g.
   `v=spf1 include:<your provider's SPF host> ~all` (your provider documents
   the exact value; only one SPF record per domain).
2. **DKIM**: the TXT/CNAME records your mail provider gives you (they sign
   outgoing mail).
3. **DMARC**: a TXT record at `_dmarc.<domain>`, starting with monitoring:
   `v=DMARC1; p=none; rua=mailto:<an inbox you read>`. After a few weeks of
   clean reports, tighten to `p=quarantine`.

Then send a test (invite yourself, or `sendtestemail` in the web container)
and check the message headers show `spf=pass`, `dkim=pass`, `dmarc=pass`.
Bounces go to the provider's bounce address: check its dashboard for
addresses to correct on owners' records.

## Onboarding owners

After loading the register, Board > Onboarding shows who has signed up. Use
"Invite everyone not yet invited", then "Remind" a few days later. Owners with
no email get a printable letter asking for one; invitations only ever go to
the email on the owner's record.
