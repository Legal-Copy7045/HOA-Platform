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
