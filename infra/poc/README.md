# POC virtual machine (Proxmox)

The POC runs on one VM on an isolated VLAN. It is reachable only through the
Cloudflare tunnel `hoa-portal-poc`; nothing on the VM accepts inbound
connections from the internet or other VLANs.

## Install in one command (D94)

Replace `v0.44.0` below with the newest release on GitHub (Releases; always a
`v` tag, never `main`). The script then fetches the newest release itself.

0. **Optional: make the server.** Any Ubuntu 24.04 LTS server with 2 CPUs,
   4 GB of memory and 40 GB of disk works. On Proxmox, `create-vm.sh` makes
   one (first a UniFi network `hoa-poc`: its own VLAN ID and subnet, DHCP on,
   "Isolate Network" on, internet access on, mDNS off). Proxmox host, web UI
   -> node -> Shell:

   ```bash
   curl -fsSLO https://raw.githubusercontent.com/Legal-Copy7045/HOA-Portal/v0.44.0/infra/poc/create-vm.sh
   ```

   ```bash
   less create-vm.sh
   ```

   ```bash
   VLAN_ID=50 BRIDGE=vmbr0 STORAGE=local-lvm bash create-vm.sh
   ```

   Save the console password it prints in your password manager.

1. **Get the tunnel token**: Cloudflare dashboard -> Zero Trust -> Networks
   -> Tunnels -> your tunnel (or Create a tunnel -> Cloudflared) ->
   Configure. Its Public Hostname is the portal's address (for example
   `portal-dev.oakcreekhoa.com`) with service `http://web:8000`. Copy the
   install command it shows: only the token in it (the long text starting
   `eyJ`) is kept.

2. **Install** (the server's console, e.g. Proxmox VM -> Console ->
   **xterm.js**, which supports paste; log in as `hoa`):

   ```bash
   curl -fsSL https://raw.githubusercontent.com/Legal-Copy7045/HOA-Portal/v0.44.0/infra/poc/install.sh | sudo bash
   ```

   To read it first: `curl -fsSLO <the same address>`, `less install.sh`,
   `sudo bash install.sh`.

   It asks for the tunnel token (typing is hidden), the portal's address
   (Enter keeps `portal-dev.oakcreekhoa.com` and skips the check from
   outside) and your name and email as the first board officer and sysadmin
   (Enter to do it later). Then it runs on its own for 15-25 minutes:

   - checks the server first (Ubuntu 22.04 or newer, CPUs, memory, disk,
     internet access) and changes nothing if one fails;
   - fetches the newest release into `/opt/hoa-portal`;
   - `provision-vm.sh`: security updates, SSH keys only, firewall, Docker;
   - `make-env.sh`: `.env` with every server secret generated on the server
     (mode 600; `COMPOSE_FILE` with both compose files, D91);
   - `harden-host.sh`: automatic updates, auditd, AIDE, Falco, the outbound
     firewall for the containers (below);
   - `start-portal.sh`: builds and starts the portal and the update agent
     (which also runs Admin -> Server tasks); `health-timer.sh`;
   - checks that every container is healthy, the database is migrated,
     cloudflared is on the `edge` network only, the outbound firewall is on
     and the tunnel is connected
     (and, if you gave the address, that it answers from outside);
   - prints a one-time sign-in link for you on the screen (never in the log).

   Open the link, choose a password, add a passkey or an authenticator app
   and save the recovery codes. Everything else (email, payments, rules) is
   set up in the browser: Settings -> Community setup. Copy `.env`'s secrets
   into the HOA password manager.

   Connected over SSH instead of the console? The install closes inbound
   SSH (the portal needs none) and stops first to ask you to choose:
   `--admin-cidr <your address>/32` keeps SSH from your address (keys only),
   `--close-ssh` closes it. With curl, options go after `bash -s --`:

   ```bash
   curl -fsSL https://raw.githubusercontent.com/Legal-Copy7045/HOA-Portal/v0.44.0/infra/poc/install.sh | sudo bash -s -- --close-ssh
   ```

   **Running it again is safe**: finished steps are skipped, `.env` and its
   secrets are kept (never regenerated), no data is touched, and the checks
   run again. If a step failed, the message says what to do, then run the
   same command again and it continues there. `--repair` runs every step
   again; `--new-token` replaces only the tunnel token. The log, with no
   secrets, is `/var/log/hoa-portal-install.log`.

   For automation, the token comes from a file or stdin, never the command
   line: `sudo bash install.sh --token-file /root/tunnel-token --hostname
   portal.example.org --officer-name "Pat Example" --officer-email
   pat@example.org` (`--help` lists every option).

