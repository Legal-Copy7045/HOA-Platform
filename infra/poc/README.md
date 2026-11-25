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

4. **Secrets and start** - written after Milestone 1 step 5 (the tunnel
   override and trusted-proxy handling). Secrets go only into
   `/opt/hoa-portal/.env` on the VM and the HOA password manager.

## Settings

`create-vm.sh`: `VLAN_ID` (required), `BRIDGE` (vmbr0), `STORAGE` (local-lvm),
`VMID` (next free), `IP_CONFIG` (ip=dhcp, or `ip=10.50.0.10/24,gw=10.50.0.1`),
`SSH_KEY_FILE` (optional), `CORES`, `MEMORY_MB`, `DISK_SIZE`.

`provision-vm.sh`: `ADMIN_CIDR` (allow SSH from one admin address; default
none), `BRANCH`, `REPO_URL`, `APP_DIR`.

Both scripts check before changing anything and are safe to re-run.
