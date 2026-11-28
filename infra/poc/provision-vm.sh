#!/usr/bin/env bash
# Hardens the POC VM and installs Docker. infra/poc/install.sh runs it as its
# first step (D94); on its own, run it INSIDE the VM, from its Proxmox
# console, as the "hoa" user with sudo, from a release (never main):
#   curl -fsSLO https://raw.githubusercontent.com/Legal-Copy7045/HOA-Portal/v<VERSION>/infra/poc/provision-vm.sh
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
#   - checks out the portal code into /opt/hoa-portal at the newest release
#     tag (D90: lib.sh checkout_release, the same pick_release_tag as
#     update-portal.sh); BRANCH=<name> follows that branch instead.
# It does NOT create .env or any secret; that is a separate, manual step.
set -euo pipefail
trap 'echo "ERROR: ${BASH_SOURCE[0]} stopped at line $LINENO (exit $?). Nothing after that line ran." >&2' ERR

REPO_URL="${REPO_URL:-https://github.com/Legal-Copy7045/HOA-Portal.git}"
# Empty: the newest release tag on main (D90). A branch name: its tip, on purpose.
BRANCH="${BRANCH:-}"
APP_DIR="${APP_DIR:-/opt/hoa-portal}"
APP_USER="${APP_USER:-hoa}"
# Optional: allow SSH from one admin address/range, e.g. ADMIN_CIDR=192.168.1.20/32
ADMIN_CIDR="${ADMIN_CIDR:-}"

say() { echo "==> $*"; }
[ "$(id -u)" -eq 0 ] || { echo "Run with sudo." >&2; exit 1; }
export DEBIAN_FRONTEND=noninteractive
id -u "$APP_USER" >/dev/null 2>&1 \
  || { echo "There is no user $APP_USER on this server. Create it, or set APP_USER=<your user>." >&2; exit 1; }

say "Installing updates and base packages"
apt-get update -q
apt-get -y -q upgrade
apt-get -y -q install ca-certificates curl git python3 ufw unattended-upgrades qemu-guest-agent
systemctl enable --now qemu-guest-agent >/dev/null 2>&1 || true

say "Enabling network time sync (authenticator-app codes depend on an accurate clock)"
timedatectl set-ntp true

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

CODE="branch $BRANCH"
if [ "${SKIP_CHECKOUT:-0}" = "1" ]; then
  # install.sh made the checkout itself (and never moves an existing one).
  CODE="$(git -c safe.directory="$APP_DIR" -C "$APP_DIR" describe --tags --always 2>/dev/null || echo unknown)"
elif [ -n "$BRANCH" ]; then
  say "Checking out the portal code (branch $BRANCH, on purpose)"
  install -d -o "$APP_USER" -g "$APP_USER" -m 0750 "$APP_DIR"
  if [ -d "$APP_DIR/.git" ]; then
    sudo -u "$APP_USER" git -C "$APP_DIR" fetch --quiet origin "$BRANCH"
    sudo -u "$APP_USER" git -C "$APP_DIR" checkout --quiet "$BRANCH"
    sudo -u "$APP_USER" git -C "$APP_DIR" pull --quiet --ff-only origin "$BRANCH"
  else
    sudo -u "$APP_USER" git clone --quiet --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
  fi
else
  say "Checking out the portal code (the newest release)"
  install -d -o "$APP_USER" -g "$APP_USER" -m 0750 "$APP_DIR"
  [ -d "$APP_DIR/.git" ] || sudo -u "$APP_USER" git clone --quiet --no-checkout "$REPO_URL" "$APP_DIR"
  if [ -f "$APP_DIR/infra/poc/lib.sh" ]; then
    # shellcheck source=infra/poc/lib.sh
    source "$APP_DIR/infra/poc/lib.sh"
  else
    # A fresh clone: lib.sh from the newest release, then its checkout_release.
    NEWEST="$(git -c safe.directory="$APP_DIR" -C "$APP_DIR" tag --list 'v*' \
      | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | sort -V | tail -1)" || NEWEST=""
    [ -n "$NEWEST" ] || { echo "GitHub has no release of the portal yet." >&2; exit 1; }
    # shellcheck source=/dev/null
    source <(git -c safe.directory="$APP_DIR" -C "$APP_DIR" show "$NEWEST:infra/poc/lib.sh")
  fi
  CODE="release $(checkout_release "$APP_DIR" "$APP_USER" | tail -1)"
fi

cat <<MSG

Provisioning complete.
  - Automatic security updates: on
  - SSH: keys only, $(if [ -n "$ADMIN_CIDR" ]; then echo "allowed from $ADMIN_CIDR"; else echo "no inbound SSH allowed"; fi)
  - Docker: $(docker --version)
  - Code: $APP_DIR ($CODE)
MSG
if [ "${HOA_FROM_INSTALL:-0}" != "1" ]; then
  cat <<MSG

Log out and back in so the docker group applies. Next: secrets in .env and
starting the portal (infra/poc/README.md), or run install.sh, which does
every step in one go.
MSG
fi
