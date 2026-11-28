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
- `DJANGO_SECRET_KEY` also seals secret ballots waiting in the mixing pool
  (D68). Rotating it while a secret vote, election or anonymous survey is
  open: put the old value in `DJANGO_SECRET_KEY_FALLBACKS` until they close,
  or those ballots cannot be counted (the vote refuses to close). Keep `.env`
  apart from the database backups; a Proxmox VM backup holds both, so it can
  link pooled ballots taken during an open vote and is kept like `.env`.
- Third-party service keys (email, Stripe, Teller, USPS, the bounce
  mailbox, Gemini, the vendor document key) are entered in the browser:
  System status -> Keys and connections (D93). Sysadmins only, after a fresh
  passkey check; each key is tested with its provider first, stored
  encrypted, never shown again, and the board is emailed about every change.
  Stripe and Teller changes wait for another board member's approval, and
  their account is pinned. The set-*.sh scripts below still work as an
  optional fallback; a value saved in the portal wins over `.env`.
- `CREDENTIALS_KEK` (created in `.env` by make-env.sh, or by start-portal.sh
  on the first start of 0.44.0) unlocks those saved keys. Copy it into the
  HOA password manager with `DJANGO_SECRET_KEY`, apart from the backups:
  restoring the database without it loses the saved keys (they read as
  empty) and with them the vendor document key and the vendor documents.

## First-time setup of the POC (once)

1. Infrastructure: `infra/poc/README.md`, "Install in one command" (D94):
   `install.sh` provisions the server, makes `.env`, hardens the host,
   starts the portal and, if you give your name and email, does step 2 too,
   showing the link on the screen.
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

   Or, without a terminal, in the portal (D54): **Homes and owners ->
   Import homes and owners** (also linked from Community setup -> People and
   access). Download the Excel or CSV template, fill one row per owner,
   delete the two EXAMPLE rows, upload it (CSV UTF-8 or .xlsx, up to 2 MB;
   it is type-checked and scanned for malware), read the check (new homes,
   new and matched people, rows with problems, homes left alone), then press
   Import. The upload is kept privately for the board: delete it on the
   import's page once done (it is removed automatically after
   `REGISTER_IMPORT_FILE_DAYS`, 30 by default). The importing board member's
   own home is created but their ownership is left for another board member
   (no self-grants): that member uploads the same file again, which is safe.
   Then invite everyone from Sign-ups.

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
6. Walk through **Admin → Settings → Community setup** (`/board/community-setup/`,
   D51) as a board member. It takes every decision in order: the community's
   details and letterhead, the monthly charge, late fees, online and automatic
   payments, notices, architectural review, violations, common areas, owner
   requests, meetings and resale, privacy, features, then status-only steps
   for people and access and for the server (email, backups, off-site copy,
   Stripe and Teller keys, which a sysadmin sets on the server, never in the
   portal), and a review that flags anything still at its default or without
   a citation. Each step shows what Pennsylvania and federal law say about
   it. "Finish community setup" stays on the board's My items until the
   review is finished; run it again at any time with "Run setup again".

## Deploying an update

