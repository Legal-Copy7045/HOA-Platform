# POC virtual machine (Proxmox)

The POC runs on one VM on an isolated VLAN. It is reachable only through the
Cloudflare tunnel `hoa-portal-poc`; nothing on the VM accepts inbound
connections from the internet or other VLANs.

## Steps

1. **VLAN** (UniFi): network `hoa-poc`, its own VLAN ID and subnet, DHCP on,
   "Isolate Network" on, internet access on, mDNS off.
2. **Create the VM** (Proxmox host, web UI -> node -> Shell):

   ```bash
   curl -fsSLO https://raw.githubusercontent.com/Legal-Copy7045/HOA-Platform/claude/ecstatic-bell-cnrpzi/infra/poc/create-vm.sh
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
   curl -fsSLO https://raw.githubusercontent.com/Legal-Copy7045/HOA-Platform/claude/ecstatic-bell-cnrpzi/infra/poc/provision-vm.sh
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

   Build and start everything, and install the daily cloudflared update:

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

```bash
cd /opt/hoa-portal && git pull --ff-only && sudo bash infra/poc/start-portal.sh
```

Migrations run automatically. If the POC was seeded before Milestone 2, add
the demo data for the new modules once:

```bash
docker compose -f docker-compose.yml -f docker-compose.tunnel.yml exec web python manage.py seed_demo_operations
```
