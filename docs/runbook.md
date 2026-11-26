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
   through a transfer on the home's page (Homes and owners); a board member's own ownership is
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
2. **Payments**: record each check on the Home account page (check number
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


## ARC neighbor sign-off and Gemini

1. Record each home's neighbors once (Board > Home neighbors), from the
   development plan. They are pre-selected on every architectural request.
2. On a request, the applicant (or the board or manager) adds the
   neighbors, adds any other addresses, and sends the requests. Owners with
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
example Cloudflare R2), and the nightly run uploads the already-encrypted
files (see "Off-site backups that cannot be deleted" below).

To take a backup at any other time, use the portal: **Admin → Server tasks →
Take a backup now** (sysadmins; also on System status next to the Backups
check). The server's agent runs it within a minute and the page shows the
result, for example "Backup 2026-11-26T1830Z, 5.3M, copied off-site" (D36).
If the portal is down, run `sudo bash /opt/hoa-portal/infra/poc/backup.sh`.

Prove a backup restores, without touching the live portal: **Admin → Server
tasks → Restore drill → Start**. If the portal is down:

```bash
sudo bash /opt/hoa-portal/infra/poc/restore-drill.sh
```

It loads the newest backup into a throwaway copy, brings it up to the current
code, checks the file store and prints the record counts. Do this before any
real data goes in, and every few months after.

The drill always uses its own empty file store, never the live one (even
after the move to R2), and fails unless every file in the backup was
restored. Before anything is decrypted it checks the backup's keyed checksum
and prints when the backup was made; it refuses a backup whose checksum is
missing, altered, or from a different backup. Backups made before
stamped checksums (November 2026) restore only with `--allow-unsigned`, which
prints a warning: use it only for a backup you know you made.

To check a backup by hand before a real restore (for example one downloaded
from the off-site bucket into a folder), run
`python3 -I /opt/hoa-portal/infra/poc/backup_mac.py verify <folder> /root/hoa-backup.key`:
it prints the time the backup was made, or refuses. Never put the key on a
command line.

## Off-site backups that cannot be deleted (Cloudflare R2, D31)

In the Cloudflare dashboard, under R2:

1. **Create bucket:** for example `oak-creek-backups`, location Automatic, storage class **Standard** (Infrequent Access isn't covered by the free tier and charges for every read). Leave public access off. Never use the bucket that holds the portal's files.
2. **Bucket → Settings → Bucket lock rules → Add rule:** prefix `backups/`, retention **90 days**.
3. **Bucket → Settings → Object lifecycle rules:** delete objects under `backups/` after **365 days**.
4. **Manage R2 API tokens → Create API token:** permission "Object Read & Write", applied to this bucket only. Note the Access Key ID, the Secret Access Key and the Account ID.

Then, on the VM:

```bash
sudo bash /opt/hoa-portal/infra/poc/setup-offsite-backups.sh
```

The script:

- asks for the four values (the secret stays hidden);
- saves them to `.env` and restarts the portal;
- **proves the lock** by uploading a tiny probe file and trying to delete it (it refuses to continue if the delete works);
- takes a backup, which goes off-site, and installs the nightly timer.

The probe files stay in the bucket until the lock expires. They hold no data.

Afterwards:

- System status → Health checks → **Backups** shows the age of the last local and off-site copies and when the lock was last verified. The sysadmins get an email if either copy is more than 36 hours old.
- Re-check the lock at any time: **Admin → Server tasks → Check the off-site lock**. If the portal is down:

  ```bash
  sudo docker compose -f docker-compose.yml -f docker-compose.tunnel.yml exec web python manage.py check_offsite_lock
  ```

- Prove the off-site copy restores: **Admin → Server tasks → Restore drill from the off-site copy** (or, if the portal is down, the command below). It downloads the newest complete backup, checks its keyed checksum, refuses it unless the time signed inside the checksum is the time in its name and not in the future, so an old backup copied under a newer name cannot pass, then restores it into the scratch copy:

  ```bash
  sudo bash /opt/hoa-portal/infra/poc/restore-drill.sh --from-offsite
  ```

Keep `/root/hoa-backup.key` offline and **outside the Cloudflare account**. Without it, the off-site backups can't be decrypted.

## Health alerts

Install the 10-minute health check, which emails the sysadmins when the site,
the background worker, the disk, the file store, the malware scanner or the
error rate has a problem (and again when it recovers):

```bash
sudo bash /opt/hoa-portal/infra/poc/health-timer.sh
```

The same checks are on Admin > System status; to run them now and email any
change, use Admin → Server tasks → Run the health checks.

### Server errors: reading the list (D45)

When the Server errors check fails, the email lists the five most common kinds
of error in the last hour, and System status → Recent server errors lists every
kind from the last 24 hours, newest first. Each kind reads in plain words
first: which page or background job failed, why (a bug in the portal's code,
the database or another service not answering, email sending down, the portal
reporting itself unavailable), whether people saw an error page, and what to
do. Underneath, "Technical:" carries what the maintainer needs: the error
type (for example `KeyError`), where in the portal's code it happened
(`ledger/views.py:233 in lot_ledger`), the method and route name, the status,
the release that was running and the first and last time it happened (site
time). Two deliberate 503s are named as such: "Health probe: database
unavailable" (the container's `/healthz` check could not reach the database)
and "Email sending unavailable page" (someone saw the "We couldn't send the
email" page). Nothing in the list carries a web address, a message, a name or
an IP address.

For the full call stack of the same error, look in the web container's log:

```bash
cd /opt/hoa-portal && sudo docker compose logs web --since 2h | grep '"exc_type"'
```

Each JSON line carries `exc_type` and `exc_frames` (file:line in function for
every frame) and the `view` name; match it to the list by error type, route
and time. Fix the cause, ship the fix, then "Clear" the Server errors check.
Counts and signatures are removed after 30 days.

## Host security

Run once on the VM:

    sudo bash /opt/hoa-portal/infra/poc/harden-host.sh

To re-apply it later (after updates, or when System status shows a service
off), use Admin → Server tasks → Re-apply host security; the command above is
the fallback when the portal is down.

It sets up:

- **Automatic security updates** (unattended-upgrades; never reboots by itself). With Ubuntu Pro attached, ESM updates come the same way and Livepatch applies kernel fixes without a reboot.
- **auditd** watching `.env`, the backup key, SSH keys and config, sudoers, users and groups, and systemd units.
- **AIDE**, a nightly (03:30) file-integrity check of /etc, /root, /usr/local, /boot and the hoa user's SSH keys. Changes are emailed, and accepted as the new baseline only once the email reached at least one sysadmin; if it did not (mail down, or no sysadmin email set), the old baseline stays, the same changes are reported again the next night, and the timer shows as failed.
- **Falco** (modern eBPF), watching the containers at runtime: shells, unexpected programs, writes and secret reads. Alerts at warning and above go to `/var/log/falco/alerts.jsonl`, and a summary is emailed at most every 10 minutes; alerts that could not be emailed are sent again on the next run.

Emails go to every active sysadmin through the portal's mail settings, and each one is audited (`system.host_alert`, with how many were sent). `host_alert` exits with an error when it reached nobody, so make sure at least one sysadmin has an email address.

Falco's packages are trusted only through Falco's signing key with the fingerprint Falco publishes (the "Trusted" row of the key table at https://falco.org/docs/setup/packages/; pinned in `harden-host.sh` as `478B2FBBC75F4237B731DA4365106822B35B1B1F`, checked 2026-11-26). If the downloaded key does not match, the script prints the fingerprints it found and stops before installing Falco. When Falco rotates its key (announced on falco.org/blog), compare the new fingerprint with that page and re-run with it: `sudo HOA_FALCO_KEY_FPR=<fingerprint> bash /opt/hoa-portal/infra/poc/harden-host.sh`, then update the pinned value in the script. System status → Host security shows whether each service is running.

If an alert is unexpected:

1. Check who was signed in to the VM: `last -n 20`.
2. Look at the detail: `sudo ausearch -k hoa_env -i` (or another key), or `sudo tail /var/log/falco/alerts.jsonl`.
3. If it may be an intrusion, stop the tunnel (`docker compose stop cloudflared`), keep the VM running for evidence, and rotate the `.env` secrets from a clean machine.

To quiet a known-good Falco alert, add an exception in `/etc/falco/rules.d/hoa-portal.yaml`, then run `sudo systemctl restart falco-modern-bpf`. Falco uses about 100–150 MB of memory.

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

## Bank feed (Teller, free tier)

The portal can read the association's bank account so the monthly
reconciliation is mostly ticking boxes (Finance -> Accounting -> Bank feed).
It is free: Teller's developer tier covers up to 100 live connections (we need
one to three). It is read-only: nothing is paid or moved, and nothing posts to
owner accounts. The portal keeps the account's last four digits only, never
the full account or routing number, and stores Teller's access key encrypted.

Teller does not cover every bank; smaller community banks may be missing. If
the HOA's bank is not in Teller's list, skip to "Statement files" below: every
US bank offers them and they are always free.

**1. Teller account (the HOA's, not a personal one).** Sign up at teller.io
with the HOA's shared email address and the board's password manager. In the
dashboard, note the **Application ID**, then under **Certificates** create a
certificate and download the two files (`certificate.pem` and
`private_key.pem`). Keep them only in the password manager and on the VM;
never paste them into chat, email or a ticket.

**2. Put them on the VM.** Copy the two files to the VM (for example with
`scp` into `/root/teller/`, folder mode 700), then:

```bash
cd /opt/hoa-portal
sudo bash -c 'source infra/poc/lib.sh
set_env .env TELLER_APPLICATION_ID "app_xxxxxxxxxxxx"
set_env .env TELLER_ENVIRONMENT development
set_env .env TELLER_CERTIFICATE_B64 "$(base64 -w0 /root/teller/certificate.pem)"
set_env .env TELLER_PRIVATE_KEY_B64 "$(base64 -w0 /root/teller/private_key.pem)"
grep -q "^BANK_FEED_TOKEN_KEY=." .env || set_env .env BANK_FEED_TOKEN_KEY "$(openssl rand -hex 32)"'
sudo shred -u /root/teller/certificate.pem /root/teller/private_key.pem
sudo bash infra/poc/start-portal.sh
```

Replace `app_xxxxxxxxxxxx` with the Application ID. `development` is Teller's
free tier for **real** accounts (`sandbox` is for trying it with fake banks;
`production` is Teller's paid tier). `set_env` writes the values with shell
builtins, so they never appear on a command line. `BANK_FEED_TOKEN_KEY`
encrypts the stored access key: it is generated once and must never change
(a new key means reconnecting the bank). Keep a copy in the password manager.

**3. Connect.** A board member: Admin -> Settings -> Features, switch on
**Bank feed**. Then Finance -> Accounting -> Bank feed -> **Connect a bank**
(the portal asks for your password or passkey again). Search for the HOA's
bank in Teller's window; if it is not listed, use statement files instead.
Sign in with the association's online-banking login. The account appears with
its last four digits and the last few months of lines.

**4. Every day after that** the portal syncs overnight; anyone on the board
or the manager can press **Sync now**. System status shows a **Bank feed**
check that fails if there was no sync for 3 days or the bank asks to be
reconnected (banks do this every few months, or after a password change):
then press **Connect a bank** again. **Disconnect** (board) deletes the access
key and keeps the lines already imported.

**Accepted risk: Teller's Connect script (D34-fixinfra-2).** The Connect a
bank page loads Teller's script from `https://cdn.teller.io/connect/connect.js`.
Teller publishes it only at that unversioned address, with no integrity hash,
so the browser cannot check that the file is the one we reviewed. If Teller's
CDN were ever compromised, that script could act as the signed-in board member
on that one page. Limits in place: only this page allows it (its
Content-Security-Policy admits scripts from Teller's `/connect/` path only,
nowhere else on the site), the page needs a fresh password or passkey check,
the script is fetched without a referrer, and it is opened only when a board
member connects or reconnects the bank (every few months). To reduce it
further: connect from a browser profile used only for this, sign out right
after.
If Teller starts publishing a versioned URL or an integrity hash, pin it in
`ledger/bankfeeds/teller.py` (`CONNECT_SCRIPT`) with `integrity` and
`crossorigin` on the script tag. If the browser console on Connect a bank
reports a blocked Teller script, the path rule in `teller.CONNECT_CSP` needs
the reported address.

**Statement files (free with any bank).** On the bank's website, download the
account's activity as **OFX** or **QFX** ("Quicken" or "Money" format) if
offered, otherwise **CSV**. In Bank feed, add an account for statement files
once (a name and the last four digits), then **Import a statement file**.
A CSV file asks which column is the date, description and amount (or debit
and credit). Importing the same or an overlapping file twice adds nothing
twice.

**Matching and the monthly reconciliation.** Bank feed suggests, for each bank
line, the deposit or vendor payment with the same amount and a nearby date;
the manager or a board member confirms each one. On **Reconcile a statement**
the matched items are already ticked and the ending balance is filled in from
the feed: check both against the paper or PDF statement before recording.
Lines that match nothing (fees, interest) stay listed until they are entered
in the books.
## Online payments (Stripe)

Decision D32-payments. Owners (and authorized contacts) pay chosen charges on
Stripe's hosted Checkout page: **bank account (no fee)**, **debit card (no
fee)** or **credit card (+ surcharge, at most 3%)**. The portal never sees a
card or bank number, has **no webhook** (no anonymous endpoint, D24) and learns
about payments only by asking Stripe with its key:

- when the owner returns to the Payment status page (a hint to check now),
- every 15 minutes for checkouts started in the last 24 hours,
- daily for bank payments still processing, refunds, disputes and returned
  bank payments on payments of the last 120 days, the Stripe balance and
  payouts, and then the reconciliation (ledger versus Stripe).

A payment is recorded only when the session and PaymentIntent fetched from
Stripe match the checkout this signed-in person started (its id and random
nonce in the metadata, the lot, account and person, amount and currency,
test or live mode, status succeeded and the charge captured). Anything else
records nothing, is audited (`payments.mismatch`) and shows on the board's My
items.

### 1. Test mode (now)

1. Create a Stripe account for testing (any email the board controls). Stay
   in **Test mode** (the toggle in the dashboard).
2. Developers -> API keys -> **Create restricted key**, named "HOA portal",
   with ONLY these rights (everything else None):
   - Checkout Sessions: **Write**
   - PaymentIntents: Read
   - Charges: Read
   - Balance transactions (Balance): Read
   - Payouts: Read
   - Disputes: Read
   - Refunds: **Read** (never Write)

   No refund, customer, product, payout or transfer write rights and no
   account settings. With this key a stolen secret can only create checkouts
   that pay the HOA. (Because the key cannot refund, a surcharge taken from a
   debit card by mistake becomes a board My item "Refund $X surcharge to lot
   N (debit card)"; refund it in the Stripe dashboard and the portal closes
   the item when it sees the refund.)
3. On the VM, put the key in `.env` (hidden prompt; never paste a key into a
   chat, ticket or the repository):

   ```bash
   sudo bash /opt/hoa-portal/infra/poc/set-stripe-key.sh
   ```

4. Admin -> Features: switch on **Online payments**. Community settings:
   check **Credit card surcharge** (default 300 = 3%) and give it its
   citation (board resolution).
5. Finance -> Online payments shows "TEST MODE"; owners' pay pages carry a
   TEST MODE banner. Try it as a demo owner (Pay online on My account):
   - Credit card: `4242 4242 4242 4242`, any future date, any CVC. The
     surcharge line shows on Stripe's page.
   - Debit card: `4000 0566 5566 5556` (Visa debit). No fee. On the credit
     card session Stripe refuses it (funding filter); on the debit session it
     is accepted.
   - Bank account: in Stripe's Financial Connections test screen choose the
     **Test Institution** and a "Success" account, or enter routing
     `110000000` and account `000123456789`. It shows "processing" and is
     recorded when Stripe reports it succeeded (the daily poll; the next
     morning). Account `000111111116` fails. Stripe's docs list more test
     numbers (returns and disputes).

What Stripe's API offers (checked against Stripe's changelog, Nov 2026; the
portal pins API version `2026-08-26.dahlia`):

- `payment_method_options[card][restrictions][funding_types_blocked]`
  (values `credit`, `debit`, `prepaid`; added in 2026-08-26.dahlia) keeps a
  credit card session to credit cards and the debit session to debit cards.
  Cards whose funding type is unknown can still get through; the safety net
  after payment reads the charge's `payment_method_details.card.funding`.
- `wallet_options[link][display]=never` hides Link on both card sessions.
  Apple Pay and Google Pay cannot be switched off per session on hosted
  Checkout (only for the whole account in Settings -> Payment methods). A
  wallet payment is a card payment and reports the underlying card's
  funding, so the same safety net applies. If the board wants no wallets,
  turn them off in that Stripe setting.
- Stripe's API version 2026-09-30 removes `payment_method_types` from
  Checkout Sessions (replaced by allowed/excluded payment method types).
  Before moving the pinned version, change `payments/checkout.py`.
- Bank payments use `us_bank_account` with `verification_method=automatic`:
  instant verification through Stripe Financial Connections where the bank
  supports it, micro-deposits otherwise (1-2 business days for the owner).
- Costs (confirm on stripe.com/pricing before go-live; the HOA pays all of
  them except the credit card surcharge): cards about 2.9% + 30 cents; ACH
  Direct Debit 0.8% capped at $5; Financial Connections verification may
  carry its own small fee; disputes $15 each; failed/returned ACH $4.

### 2. Securing the HOA's Stripe account

- Hardware security key (passkey) two-step sign-in for every Stripe user.
- At most two Administrators (the treasurer and one other board member);
  everyone else View only. No management company admin.
- Turn on Stripe email alerts for changes to the payout bank account and for
  new team members.
- Payout schedule: daily (smaller deposits, easier to match).
- Radar: keep the default rules on.

### 3. What the board does

- **My items** shows: payments that did not match (check them in Stripe's
  dashboard), returned bank payments (already reversed on the account),
  refunds made in Stripe (record them on the lot's account: reverse or
  adjust), disputes (answer in Stripe), surcharges to refund, and, for the
  treasurer, differences found by the daily reconciliation.
- **Finance -> Online payments**: recent payments, bank payments processing,
  failures, payouts (gross payments, refunds, fees and the net deposit) and
  the last reconciliation. Close an item with "Mark done" and a note.
- The accounting export books online payments to **Stripe clearing**, the
  surcharge to **credit card surcharge income**, Stripe's fees to **payment
  processing fees**, and each payout from Stripe clearing to the bank, so the
  books balance. Online payments never go into deposit batches.
- The health check **Online payments** fails when the daily reconciliation
  finds a ledger payment Stripe does not have, a Stripe charge the ledger
  does not have after 24 hours, or a payout that does not add up; the board
  and sysadmins get an email (no personal data).

### 4. Go-live checklist (before live keys)

1. The HOA's own Stripe account, opened in the association's legal name with
   its EIN, the HOA's operating bank account for payouts, and the account
   security above.
2. Card-network surcharge registration: tell Visa and Mastercard (through
   Stripe's surcharging guide/acquirer) at least **30 days before**
   surcharging, and check state rules (Pennsylvania allows surcharges).
3. Counsel approves the disclosure wording ("A 3% surcharge applies to
   credit card payments; no surcharge on debit cards or bank payments. The
   surcharge is not greater than our cost of accepting credit cards.") and
   the receipt, and the board records the surcharge rule with its citation.
4. A restore-and-migrate drill (CLAUDE.md) and one real end-to-end payment
   of each kind in test mode on the production server's settings.
5. Live restricted key with the same rights, entered with the same hidden
   prompt, and `DEPLOYMENT_STAGE=production` plus `PAYMENTS_LIVE_ALLOWED=true`
   in the production `.env`. The portal refuses live keys without both, and
   refuses test keys in production.

## Server tasks (D36)

Reusable host operations are started in the portal, not in a terminal:
**Admin → Server tasks** (sysadmins start them; the board sees the history).
The web app only records the request; the VM's agent
(`infra/poc/portal-agent.sh`, every minute) runs the fixed command for it
from `infra/poc/host-tasks.sh` and writes back the state and a one-line
summary. One task runs at a time. The full output of each task is in
`/var/log/hoa-portal-tasks.log` on the VM.

| Task | What runs on the VM | Time limit |
|---|---|---|
| Take a backup | `infra/poc/backup.sh` | 20 min |
| Restore drill | `infra/poc/restore-drill.sh` (newest local backup) | 25 min |
| Restore drill from the off-site copy | `infra/poc/restore-drill.sh --from-offsite` (only once off-site backups are set up) | 25 min |
| Check the off-site lock | `manage.py check_offsite_lock` in the web container (only once off-site backups are set up) | 5 min |
| Re-apply host security | `infra/poc/harden-host.sh` | 25 min |
| Run the health checks | `manage.py health_check --alert` in the web container | 5 min |

If the page says tasks will not run, the agent has not been seen for five
minutes: check `systemctl status hoa-portal-agent.timer` and
`journalctl -u hoa-portal-agent.service` on the VM. Steps that need a secret
typed by a person (`setup-offsite-backups.sh`, `set-stripe-key.sh`,
`move-files-to-r2.sh`) and one-time setup (`provision-vm.sh`, the first
`harden-host.sh`) stay in the terminal.