Only released versions are deployed (D90, the owner's standard): a merged
version bump on `main` is the release, and CI tags it `v<VERSION>` and creates
the GitHub Release with that version's CHANGELOG section. The VM moves to the
newest release tag and builds its own image from it (no registry):

```bash
sudo bash /opt/hoa-portal/infra/poc/update-portal.sh               # the newest release tag
sudo bash /opt/hoa-portal/infra/poc/update-portal.sh --ref main    # main's tip, on purpose (not a release)
sudo bash /opt/hoa-portal/infra/poc/update-portal.sh --ref <branch>  # another branch, on purpose
```

Or from the portal: **System status** → Check now / Install now / Install
tonight (the VM agent, D25/D38). "Update available" means a newer release tag
than the running code: it shows that release's number, how many commits it
adds and their titles. The local `main` branch is fast-forwarded to the tag,
never past it; a checkout already newer than the newest release (deployed with
`--ref main`) is left alone until the next release. If `main` has no release
tag yet, the script says so and installs nothing (`--ref main` deploys the tip
on purpose). The "Install" button works only while the checkout is on `main`,
or on the one branch named in `ALLOW_BRANCH` in `.env` (a deliberate test,
review R2-011), which installs that branch's tip.

`update-portal.sh` runs `start-portal.sh`, which stops the containers first
(data volumes are kept), so expect a few seconds of downtime.
The `migrate` service applies migrations (as the
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

cloudflared and the other infrastructure images are pinned in the compose
files and updated by Dependabot pull requests (see "Container image updates"
below); there is no daily cloudflared timer any more.

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

## Lost sign-in (D58, D62)

Everything starts from **Can't sign in?** on the sign-in page (D62). It asks
what's wrong ("I forgot my password", "I lost my phone, or my passkey or
authenticator app isn't working", "My setup link expired, or I never
finished setting up", "I can't get into my email any more", "It says I'm
locked out or there were too many attempts") and opens the right step below.
Tell people to start there.

### For an owner, account contact or tenant

1. **Forgot the password only:** Can't sign in?, "I forgot my password".
   Open the emailed link within an hour, enter the code from the authenticator
   app (or use the passkey, or one recovery code), then choose a new password.
   The reset never signs you in; sign in with the new password as usual.
2. **Lost the phone or passkey:** Can't sign in?, "I lost my phone ...",
   "Yes": sign in with the password and type one of the recovery codes
   instead of the 6-digit code, or use a passkey on another device. Then add
   a new passkey or app on Sign-in and security.
3. **Lost the phone and the recovery codes:** Can't sign in?, "I lost my
   phone ...", "No", enter your email, open the link (within an
   hour) and confirm. The recovery waits 48 hours (Community settings, "Sign-in
   and security"). You get an email at every address on file and a text if you
   get our texts. After the wait, ask again on the same page; open the new
   link on the phone or computer you will use, press "Remove the old ones and
   continue", add a passkey or app right away and save the new recovery codes.
   For 48 hours afterwards you cannot vote, give a proxy, change contact
   details, notice or text choices, or set up automatic payments.
4. **Didn't ask for it?** Sign in normally with your passkey or app: that
   cancels it. Or open the link in the email ("Account recovery" page) and
   press Cancel, or call the board.
5. **Setup link expired or never finished:** Can't sign in?, "My setup link
   expired ...", enter your email and press "Ask the board". The page says
   the same whatever you type; the board gets a My items entry only if the
   address belongs to a login that never added a passkey or app.
6. **No access to the email any more:** Can't sign in?, "I can't get into my
   email any more" explains it and names the sign-in help contact (Community
   settings). Only the board can help: the assisted reset below, after
   updating the email on the person's record.
7. **Locked out:** Can't sign in?, "It says I'm locked out ..." states the
   wait from the settings (`AXES_FAILURE_LIMIT`, `AXES_COOLOFF_HOURS`, default
   5 wrong passwords and 30 minutes; allauth's per-connection and per-address
   limits), then "Try again".

### For the board

**"<person> asked for a new setup link"** on My items (D62) comes from
Can't sign in?. Check it is really them (phone or in person) before opening
their page and pressing Send a new setup link; it goes to the email on their
record. The entry clears once a link is sent, if they set up a passkey or
app, or after 14 days. Nobody was emailed about it.

1. Every recovery request shows on My items ("Account recovery requested for
   ..."), and each board member gets an email. If the person says they did not
   ask, or anything looks wrong, open it and press **Cancel the recovery**.
2. After a recovery completes, My items shows "... recovered their sign-in;
   review if unexpected" for seven days. If it was not them, start an assisted
   reset (below) with a second board member and talk to the person.
3. Board members, officers, the manager, sysadmins, auditors and ARC reviewers
   (and anyone who was one in the last 30 days) cannot use self-service
   recovery: they always get the assisted reset below. The form never says so
   to the person asking (it answers every address the same way).
4. The waiting period and the lock are Community settings ("Sign-in and
   security"): at least 48 hours to wait and 24 hours of lock, at most 14
   days each, with a citation (board security policy). A change applies to
   requests started afterwards.

## Lost authenticator (assisted reset, two board members)

1. The person contacts the board by phone or in person. This is the only
   path for staff roles and is open to everyone (D58).
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
   Reminders, payment plans and referrals are recorded by hand. Late fees:
   the board posts them from the late list with a button (allowed while
   `LEDGER_LATE_FEES_ENABLED` is true, the default; set it to false in
   `.env` as an emergency off switch). They are posted automatically only
   if the board switches on "Post late fees automatically after the grace
   period" (`late_fee.auto_apply`, off by default) in Community settings.
5. **Reports**: Reports page; every report downloads as CSV.

## Filling in the governing rules

The easiest way is Settings → Community setup (step by step, D51); the same
values are on Board → Community settings. Every value needs a citation (Declaration,
bylaws, or the board resolution that set it). Demo data cites "DEMO -
replace": replace every one before real use.

## Malware scanning

On wherever real files are held: the POC/production overlay
(`docker-compose.tunnel.yml`) runs the `clamav` container and sets
`DOCUMENT_SCANNER=clamd`, `DOCUMENT_CLAMD_ADDRESS=clamav:3310` and
`DOCUMENT_REQUIRE_CLEAN_SCAN=true` itself, and production settings refuse to
start on the POC or in production without them (D57). Local development
(`docker compose up`) has no scanner.

Every upload is scanned while it is stored. If ClamAV does not answer (it
needs a few minutes after a restart to load its signatures, or the container
is down), the file is kept but cannot be downloaded: it is **waiting for a
scan**. The worker tries it again two minutes after the upload and then every
10 minutes while ClamAV answers, so waiting files clear by themselves once
the scanner is back.

System status → **Waiting for a scan** lists each waiting file: where it
belongs (Document library, ARC request attachment, Owner request photo, ...),
when it was uploaded, how long it has waited, its size, how many scans were
tried and what the last one said (scanner unreachable, timed out, never
attempted). No names, titles or owners: sysadmins see operations data only.
A file is **stuck** after 15 minutes; the health check "Files waiting for a
malware scan" fails after 30.

When files are stuck:

1. Look at the **Malware scanner** health line. "Not answering": check the
   container (`dc ps clamav`, `dc logs --tail 30 clamav`) and give it a few
   minutes after a restart.
2. Once it answers, press **Scan waiting files now** (sysadmin; audited with
   counts only). The worker scans each file within a minute or two.
3. A file that keeps timing out is probably very large; the scan is retried
   every 10 minutes.

Everything already stored can be scanned again (after a signature update, for
example); a file found infected later is blocked from download immediately:

```bash
dc exec web python manage.py rescan_files --actor you@example.org --reason "signatures updated"
```

## Container image updates (D57)

The infrastructure images (cloudflared, ClamAV, Postgres, SeaweedFS, Mailpit)
are pinned in `docker-compose.yml` and `docker-compose.tunnel.yml` to a
version tag and a digest, so the server runs exactly what was tested.
Dependabot (`docker-compose` in `.github/dependabot.yml`) checks weekly and
opens a pull request for each new release; CI validates both compose files,
builds the portal image and scans the images with Trivy. After the pull
request is merged, the update reaches the VM like any other: System status →
Install now (or Install tonight). A new Postgres major version is not
proposed: it needs a dump and restore, planned separately.

cloudflared used to follow `latest` with a daily pull on the VM
(`hoa-cloudflared-update.timer`); `start-portal.sh` removes that timer. Keep
merging the cloudflared pull requests: Cloudflare stops supporting releases
older than about a year.

The portal image's base (`python:3.13-slim-bookworm` in the `Dockerfile`)
stays on its tag: `start-portal.sh` refreshes it weekly for security fixes,
and Dependabot's `docker` entry proposes new tags. The Postgres image of the
CI test jobs (`.github/workflows/ci.yml`) also stays on its tag: Dependabot
does not update service images in workflows.

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

## Go-live checklist (before real owners, real data or live payments)

Work through it with the board; tick each item in the go-live notes.

**Accounts the HOA owns** (never a volunteer's personal account; at least two
board members can sign in to each, with MFA):

- [ ] Cloudflare: the domain (registrar and DNS zone), the tunnel, and Zero
      Trust / Access (remove or replace the POC hostname's Access rule).
- [ ] Stripe: the HOA's own account, verified, with the restricted key made
      as in "Online payments (Stripe)" (live keys need
      `DEPLOYMENT_STAGE=production` and `PAYMENTS_LIVE_ALLOWED=true`).
- [ ] Teller (bank feed, if used): the HOA's own application, certificate and
      enrollment.
- [ ] Twilio (urgent texts, if used): the HOA's account and number, with its
      A2P registration.
- [ ] Gemini (scanned acknowledgement forms, if used): an API key on terms
      that do not use submitted data for training (a paid tier or an
      agreement that says so). Until then, do not send real signed documents
      to it: leave `GEMINI_API_KEY` empty and read the forms by hand.
- [ ] Email (Hostinger or another provider), the GitHub repository and the
      hosting account.

**Production settings** (the portal refuses to start without most of them):

- [ ] `DEPLOYMENT_STAGE=production`.
- [ ] Malware scanning: `DOCUMENT_SCANNER=clamd`,
      `DOCUMENT_CLAMD_ADDRESS=clamav:3310`, `DOCUMENT_REQUIRE_CLEAN_SCAN=true`
      (set by the tunnel overlay; enforced), and no file left under System
      status → Waiting for a scan.
- [ ] `EMAIL_REDIRECT_ALL_TO` empty (enforced in production).
- [ ] `SECURE_HSTS_SECONDS` unset or at least `31536000` (enforced);
      `SECURE_HSTS_INCLUDE_SUBDOMAINS=true` once every subdomain serves HTTPS.
- [ ] `SEED_DEMO_ALLOWED=false` (ignored outside the POC anyway),
      `ADMIN_ENABLED=false` (enforced).
- [ ] Fresh secrets generated for production (never reuse POC secrets).
- [ ] Container images pinned by digest (they are) and the open Dependabot
      pull requests reviewed and merged.

**Backups**:

- [ ] Off-site backups to a locked bucket (`setup-offsite-backups.sh`, lock
      proved with `check_offsite_lock`). In production the Backups health
      check fails without an off-site copy less than 36 hours old.
- [ ] A restore drill passed (Admin → Server tasks → Restore drill, and once
      from the off-site copy) and written up. In production the health check
      fails when the last passed drill is more than 90 days old.
- [ ] Proxmox/VPS backups that contain `.env` are encrypted.

**Before the first real vote and first real owners**:

- [ ] The deployed version (System status → Version) includes the
      secret-ballot fix (secret ballots stored unlinkable, D21 and the
      security review). Do not open a real secret ballot on an older version.
- [ ] Audit IP retention confirmed with the association's counsel.
- [ ] At least two board members with MFA for every action
      (`check_coverage` clean).
- [ ] Owner sign-in help page and spare hardware security keys ready.
- [ ] Board decisions in `docs/security-review-2026-11.md` section 3 made,
      and the findings of the latest full regression run (`/regression`,
      which includes a full security audit) closed or accepted.

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

### The incident timeline and the other checks (D74)

System status → Incident timeline (and "See the whole timeline", `/ops/timeline/`)
is one list, newest first, of what happened to the portal as a system in the
last 90 days: each health check when it **started failing** and when it
**recovered**, each health email (and one that reached nobody), backups,
off-site copies and passed restore drills, updates the VM agent installed or
failed, host-security alerts (Falco, file integrity, audit; the details stay in
the email), bursts of bounced email, each kind of server error once when first
seen, and server tasks when they finished. Filter by kind at the top; Older and
Newer page through 25 at a time. Read it after an alert to see what else
changed around the same time (a failed update just before errors started, a
backup that stopped, the worker going quiet). It holds operations words and
counts only: never a name, an address, a home or a message. Sysadmins only.

The checks added with it:

- **Background jobs**: a job that failed in the last day, keeps retrying (more
  than 3 tries), has hung (running over 45 minutes) or is waiting over 20
  minutes to start. System status → Background job problems says which job in
  plain words with its task name underneath. A failed job runs again at its
  next time; if one keeps failing, look in the worker's log for the task name
  (`sudo docker compose logs worker --since 24h | grep <task name>`), fix the
  cause, then **Clear** the check. A backlog with "Background worker" failing
  means the worker is down: `sudo docker compose restart worker`.
- **Payment processing** (once Stripe is set up): payments that did not match
  Stripe's record, automatic payments that failed for a technical reason,
  Stripe not answering or refusing the key (`sudo docker compose exec web
  python manage.py check_stripe`; re-run `set-stripe-key.sh` if the key was
  revoked), or an unusual number of declines in a day. One decline is normal:
  the payer was emailed, and a failed automatic payment is on the board's My
  items until it is resolved.
- **Database connections**: above 80% of Postgres's `max_connections` new pages
  may fail; restart the web and worker containers.
- **Storage growth**: from a daily record of the database, the uploaded files
  and the disk, "at this rate the disk is full in about N days"; it fails below
  the board's warning (Settings → Community setup → Server warnings, default 60
  days). Free space (old Docker images: `sudo docker image prune`), grow the VM's
  disk, or move uploads to R2 (`move-files-to-r2.sh`). The figures and a 90-day
  line are on System status → Storage and database growth.

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
`scp` into `/root/teller/`, folder mode 700), then run the guided script and
answer its questions (the Application ID, `development`, and the two file
paths, which default to `/root/teller/`):

```bash
sudo bash /opt/hoa-portal/infra/poc/set-teller.sh
```

It checks that the files are a certificate and a key that belong together
and have not expired, saves them in `.env` (never on a command line), creates
`BANK_FEED_TOKEN_KEY` if there is none, restarts the portal, checks the result
with `manage.py check_teller`, and offers to delete the copied files.
`development` is Teller's free tier for **real** accounts (`sandbox` is for
trying it with fake banks; `production` is Teller's paid tier).
`BANK_FEED_TOKEN_KEY` encrypts the stored access key: it is generated once and
must never change (a new key means reconnecting the bank). Keep a copy in the
password manager. While the check runs the old file is kept as
`.env.before-teller` (mode 600); a failed check says how to put it back, and a
passed check removes it (it holds the old keys, D87). To change it, run the
script again.

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
   - PaymentIntents: Read (**Write** once Automatic payments are used: it
     creates the monthly off-session charge)
   - Charges: Read
   - Balance transactions (Balance): Read
   - Payouts: Read
   - Disputes: Read
   - Refunds: **Read** (never Write)
   - For Automatic payments only (D46): Customers: **Write** (one
     Customer per ownership account, metadata ids only); SetupIntents: Read
     (the saved card or bank account and its debit authorization);
     PaymentMethods: **Write** (to detach a payment method when a payer turns
     automatic payments off, replaces the method, or the home is sold).

   No refund, product, payout or transfer write rights and no account
   settings. With this key a stolen secret can only create checkouts and
   charges that pay the HOA (charges only to payment methods payers saved
   for automatic payments), never send money out. (Because the key cannot refund, a surcharge taken from a
   debit card by mistake becomes a board My item "Refund $X surcharge to lot
   N (debit card)"; refund it in the Stripe dashboard and the portal closes
   the item when it sees the refund.)
3. On the VM, put the key in `.env` (hidden prompt; never paste a key into a
   chat, ticket or the repository). Only a restricted key (`rk_...`) is
   accepted: the script and the portal refuse a full secret key (`sk_...`)
   everywhere (D87), and `check_stripe` says so:

   ```bash
   sudo bash /opt/hoa-portal/infra/poc/set-stripe-key.sh
   ```

4. Settings → Features (board): switch on **Online payments**. Community settings:
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

### Automatic payments (D46)

Built and tested in test mode; live use needs the HOA's own Stripe account
(the same go-live checklist below).

1. Give the restricted key the extra rights above (Customers Write,
   SetupIntents Read, PaymentIntents Write, PaymentMethods Write). Without
   them, setting up fails with a message and nothing is saved; without
   PaymentMethods Write, turning off still stops the charges but the card
   stays on the Customer at Stripe (the audit log shows
   `payments.autopay_detach_failed`; detach it in the dashboard).
2. Settings → Features (board): switch on **Automatic payments** (Online payments must
   be on too). Community settings: **Automatic payments above this wait for
   the board's review** (`payments.autopay_review_cents`, default $500.00, D47)
   with its citation; the due day (`dues.due_day`) is when the charge is made.
   The old percent ceiling (`payments.autopay_ceiling_percent`) is retired: a
   value recorded for it stays in the history and is ignored.
3. As a demo owner: My account -> Pay assessments automatically (asks for
   the password again) -> bank account or card -> Stripe's setup page. Test
   card `4242 4242 4242 4242` (credit, surcharge), `4000 0566 5566 5556`
   (debit, no fee), `4000 0000 0000 9995` (declines with insufficient funds:
   one retry three days later, then paused), `4000 0027 6000 3184` (needs
   authentication: paused at once, the owner pays by hand). Bank: the Test
   Institution, as for Pay online.
4. What it charges: on the due day, site time (the hourly task acts from the
   due day for a week), the open amount of that month's assessment only,
   after any credit on the account, plus the credit card surcharge on a
   credit card. Never late fees or other charges. It is recorded only when
   Stripe says the charge succeeded (cards at once, bank debits after a few
   business days).
5. The board sees every account's status and recent failures on Finance ->
   Online payments ("Automatic payments"). A charge above the review limit
   (surcharge included) is not charged: a My item "Approve automatic payment
   of $X for <address>"; Approve (charged on the next hourly run) or Decline
   with a reason (the owners are emailed) under "Waiting for review". Not
   decided within 14 days, nothing is charged and the owners are told. A
   board member never decides a payment for a home they pay for.
6. Once a year (D47): the payer picks "Once a year" and a month; on that
   month's due day the next 12 months' assessments are charged at once, less
   any credit, and the money is drawn down month by month as credit. Months
   before the first yearly payment (or in a gap after changing the month)
   are charged monthly as usual; no month is charged twice. At the
   default limit ($500) every yearly payment waits for the board's approval.
   No Stripe key rights change.

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

## Disbursements and checks (D52)

Finance → Disbursements pays money back to owners: a **surplus distribution** to every home, or an **owner refund** of one account's credit (usually a former owner's, after a sale: My items shows "Refund $X to the former owner of <address>").

1. **Propose** (board or manager). Surplus: the total, the year, the board resolution (choose it, or type its reference), and any homes to pay by check instead of a statement credit; "Show the split" first. Refund: open it from My items or the "Credit owed to former owners" list; check the payee and type the former owner's forwarding address.
2. **Approve** (a board member other than the proposer; two at or over the rule's amount; a surplus approved by a recipient needs two). Statement credits post on the final approval.
3. **Write the checks.** Either download the **check register** (CSV or PDF) and write the checks in the bank's bill pay or the accounting package, or print them on the association's **pre-printed check stock** ("Print on check stock"). Then "Mark as issued" with the first check number and the date on the checks: the numbers go to the homes in the order listed. A refund is posted on the owner's account at this point.
4. **Clear or void.** "Mark cleared" when the bank statement shows the check (the page suggests the bank feed line with the same amount and check number); the bank reconciliation lists issued checks as outstanding until then. "Void" (board) reverses a refund so the credit is back on the account and a new check can be written.

**Check stock alignment** (Disbursements → Check printing): print the alignment test page on plain paper at 100% (turn off "fit to page"), hold it over a blank check against the light, and adjust each field's position (hundredths of an inch from the top-left corner of the page; "Move everything" shifts all fields for a printer that feeds a little off). Print one real check on a voided blank before a batch. The portal never needs, and never stores, the bank's account or routing numbers: they are already on the stock.

**Uncashed checks.** After 90 days an uncashed check is a My item. Pennsylvania's unclaimed property law applies to checks that stay uncashed: after the dormancy period the money must be reported and paid to the PA Treasury instead of being kept or re-credited. The treasurer should confirm the dormancy period and the reporting steps (and counsel the wording of any letter) before voiding or reissuing. Voiding restores the owner's credit on the account; the board then follows the Treasury's reporting process for it.

**For counsel before the first surplus distribution:** 68 Pa.C.S. § 5313 (surplus funds) applies unless the Declaration provides otherwise; confirm what the Declaration says, whether reserve funds may be treated as surplus, and record the citation on the rule in Community settings.

## Treasurer's guide: special assessments, payment plans, reimbursements, checks, next year's budget (D75-D78)

The association is self-managed, so these are the treasurer's (and the board's) steps. Each opens from Finance → Finance overview unless noted.

**Special assessment** (Special assessments → Levy a special assessment)
1. Adopt the board resolution first (Resolutions and policies, or note its reference). If the total is above the Declaration's limit (Community setup → "Special assessments, payment plans and reimbursements"), hold the owners' vote first and link it.
2. Levy: title, purpose, total, fund (operating or reserve), number of installments, first due date and spacing. Equal shares per home unless the community's rule says otherwise.
3. Send the owner notice from the special assessment's page (email at once to owners who accepted email; prepare paper copies on Paper mail for the rest).
4. Installments post by themselves each morning on their due dates; "Post installments due now" does it at once. To stop future installments, use "Cancel future installments" with the reason; a posted charge is corrected only by a reversal on the home's account.

**Payment plan** (from the home's collection case → Set up payment plan)
1. The plan covers what is open on the home's account today. Enter the number of monthly installments and the first due date, or type your own schedule (one line per installment: `2027-01-15 150.00`).
2. Another board member approves it (Payment plans, or My items); the case then shows "Payment plan agreed". Download the agreement letter, mail it, and ask the owner to agree on My account or sign and return the letter.
3. Missed installments show on My items; after the default period the plan ends by itself and the case returns to its earlier step (letters and late fees resume). A paid-off plan closes the case.

**Expense reimbursement** (a board member or committee member who paid an association expense: My reimbursements → Claim an expense, linked from the Finance overview and their committee's page, D95; owners on neither do not claim)
1. Submit the amount, date, shop, category and description with the receipt.
2. A board member other than the claimant approves (two above the set limit). Nobody approves or pays their own claim; a board member's claim is paid by another board member, never the manager.
3. Pay by check from Checks, or "Record bank transfer" with the transfer's reference (never account numbers).

**Checks** (Checks)
1. Tick the approved invoices and reimbursements to pay; print them on the check stock ("Print checks (PDF)", 100% scale). Test the alignment first with Disbursements → Check printing → alignment page on plain paper; the positions are shared with refund checks.
2. Sign and mail, then "Record as issued" with the first check number on the stock: each invoice is recorded as paid by its check. A number is used once across all the association's checks.
3. When the bank statement shows the check, mark it Cleared (the bank feed line is suggested when there is one). A lost or wrong check is voided by a board member; the invoice is open again.

**Next year's budget** (Budgets → Start next year's budget)
1. Choose what each line starts from (this year's budget, actual spending, or the higher), any increase, and whether to use the adopted reserve plan's contribution.
2. Check the draft on "Budget compared": last year's budget, actuals and the new figures, with the monthly charge per home (rounded up as the board's rule says) and the change.
3. Edit lines on the budget page, send the proposed budget to owners, adopt it, then send the adopted notice and set the new monthly amount in Community settings.

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
| Reseed with demo data | `infra/poc/reseed-demo.sh <dataset>` (only on the POC with demo data on; started from Settings → Demo data) | 25 min |

If the page says tasks will not run, the agent has not been seen for five
minutes: check `systemctl status hoa-portal-agent.timer` and
`journalctl -u hoa-portal-agent.service` on the VM. Steps that need a secret
typed by a person (`setup-offsite-backups.sh`, `set-stripe-key.sh`,
`move-files-to-r2.sh`) and one-time setup (`provision-vm.sh`, the first
`harden-host.sh`) stay in the terminal.

## Reseed the POC with demo data (D53)

Only on the POC (`DEPLOYMENT_STAGE=poc` and `SEED_DEMO_ALLOWED=true` in
`.env`); on a real deployment the page does not exist and the agent refuses.

1. Sign in as a sysadmin with your own login (passkey or app), open
   **Settings → Demo data**. It shows which dataset the portal holds now.
2. Pick a dataset: **Oak Creek Farms (full demo)** (three years of history)
   or **Fresh start (just handed over)** (44 homes and owners, a
   declarant-appointed board of three, a sysadmin, a placeholder Declaration,
   nothing else: good for trying the setup and invitations). Type `RESEED`
   and press **Reseed with this dataset**.
3. Within a minute the agent takes a backup (the reseed stops if it fails),
   saves the real logins, stops the portal, empties the database, migrates,
   puts the real logins back, seeds the dataset, removes the old data's
   files and starts the portal (about a minute without the site, more for
   the backup). Sign in again with your own passkey or app: your login,
   authenticators, recovery codes, person and sysadmin role are kept, and so
   are other non-demo logins and the server-task history. With "After
   reseeding, make me a board member and the owner of a demo home in the new
   data" ticked (the default), you also get a declarant-appointed board seat
   from today and become joint owner of the first demo home with a single
   owner; untick it to come back as sysadmin only (then
   `demo_make_me_owner` gives you a home). Other kept logins get only their
   login, person and sysadmin role.
4. The result is on Server tasks (and Demo data). Full output:
   `/var/log/hoa-portal-tasks.log` on the VM.

By hand on the VM: `sudo bash /opt/hoa-portal/infra/poc/reseed-demo.sh fresh_start`.
If it fails before "Emptying the database", nothing was changed. If it fails
while seeding or removing files, the real logins are already back: sign in
and reseed again. If it fails in between (migrations, putting the logins
back), the portal is empty and the saved logins are shredded: restore the
backup it took first ("Backups and the restore drill"). The audit log of
the replaced data is in that backup only.

## Printing paper notices (Paper mail, D55)

Association → Paper mail shows who gets paper (owners on the deed who have not agreed to official notices by email) and the mailings. Board members and the manager can prepare and print.

1. **Once: check alignment.** Settings → Paper mail settings: choose single- or double-window #10 envelopes, then print the test letter. Print at **actual size / 100% (no "fit to page", no scaling)**, fold in thirds on the dashed lines, put it in an envelope and check the address shows fully in the window. Adjust the inches (recipient block from the left and top; return address for double windows) and print again. Do the same with the test envelope and the test label sheet; use the shifts to correct a printer that prints slightly off.
2. **Envelopes:** feed #10 envelopes as the printer's manual says (usually the short edge first, flap closed, printed side as marked on the tray); choose the envelope paper size "#10 (4.125 x 9.5 in)" and landscape if the driver asks. Labels: Avery 5160 or compatible, plain paper setting, actual size; "Start at label" reuses a partly used sheet.
3. **A mailing:** from a meeting page (Prepare paper notices), the budget page after its notice, or Paper mail → New mailing (blank, meeting, budget, or a Letters and certificates wording). Preview, then Prepare mailing. On the mailing page print the letters (tick "start every letter on a front side" when printing both sides), and envelopes or labels unless you use window envelopes.
4. **After posting:** "Mark as mailed" with the date (or "Not sending this"). Until then the mailing stays on My items; for a meeting, the meeting page then shows "Paper notices mailed <date> to N homes".

**Tracking barcode (optional, free).** Create a USPS Business Customer Gateway account to get a Mailer ID, and sign up for Informed Visibility (IV). Enter the Mailer ID on Paper mail settings, keep Barcode ID 00, choose the Service Type ID (310 is suggested for First-Class with IV tracking: confirm with USPS), and tick "Print the tracking barcode". Window letters and envelopes then carry the barcode above the name (labels are too small for it). To follow a mailing, sign in to IV and search by Mailer ID and serial range (the mailing's pieces are numbered consecutively; the portal keeps each letter's serial and home). USPS returns scans in IV (and its API) only for barcodes carrying the HOA's own Mailer ID, and keeps scan data for 30 days, so look within a month. The portal can also read the scans itself: see "USPS tracking (Informed Visibility)" below.

## USPS tracking (Informed Visibility)

With USPS tracking on, the portal reads USPS scans of barcoded paper mail itself (decision D60): a few times a day, for 30 days after a mailing is marked as mailed. Nothing is sent to USPS but the barcode numbers; USPS never calls the portal.

1. **USPS accounts.** The association needs a USPS Business Customer Gateway (BCG) account (it also gives the Mailer ID for the barcode, see "Printing paper notices"). The account's Business Service Administrator (BSA), or a BSA Delegate, gives the user the portal will sign in as access to the **Informed Visibility Mail Tracking & Reporting (IV-MTR)** service. Use a dedicated user for the portal, with a long password kept in the HOA's password manager.
2. **Test first (CAT).** USPS has a test environment, CAT. Ask InformedVisibility@usps.gov for a CAT test account for IV-MTR. USPS is changing its sign-in in 2026 and it is not confirmed whether the API's sign-in changes with it, so always prove the connection in CAT before production.
3. **Enter the login on the server** (never on the portal's pages, by email or in a chat):

   ```
   sudo bash /opt/hoa-portal/infra/poc/set-usps.sh
   ```

   Choose `cat` (the default) or `production`, type the BCG user name and the password (hidden). The script saves them in `.env` (the old file is kept as `.env.before-usps`, mode 600, until the check passes, then removed, D87), restarts the portal and signs in once with `manage.py check_usps_iv` in the worker container, the only one given the password (no mail is looked up). It prints `OK: signed in to ...` or what failed; a failed check says how to put the old file back. To change the login, run it again. When CAT works, run it again with `production`.
4. **Switch it on:** Settings → Features (board) → USPS tracking. Settings → Paper mail settings then shows "Connected to the USPS test environment (CAT)" (or "Connected to USPS Informed Visibility") and when it last checked. The tracking barcode must be on for the mailing: only letters printed with it can be followed.

**What the statuses mean** (mailing page, USPS tracking card):

| Status | Meaning |
|---|---|
| Mailed | Marked as mailed; USPS has not scanned it yet. |
| Processed | USPS has scanned it on its way. |
| Sorted for delivery on <date> | Sorted into the carrier's delivery order (operations 918/919). Usually delivered that day or the next. USPS gives actual delivery scans only to bulk mailers who file an electronic mailing statement (eDoc), so this is normally the last scan. |
| Delivered on <date> | A delivery scan (rare without an eDoc). |
| No scans after 5 days | Never scanned 5 days after mailing: the address or the barcode may be wrong, or it was not mailed. It goes on My items; check the address, then press "Address checked". |
| Gave up after 30 days | USPS keeps scans for 30 days; the portal stops asking. |
| USPS would not show it | USPS answered "not yours": the barcode's Mailer ID is not on this BCG account. |

**Problems.** System status → "USPS tracking" fails when USPS refuses the sign-in (the sysadmins also get one email a day: re-run `set-usps.sh`, check the password has not expired and the user still has IV-MTR access), when USPS refuses every letter (check that the Mailer ID on Paper mail settings belongs to this account), or when no check has worked for a day (the worker, or USPS, is down). The portal keeps under 400 requests a minute (USPS allows 500).

## Bounced email (D74)

With **Bounced email** on, the portal reads bounce reports from a mailbox the
association owns every 20 minutes (IMAP over TLS, pull only; nothing calls the
portal). A hard bounce (the address does not accept mail) shows on the
person's page for the board and on their own My profile; a hard bounce of an
**official notice** (meeting or budget notice to an owner who chose email) puts
"Send a paper copy to <address>" on My items and on Paper mail → Official notices
that bounced, until a mailing for that home is prepared or someone presses
"Sent another way".

1. **A mailbox.** Best: a dedicated address, e.g. `bounces@<your domain>`, at
   the association's mail provider, with IMAP switched on (and an app password
   where the provider needs one). Keep its password in the HOA's password
   manager.
2. **Make bounces reach it.** Either set the portal's sender address
   (`DEFAULT_FROM_EMAIL`) to an address whose bounces land in that mailbox, or
   give the mailbox's own address when the script asks for the envelope sender
   (`BOUNCE_RETURN_PATH`): outgoing mail is then sent with the envelope sender
   `bounces+<token>@<domain>` (the From line people see does not change), so the
   provider must deliver plus-addresses to the mailbox (most do). Check that SPF
   still passes for that domain (see "Email deliverability").
3. **Enter the login on the server** (never on the portal's pages, by email or
   in a chat):

   ```
   sudo bash /opt/hoa-portal/infra/poc/set-bounce-mailbox.sh
   ```

   It asks the IMAP host (e.g. `imap.example.org`), the port (993), the user
   name, the folder (INBOX), the optional envelope sender and the password
   (hidden), changes only the BOUNCE_ keys in `.env` (D92; a copy is
   kept as `.env.before-bounce-mailbox`, mode 600, while it runs and removed
   afterwards), restarts the portal, signs in once with
   `manage.py check_bounce_mailbox` in the worker (the only container with the
   password; no message is read) and sends a test email. A failed check puts
   back only the BOUNCE_ settings; if the test email fails with the envelope
   sender, that one setting is switched off again (many SMTP relays refuse it).
   With SMTP2Go (`set-smtp2go.sh`) no bounce mailbox is needed: SMTP2Go reports
   bounces. To change the mailbox, run it again.
4. **Switch it on:** Settings → Features (board) → Bounced email. System status then has a
   "Bounce mailbox" check (last read, hard bounces in the last 7 days), and the
   Community setup's server step shows it as set up.

**How a bounce is matched.** Every email the portal sends carries a signed
token (an `X-Portal-Ref` header, inside its Message-ID, and in the envelope
sender when set up). A report counts only if the token's signature checks, it
is under 45 days old and the address it names is one the message went to, so a
forged bounce can mark at most the forger's own address (BUG-002). The portal
reads only the report's status lines and returned headers, never attachments,
and keeps a keyed hash of the failed address, never the address or the text.
Messages it read are marked as read in the mailbox; empty the folder now and
then, or let the provider's retention do it.

**When the check fails.** "the mailbox refused the user name or password": the
password changed or the app password was revoked, run the script again; "could
not be reached": host, port or firewall; "certificate did not check out": the
host name is not the one on the server's certificate (use the provider's IMAP
host name, and port 993 for IMAP over TLS). A single failed read does not alert;
two hours without a successful read does.

**A bounced address.** On the person's page, ask the owner for a working address
(they see the same note on My profile). Once it is fixed, press "Mark as fixed";
if the next email bounces too, it shows again. For a bounced official notice,
use "Prepare the letter" on Paper mail (it prepares a letter for that one home,
whatever their email choice) and mark the mailing as mailed as usual.


## Month-end and year-end close (D70, D71)

**What happens by itself.** Every day the portal codes new bank lines by the board's rules, confirms bank matches that can only be one thing, and from the close day (rule `books.close_day`, the 3rd) checks last month. If every bank line is matched or coded, every account's balance at month end is known and reconciles, the month's assessments and late fees are posted (or waived), no payment waits to be deposited, the Stripe payouts are matched, transfers have both sides and no fund is below zero, the month closes by itself. Once a fiscal year's twelve months are closed the year closes too, the tax filings are put on Recurring deadlines and the year-end package is built.

**First time.** Accounting → Setup: the opening date (the books start the next day). Finance overview → Bank accounts and funds: each account's fund (operating, reserve), chart account and balance on that date; tick the primary operating account. Finance overview → Bank categories and rules: check the categories, add rules for lines that repeat ("INTEREST" → Interest income, "SERVICE CHARGE" → Bank fees, "TRANSFER TO SAVINGS" money out and "TRANSFER FROM CHECKING" money in → Transfer between the association's accounts).

