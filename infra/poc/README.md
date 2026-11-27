# POC virtual machine (Proxmox)

The POC runs on one VM on an isolated VLAN. It is reachable only through the
Cloudflare tunnel `hoa-portal-poc`; nothing on the VM accepts inbound
connections from the internet or other VLANs.

## Steps

1. **VLAN** (UniFi): network `hoa-poc`, its own VLAN ID and subnet, DHCP on,
   "Isolate Network" on, internet access on, mDNS off.
2. **Create the VM** (Proxmox host, web UI -> node -> Shell):

   ```bash
   curl -fsSLO https://raw.githubusercontent.com/Legal-Copy7045/HOA-Platform/main/infra/poc/create-vm.sh
   ```

   ```bash
   less create-vm.sh
   ```

   ```bash
   VLAN_ID=50 BRIDGE=vmbr0 STORAGE=local-lvm bash create-vm.sh
   ```

   Save the console password it prints in your password manager.
3. **Provision the VM** (VM -> Console -> **xterm.js**, which supports paste; log in as `hoa`):

   ```bash
   curl -fsSLO https://raw.githubusercontent.com/Legal-Copy7045/HOA-Platform/main/infra/poc/provision-vm.sh
   ```

   ```bash
   less provision-vm.sh
   ```

   ```bash
   sudo bash provision-vm.sh
   ```

4. **Secrets and start** (VM console, as `hoa`). Get the latest code first:

   ```bash
   cd /opt/hoa-portal && git pull --ff-only
   ```

   Create `.env` with generated secrets. It asks for the tunnel token
   (typing is hidden) and an optional catch-all email:

   ```bash
   bash infra/poc/make-env.sh
   ```

   Build and start everything and install the update agent (images are
   pinned in the compose files and updated by Dependabot pull requests, D57):

   ```bash
   sudo bash infra/poc/start-portal.sh
   ```

   Then open https://portal-dev.oakcreekhoa.com. Cloudflare Access asks for
   your email and sends a one-time code; after that you reach the portal.

   Copy the generated secrets from `.env` into the HOA password manager.

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
4. Admin → Features → Bounced email on, so addresses that bounce are flagged.

Every set-*.sh script edits `.env` through `lib.sh` (`env_update`): only
its own keys change, every other line is kept, the file stays mode 600, and
the `.env.before-*` copy is shredded when the script ends.

## Settings

`create-vm.sh`: `VLAN_ID` (required), `BRIDGE` (vmbr0), `STORAGE` (local-lvm),
`VMID` (next free), `IP_CONFIG` (ip=dhcp, or `ip=10.50.0.10/24,gw=10.50.0.1`),
`SSH_KEY_FILE` (optional), `CORES`, `MEMORY_MB`, `DISK_SIZE`.

`provision-vm.sh`: `ADMIN_CIDR` (allow SSH from one admin address; default
none), `BRANCH`, `REPO_URL`, `APP_DIR`.

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
