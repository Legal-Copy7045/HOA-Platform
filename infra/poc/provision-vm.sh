#!/usr/bin/env bash
# Hardens the POC VM and installs Docker. Run INSIDE the VM, from its Proxmox
# console, as the "hoa" user with sudo:
#   curl -fsSLO https://raw.githubusercontent.com/Legal-Copy7045/HOA-Platform/claude/ecstatic-bell-cnrpzi/infra/poc/provision-vm.sh
#   less provision-vm.sh          # read it first
#   sudo bash provision-vm.sh
#
# Safe to re-run. What it does:
#   - installs security updates and turns on automatic security updates;
#   - SSH: keys only, no root login, no passwords;
#   - host firewall (ufw): deny all incoming except SSH from ADMIN_CIDR if set;
#     outgoing allowed (Cloudflare tunnel, updates, email). The network-level
#     isolation is the UniFi VLAN; this is a second layer;
#   - installs Docker Engine and the compose plugin from Docker's repository;
#   - checks out the portal code into /opt/hoa-portal.
# It does NOT create .env or any secret; that is a separate, manual step.
set -euo pipefail
trap 'echo "ERROR: ${BASH_SOURCE[0]} stopped at line $LINENO (exit $?). Nothing after that line ran." >&2' ERR

REPO_URL="${REPO_URL:-https://github.com/Legal-Copy7045/HOA-Platform.git}"
BRANCH="${BRANCH:-claude/ecstatic-bell-cnrpzi}"
APP_DIR="${APP_DIR:-/opt/hoa-portal}"
APP_USER="${APP_USER:-hoa}"
# Optional: allow SSH from one admin address/range, e.g. ADMIN_CIDR=192.168.1.20/32
ADMIN_CIDR="${ADMIN_CIDR:-}"

say() { echo "==> $*"; }
[ "$(id -u)" -eq 0 ] || { echo "Run with sudo." >&2; exit 1; }
export DEBIAN_FRONTEND=noninteractive

say "Installing updates and base packages"
apt-get update -q
apt-get -y -q upgrade
apt-get -y -q install ca-certificates curl git ufw unattended-upgrades qemu-guest-agent
systemctl enable --now qemu-guest-agent >/dev/null 2>&1 || true

say "Enabling automatic security updates"
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'CONF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
CONF

say "Hardening SSH"
cat > /etc/ssh/sshd_config.d/10-hoa-hardening.conf <<'CONF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
X11Forwarding no
AllowAgentForwarding no
CONF
# Ubuntu's cloud image ships a drop-in that may re-enable passwords.
rm -f /etc/ssh/sshd_config.d/50-cloud-init.conf
sshd -t && systemctl reload ssh

say "Configuring host firewall"
ufw --force reset >/dev/null
ufw default deny incoming
ufw default allow outgoing
if [ -n "$ADMIN_CIDR" ]; then
  ufw allow from "$ADMIN_CIDR" to any port 22 proto tcp
fi
ufw --force enable

say "Installing Docker Engine"
install -m 0755 -d /etc/apt/keyrings
curl -fsSL --proto '=https' https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
. /etc/os-release
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
  > /etc/apt/sources.list.d/docker.list
apt-get update -q
apt-get -y -q install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
# Docker publishes ports past ufw; our compose files publish none on the POC.
# Membership of the docker group is root-equivalent: only the admin user.
usermod -aG docker "$APP_USER"

say "Checking out the portal code"
install -d -o "$APP_USER" -g "$APP_USER" -m 0750 "$APP_DIR"
if [ -d "$APP_DIR/.git" ]; then
  sudo -u "$APP_USER" git -C "$APP_DIR" fetch --quiet origin "$BRANCH"
  sudo -u "$APP_USER" git -C "$APP_DIR" checkout --quiet "$BRANCH"
  sudo -u "$APP_USER" git -C "$APP_DIR" pull --quiet --ff-only origin "$BRANCH"
else
  sudo -u "$APP_USER" git clone --quiet --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
fi

cat <<MSG

Provisioning complete.
  - Automatic security updates: on
  - SSH: keys only${ADMIN_CIDR:+, allowed from $ADMIN_CIDR}${ADMIN_CIDR:-, no inbound SSH allowed}
  - Docker: $(docker --version)
  - Code: $APP_DIR (branch $BRANCH)

Log out and back in so the docker group applies. The next step (secrets in
.env and starting the portal) comes after Milestone 1 step 5; see
infra/poc/README.md.
MSG