**When My items says "<Month> books: N exceptions".** Open Finance → Month and year close. What each means:
- *A bank line is not matched or coded*: code it to a category (tick "lines like this" to make a rule), or match it on the Bank feed (a deposit, payout or check).
- *Enter the balance on <date>*: the bank did not report a month-end balance. Import the month's OFX/QFX file (it carries the ledger balance) or type the statement's ending balance.
- *Off by $X*: usually a missing bank line, a wrong opening balance or an item matched to the wrong account. If it is genuinely right (a bank error corrected next month), explain it; another board member must agree.
- *No late fee*: post the month's late fees, or waive one with the reason (a board decision; never your own home).
- *Payment waiting to be deposited*: record the deposit on Accounting.
- *Payout not matched or not arrived*: match it on the Bank feed; one Stripe has not paid yet stays until it arrives.
- *Transfer with no matching line*: code the other account's line as a transfer too; a transfer in transit at month end can be explained.
- *Fund below zero*: money was spent from the wrong fund or an entry is missing.
- Warnings (stale checks, reserve contribution short) never stop the close.

**Mistakes in a closed month.** Post the correction in the open month (coding a line again, a reversal): the portal adds the reference to the original date. Reopening needs a board member's request and a different board member's approval; only the latest closed month, and only while its year is open.