3. **Optional: off-site backups** to a locked Cloudflare R2 bucket that a
   compromised portal cannot delete, deliberately a separate step on the
   server: `sudo bash /opt/hoa-portal/infra/poc/setup-offsite-backups.sh`
   (see the instructions at the top of that script).

## The same, step by step

The scripts install.sh runs still work on their own, in this order (each is
safe to re-run): `sudo bash provision-vm.sh` (downloaded as above; it checks
out the newest release), then in `/opt/hoa-portal` as `hoa`:
`bash infra/poc/make-env.sh` (asks for the token and an optional catch-all
email), `sudo bash infra/poc/harden-host.sh`, `sudo bash
infra/poc/start-portal.sh`, `sudo bash infra/poc/health-timer.sh`, and the
first officer: `sudo docker compose exec web python manage.py
bootstrap_first_officer --name "Your Name" --email you@example.org
--also-sysadmin` (docs/runbook.md, "First-time setup").

## Email through SMTP2Go

The portal sends through SMTP2Go's HTTP API and reads delivery and bounce
reports back from SMTP2Go every five minutes (pull only: no webhook, and no
bounce mailbox is needed). In the SMTP2Go dashboard, once:

1. **Sending → Verified Senders → Sender Domains → Add**: your domain (the
   part after @ in the portal's From address). SMTP2Go then shows the exact
   records to add at your domain's DNS host; generically they are:

   | Purpose | Type | Name | Value |
   |---|---|---|---|
   | SPF | TXT | `@` (the domain) | `v=spf1 include:spf.smtp2go.com ~all` (add the include to an existing SPF record instead of making a second one) |
   | DKIM | CNAME | `s<nnn>._domainkey` | `dkim.smtp2go.net` |
   | Return path (bounces) | CNAME | `em<nnn>` | `return.smtp2go.net` |
   | Link tracking (optional) | CNAME | `link` | `track.smtp2go.net` |
   | DMARC (recommended) | TXT | `_dmarc` | `v=DMARC1; p=none; rua=mailto:dmarc@<your domain>` |

   Use the names and numbers SMTP2Go shows (`<nnn>` is per account), wait
   for "Verified", and leave open and click tracking off.
2. **Sending → API Keys → Add API Key**: permissions *Emails* (send),
   *Activity* (search) and *Statistics* (the harmless key check) only.
   Copy the key once into the HOA password manager.
3. On the VM:

   ```bash
   sudo bash infra/poc/set-smtp2go.sh
   ```

   It asks for the region, the sender address and name, and the key (hidden),
   checks the key with SMTP2Go (no email is sent), changes only its own keys
   in `.env` (`EMAIL_PROVIDER`, `SMTP2GO_API_KEY`, `SMTP2GO_REGION`,
   `DEFAULT_FROM_EMAIL`), restarts web and worker, checks again from the
   portal and sends a test email. If anything fails, only those keys are put
   back. Back to plain SMTP: `sudo bash infra/poc/set-smtp2go.sh --off`.
4. Settings → Features (board) → Bounced email on, so addresses that bounce are flagged.

Every set-*.sh script edits `.env` through `lib.sh` (`env_update`): only
its own keys change, every other line is kept, the file stays mode 600, and
the `.env.before-*` copy is shredded when the script ends.

## Settings

`create-vm.sh`: `VLAN_ID` (required), `BRIDGE` (vmbr0), `STORAGE` (local-lvm),
`VMID` (next free), `IP_CONFIG` (ip=dhcp, or `ip=10.50.0.10/24,gw=10.50.0.1`),
`SSH_KEY_FILE` (optional), `CORES`, `MEMORY_MB`, `DISK_SIZE`.

`provision-vm.sh`: `ADMIN_CIDR` (allow SSH from one admin address; default
none), `BRANCH` (default: the newest release tag; a branch name follows that
branch on purpose), `REPO_URL`, `APP_DIR`, `APP_USER`.

`install.sh`: the options in `--help`; `APP_DIR` (`/opt/hoa-portal`),
`APP_USER` (the checkout's owner, else `hoa`), `REPO_URL`.

Both scripts check before changing anything and are safe to re-run.

## Updating to a new version

One command moves the checkout to the newest release tag (`v<VERSION>`, made
by CI when a version bump merges; D90), shows what changed, rebuilds the image
on the VM, restarts (migrations run automatically) and prints the record
counts. It refuses to pull over local edits and does nothing if there is
nothing new (`--force` restarts anyway); `--ref <branch>` deploys that
branch's tip on purpose (`--ref main` for unreleased code). Optional shortcut, once:
`sudo ln -sf /opt/hoa-portal/infra/poc/update-portal.sh /usr/local/bin/hoa-update`,
then `sudo hoa-update`.

```bash
sudo bash /opt/hoa-portal/infra/poc/update-portal.sh
```

The "Install update" button on System status installs only when the VM's
checkout is on `main` (decision D25: main receives only CI-tested merges).
On any other branch it refuses and says why; set `ALLOW_BRANCH=<branch>` in
`.env` only for a deliberate test. Move an older VM to main once with
`git -C /opt/hoa-portal checkout main` (as the hoa user), then update.

Migrations run automatically. If the POC was seeded before Milestone 2, add
the demo data for the new modules once:

```bash
docker compose -f docker-compose.yml -f docker-compose.tunnel.yml exec web python manage.py seed_demo_operations
```

## Compose files always together (D91)

On the VM the portal is always `docker-compose.yml` plus
`docker-compose.tunnel.yml`. `.env` holds
`COMPOSE_FILE=docker-compose.yml:docker-compose.tunnel.yml` (written by
`make-env.sh`; `start-portal.sh` and `update-portal.sh` add it to an older
`.env`, appending one line and touching no other), so a plain
`docker compose ...` in `/opt/hoa-portal` uses both files too. Without it,
`docker compose up -d --force-recreate web worker` recreated web without the
`edge` network and cloudflared lost the site.

cloudflared is on the `edge` network only (security review E3/E9): it must
never reach Postgres or the file store. Never fix a lost tunnel with
`docker network connect <project>_default <cloudflared>`; run
`sudo bash infra/poc/update-portal.sh` (or `start-portal.sh`), which
disconnects cloudflared from any other network every time it runs. The
"Tunnel network" health check on System status fails while cloudflared is on
another network (the agent reports it each minute).

To restart web and worker, use System status -> Restart the portal (a server
task: sysadmins, a fresh sign-in check and a confirmation, at most once every
10 minutes), or `sudo bash infra/poc/restart-portal.sh` on the VM. Both use
both compose files.

## Outbound firewall (D101)

The portal's containers may connect only where the portal needs to. This is
part of host security: the install sets it up, and System status -> Server
tasks -> **Re-apply host security** puts it back or brings a new release's
rules (nothing to do by hand). On an older VM: Install update, then Re-apply
host security once.

What the containers (web, worker, migrate, Postgres, the file store, ClamAV,
cloudflared) may do, in order:

1. answer and continue connections already made;
2. talk to each other on the same portal network (Postgres, the file store,
   ClamAV, web and worker; cloudflared and web on `edge`);
3. ask the VM's own DNS resolvers (port 53), and no other DNS server;
4. **never** reach a private or special address, on any port, 443 included:
   the home network and Proxmox (10.x, 172.16-31.x, 192.168.x), carrier-grade
   NAT (100.64.x), cloud metadata (169.254.x), loopback and reserved ranges;
5. reach public addresses on HTTPS (443) and mail submission and IMAP
   (587, 465, 2525, 993);
6. cloudflared only (the `edge` network): Cloudflare's tunnel port 7844, TCP
   and UDP;
7. nothing else: port 25, plain HTTP, SSH and the rest are dropped and
   logged (at most 6 a minute: `sudo journalctl -k | grep hoa-egress`).
   No IPv6 leaves the portal's networks.

The VM itself (updates, GitHub, the agent) is not affected. On the VM:
`sudo hoa-egress-firewall show` prints the exact rules, `sudo
hoa-egress-firewall status` says `active 2` when they are in place. System
status shows "Outbound firewall" in the health checks and the Host security
card. The settings are in `infra/poc/egress-firewall.conf` (copied to
`/etc/hoa-portal/egress-firewall.conf`); a change for one server only goes in
`/etc/hoa-portal/egress-firewall.local.conf`, with the owner's approval,
because it changes what the portal can reach. Local development (`docker
compose up` on your own computer, with Mailpit) never uses these rules.
