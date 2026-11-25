#!/usr/bin/env bash
# Creates the POC virtual machine on a Proxmox VE host.
#
# Run as root in the Proxmox web UI: <your node> -> Shell. Example:
#   VLAN_ID=50 BRIDGE=vmbr0 STORAGE=local-lvm bash create-vm.sh
#
# What it does:
#   - checks the bridge, VLAN-awareness, storage and VM ID first, and stops
#     with a clear message if anything is wrong (nothing is created then);
#   - downloads the official Ubuntu 24.04 cloud image and verifies its checksum;
#   - creates a 2 vCPU / 4 GB / 40 GB VM on the given VLAN, protected from
#     accidental deletion, starting at boot;
#   - creates user "hoa" with a random console password (shown ONCE) and,
#     optionally, your SSH public key. SSH password login is disabled later
#     by provision-vm.sh;
#   - starts the VM.
#
# Re-running with an existing VM ID stops without changing anything.
set -euo pipefail
trap 'echo "ERROR: ${BASH_SOURCE[0]} stopped at line $LINENO (exit $?). Nothing after that line ran." >&2' ERR

VLAN_ID="${VLAN_ID:?Set VLAN_ID, e.g. VLAN_ID=50}"
BRIDGE="${BRIDGE:-vmbr0}"
STORAGE="${STORAGE:-local-lvm}"
VMID="${VMID:-$(pvesh get /cluster/nextid)}"
VM_NAME="${VM_NAME:-hoa-portal-poc}"
CORES="${CORES:-2}"
MEMORY_MB="${MEMORY_MB:-4096}"
DISK_SIZE="${DISK_SIZE:-40G}"
# "dhcp" or a CIDR address with gateway, e.g. IP_CONFIG="ip=10.50.0.10/24,gw=10.50.0.1"
IP_CONFIG="${IP_CONFIG:-ip=dhcp}"
# Optional: path to a file holding your SSH public key (one line, ssh-ed25519 ...).
SSH_KEY_FILE="${SSH_KEY_FILE:-}"

IMAGE_BASE="https://cloud-images.ubuntu.com/noble/current"
IMAGE_NAME="noble-server-cloudimg-amd64.img"

fail() { echo "ERROR: $*" >&2; exit 1; }
say() { echo "==> $*"; }

# --- Pre-flight checks (nothing is changed until all pass) --------------------
command -v qm >/dev/null || fail "Run this on a Proxmox VE host (qm not found)."
[ "$(id -u)" -eq 0 ] || fail "Run as root (Proxmox web UI -> node -> Shell)."
[[ "$VLAN_ID" =~ ^[0-9]+$ ]] && [ "$VLAN_ID" -ge 2 ] && [ "$VLAN_ID" -le 4094 ] \
  || fail "VLAN_ID must be a number between 2 and 4094."
[ -d "/sys/class/net/$BRIDGE/bridge" ] || fail "Bridge $BRIDGE not found. See: node -> System -> Network."
if [ "$(cat "/sys/class/net/$BRIDGE/bridge/vlan_filtering")" != "1" ]; then
  fail "Bridge $BRIDGE is not VLAN aware. In node -> System -> Network, edit $BRIDGE, tick 'VLAN aware', then Apply Configuration."
fi
pvesm status --storage "$STORAGE" >/dev/null 2>&1 || fail "Storage $STORAGE not found. See: Datacenter -> Storage."
if qm status "$VMID" >/dev/null 2>&1; then
  fail "VM ID $VMID already exists. Nothing changed. Set VMID=<free id> to use another."
fi
if [ -n "$SSH_KEY_FILE" ]; then
  [ -f "$SSH_KEY_FILE" ] || fail "SSH_KEY_FILE $SSH_KEY_FILE not found."
  grep -qE '^(ssh-ed25519|ssh-rsa|ecdsa-sha2-)' "$SSH_KEY_FILE" || fail "SSH_KEY_FILE does not look like a public key."
fi

# --- Download and verify the image --------------------------------------------
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
say "Downloading Ubuntu 24.04 cloud image"
curl -fsSL --proto '=https' -o "$WORK/$IMAGE_NAME" "$IMAGE_BASE/$IMAGE_NAME"
curl -fsSL --proto '=https' -o "$WORK/SHA256SUMS" "$IMAGE_BASE/SHA256SUMS"
say "Verifying checksum"
(cd "$WORK" && grep " \*\?$IMAGE_NAME\$" SHA256SUMS | sed 's/ \*/  /' | sha256sum -c -) \
  || fail "Checksum mismatch - image not used."

# --- Create the VM ------------------------------------------------------------
# openssl's output is finite, so no pipe is cut short (that would trip pipefail).
CONSOLE_PASSWORD="$(openssl rand -base64 32 | tr -dc 'A-Za-z0-9' | cut -c1-24)"
[ "${#CONSOLE_PASSWORD}" -ge 20 ] || fail "Could not generate a console password."

say "Creating VM $VMID ($VM_NAME) on $BRIDGE, VLAN $VLAN_ID"
qm create "$VMID" \
  --name "$VM_NAME" \
  --ostype l26 \
  --cores "$CORES" --cpu host \
  --memory "$MEMORY_MB" \
  --net0 "virtio,bridge=$BRIDGE,tag=$VLAN_ID" \
  --scsihw virtio-scsi-single \
  --agent enabled=1 \
  --onboot 1 \
  --description "HOA portal POC. Created by infra/poc/create-vm.sh"
qm set "$VMID" --scsi0 "$STORAGE:0,import-from=$WORK/$IMAGE_NAME,discard=on,ssd=1"
qm set "$VMID" --ide2 "$STORAGE:cloudinit" --boot order=scsi0
qm resize "$VMID" scsi0 "$DISK_SIZE"
qm set "$VMID" --ciuser hoa --cipassword "$CONSOLE_PASSWORD" --ipconfig0 "$IP_CONFIG"
if [ -n "$SSH_KEY_FILE" ]; then
  qm set "$VMID" --sshkeys "$SSH_KEY_FILE"
fi
qm set "$VMID" --protection 1
qm start "$VMID"

cat <<MSG

Done. VM $VMID ($VM_NAME) is starting on VLAN $VLAN_ID.

Console login (store it in your password manager NOW; it is not shown again):
  user:     hoa
  password: $CONSOLE_PASSWORD

Next: open the VM's Console in the Proxmox UI, log in, and run provision-vm.sh
(see infra/poc/README.md).
MSG