**Year end.** Finance → Financial statements → the fiscal year: the statements (PDF, CSV) and the year-end package. The package's manifest.json is signed with `BOOKS_PACKAGE_KEY` (set it in the server's .env and keep a copy with the treasurer's records); on the server `python manage.py verify_books_package <file.zip>` checks it; elsewhere use the snippet in the package's README.txt with the key. Finance overview → Tax worksheet: Form 1120-H line by line with the 60%/90% tests, the 1099 IRIS upload (add each vendor's TIN from the W-9 in IRIS; the portal keeps none) and recipient copies. Nothing is filed by the portal; the preparer confirms the form and the figures.

## Vendor onboarding (D79)

**Key set-up (sysadmin, once, on the VM).** `sudo bash infra/poc/set-vendor-key.sh` makes the vendor document key on the server (nothing to type), saves it in `.env`, restarts the portal and runs `manage.py check_vendor_key`. Then copy the key into the HOA's password manager (`sudo grep -E '^VENDOR_DOCUMENT_KEY' /opt/hoa-portal/.env`) (the script removes its `.env.before-vendor-key` copy itself, EXP-004). Keep that copy apart from the backups: the backups hold the encrypted documents only. The setup wizard's server step shows "Vendor document key: Set up".

**Rotating the key.** `sudo bash infra/poc/set-vendor-key.sh --rotate` keeps the old key in `VENDOR_DOCUMENT_KEY_FALLBACKS`, makes a new one and re-encrypts every stored document; `check_vendor_key` then reports "0 under an old key". Remove the fallback only after that, and store the new key in the password manager. A restore needs the key that was current when the backup was taken (or the fallbacks).

**Switching it on.** Settings → Features (board) → Vendor onboarding (or the Vendors step of Community setup, where the link life, insurance minimums and W-9 retention are set with citations). Finance → Vendors → Vendor requirements and terms: the board ticks what each kind of vendor provides (with a citation) and edits the terms.

**Sending a link (board or manager).** Finance → Vendors → Vendor onboarding → Invite a vendor: name, email, services and kind of work. The vendor is created inactive and gets an email with the link (it works until it expires or they submit). "Send a new link" on the request ends the old one. For a vendor added before onboarding, open its Onboarding record → Complete onboarding; for an expired certificate or a new W-9, Ask for updated documents (the link shows only those items).

**Reviewing (board).** My items lists "Review vendor onboarding: <vendor>". Check the answers, the warnings (insurance below the minimum, expired certificates), the documents (W-9s open for board members only; every view is logged) and any conflict disclosure; link anyone it names (they cannot approve the vendor or its invoices). Approve, Return to the vendor (say what to change: they get a new link), or Decline. Two different board members approve; the one who sent the link does not. Until then the vendor's invoices cannot be approved or paid.

**W-9 retention.** My items shows "W-9s past retention" when any are due; Finance → Vendors → W-9 retention: one board member proposes disposal, a different one confirms.

