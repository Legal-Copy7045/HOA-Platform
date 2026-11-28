#!/usr/bin/env bash
# Installs the whole portal on a fresh Ubuntu server in one command (D94).
#
#   curl -fsSL https://raw.githubusercontent.com/Legal-Copy7045/HOA-Portal/v<VERSION>/infra/poc/install.sh | sudo bash
#
# (always a release tag in that address, never main), or from a checkout:
#
#   sudo bash /opt/hoa-portal/infra/poc/install.sh
#
# It asks for the Cloudflare tunnel token (typing is hidden), and optionally
# the portal's public address and the first board officer's name and email.
# Everything else is generated on the server or set up later in the browser.
# Then, in order, with the same scripts that exist for each step:
#   1. checks the server first and changes nothing if it is not suitable;
#   2. fetches the newest release (lib.sh checkout_release, as update-portal.sh);
#   3. provision-vm.sh: updates, SSH keys only, firewall, Docker;
#   4. make-env.sh: .env with every server-only secret generated (mode 600);
#   5. harden-host.sh: automatic updates, auditd, AIDE, Falco, the outbound
#      firewall for the containers (D101);
#   6. start-portal.sh: builds and starts the portal, the update agent and
#      its server tasks; health-timer.sh: the 10-minute health check;
#   7. checks the containers, migrations, the tunnel and its network, the
#      outbound firewall;
#   8. creates the first officer and sysadmin and shows a one-time sign-in
#      link on the screen (never in the log).
# Safe to run again: finished steps are skipped, existing secrets are never
# regenerated and no data is touched. Log (no secrets): /var/log/hoa-portal-install.log
#
# Options (none needed):
#   --token-file <file|->   read the tunnel token from a file, or stdin with -
#                           (for automation; never put it on the command line)
#   --hostname <name>       the portal's public address, e.g. portal.example.org;
#                           checked from outside at the end (default: no check)
#   --officer-name <name>   the first board officer (also the first sysadmin)
#   --officer-email <addr>  their email (fictional in examples: pat@example.org)
#   --skip-first-officer    do not create the first officer now
#   --admin-cidr <cidr>     keep SSH open from this address, e.g. 192.0.2.10/32
#   --close-ssh             go ahead although you are connected over SSH
#   --new-token             replace the tunnel token in .env (asked, or --token-file)
#   --repair                run every step again (each is safe to re-run)
#   --yes                   do not ask before continuing an earlier install
#   -h, --help              this text
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/Legal-Copy7045/HOA-Portal.git}"
LOG="${HOA_INSTALL_LOG:-/var/log/hoa-portal-install.log}"
STATE_DIR="${HOA_INSTALL_STATE_DIR:-/var/lib/hoa-portal}"
STATE_FILE="$STATE_DIR/install-state"
OS_RELEASE="${HOA_OS_RELEASE:-/etc/os-release}"
MEMINFO="${HOA_MEMINFO:-/proc/meminfo}"
CHECK_WAIT="${HOA_CHECK_WAIT:-240}"
MIN_CPUS=2
MIN_MEM_KB=3670016     # 3.5 GB: a 4 GB server (ClamAV alone needs about 1.5 GB)
MIN_DISK_KB=15728640   # 15 GB free for the images, the database and files
DEFAULT_HOSTNAME="portal-dev.oakcreekhoa.com"
STEPS=(provision env harden start health_timer)

TOKEN_FILE=""
HOSTNAME_ARG=""
OFFICER_NAME=""
OFFICER_EMAIL=""
SKIP_OFFICER=0
ADMIN_CIDR="${ADMIN_CIDR:-}"
CLOSE_SSH=0
NEW_TOKEN=0
REPAIR=0
YES=0
TOKEN=""
CHECK_HOST=""

# --- Output and the log --------------------------------------------------------------
# The screen and the log get every line the steps print, with anything that
# looks like a token, a key or a sign-in link replaced; the sign-in link is
# printed to the terminal only, never through these functions.

mask_line() {
  local line="$1"
  case "$line" in
    *"/invite/"*) printf '%s\n' "[a line with a sign-in link: shown on screen only]"; return 0 ;;
  esac
  if [ -n "$TOKEN" ]; then line="${line//"$TOKEN"/[hidden]}"; fi
  while [[ "$line" =~ eyJ[A-Za-z0-9+/=_-]{20,} ]]; do
    line="${line//"${BASH_REMATCH[0]}"/[hidden]}"
  done
  if [[ "$line" =~ ^(.*(TOKEN|PASSWORD|SECRET|_KEY|_KEY_ID)[A-Z0-9_]*=)(.+)$ ]]; then
    line="${BASH_REMATCH[1]}[hidden]"
  fi
  printf '%s\n' "$line"
}

log_line() {
  printf '%(%Y-%m-%dT%H:%M:%S)T %s\n' -1 "$(mask_line "$1")" >>"$LOG" 2>/dev/null || true
}

# log_tee: what a step prints, masked, on the screen and in the log.
log_tee() {
  local line
  while IFS= read -r line || [ -n "$line" ]; do
    line="$(mask_line "$line")"
    printf '%s\n' "$line"
    printf '%(%Y-%m-%dT%H:%M:%S)T %s\n' -1 "$line" >>"$LOG" 2>/dev/null || true
  done
}

say() { printf '==> %s\n' "$*"; log_line "==> $*"; }
note() { printf '    %s\n' "$*"; log_line "    $*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; log_line "WARNING: $*"; }

# fail <what went wrong> [<the exact next step>...]
fail() {
  local line
  printf '\nSTOPPED: %s\n' "$1" >&2
  log_line "STOPPED: $1"
  shift
  for line in "$@"; do printf '  %s\n' "$line" >&2; log_line "  $line"; done
  printf '\nNothing after this point ran. The log (no secrets) is %s.\n' "$LOG" >&2
  exit 1
}

# run_logged <command...>: run it, showing and logging (masked) what it prints.
run_logged() {
  "$@" 2>&1 | log_tee
  return "${PIPESTATUS[0]}"
}

start_log() {
  mkdir -p "$(dirname "$LOG")"
  ( umask 077; touch "$LOG" )
  chmod 600 "$LOG"
  log_line "=== install.sh started"
}

# --- Questions (from the terminal, also under curl | sudo bash) ------------------------

have_tty() { { : </dev/tty; } 2>/dev/null; }

# ask <variable> <prompt> [hidden]
ask() {
  local value=""
  if [ "${3:-}" = "hidden" ]; then
    IFS= read -r -s -p "$2" value </dev/tty 2>/dev/tty || true
    printf '\n' >/dev/tty
  else
    IFS= read -r -p "$2" value </dev/tty 2>/dev/tty || true
  fi
  printf -v "$1" '%s' "$value"
}

# confirm <question>: yes without a terminal or with --yes (every step is safe to re-run).
confirm() {
  local answer=""
  if [ "$YES" -eq 1 ] || ! have_tty; then return 0; fi
  ask answer "$1 [Y/n] "
  case "${answer,,}" in n|no) return 1 ;; *) return 0 ;; esac
}

usage() { sed -n '2,41p' "${BASH_SOURCE[0]:-/dev/null}" 2>/dev/null | sed 's/^# \{0,1\}//'; }

parse_args() {
  while [ $# -gt 0 ]; do
    case "$1" in
      --token-file) [ $# -ge 2 ] || { echo "--token-file needs a file (or - for stdin)." >&2; exit 2; }
        TOKEN_FILE="$2"; shift ;;
      --hostname) [ $# -ge 2 ] || { echo "--hostname needs a name." >&2; exit 2; }
        HOSTNAME_ARG="${2,,}"; shift ;;
      --officer-name) [ $# -ge 2 ] || { echo "--officer-name needs a name." >&2; exit 2; }
        OFFICER_NAME="$2"; shift ;;
      --officer-email) [ $# -ge 2 ] || { echo "--officer-email needs an address." >&2; exit 2; }
        OFFICER_EMAIL="$2"; shift ;;
      --skip-first-officer) SKIP_OFFICER=1 ;;
      --admin-cidr) [ $# -ge 2 ] || { echo "--admin-cidr needs an address, e.g. 192.0.2.10/32." >&2; exit 2; }
        ADMIN_CIDR="$2"; shift ;;
      --close-ssh) CLOSE_SSH=1 ;;
      --new-token) NEW_TOKEN=1 ;;
      --repair) REPAIR=1 ;;
      --yes|-y) YES=1 ;;
      -h|--help) usage; exit 0 ;;
      *) echo "Unknown option: $1 (try --help)" >&2; exit 2 ;;
    esac
    shift
  done
  if [ -n "$TOKEN_FILE" ] && [ "$TOKEN_FILE" != "-" ]; then
    case "$TOKEN_FILE" in /*) ;; *) TOKEN_FILE="$PWD/$TOKEN_FILE" ;; esac
  fi
  if [ -n "$ADMIN_CIDR" ] && ! [[ "$ADMIN_CIDR" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}/[0-9]{1,2}$ ]]; then
    echo "--admin-cidr takes an IPv4 address with its prefix, e.g. 192.0.2.10/32." >&2; exit 2
  fi
}

# --- Preflight: nothing is changed until these pass ------------------------------------

# check_os <os-release file>: Ubuntu 22.04 or newer.
check_os() {
  local id="" version="" pretty="" line major minor
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      ID=*) id="${line#ID=}" ;;
      VERSION_ID=*) version="${line#VERSION_ID=}" ;;
      PRETTY_NAME=*) pretty="${line#PRETTY_NAME=}" ;;
    esac
  done <"$1"
  id="${id//\"/}"; version="${version//\"/}"; pretty="${pretty//\"/}"
  if [ "$id" != "ubuntu" ] || ! [[ "$version" =~ ^([0-9]+)\.([0-9]+)$ ]]; then
    echo "This installer needs Ubuntu 22.04 or newer (24.04 LTS recommended); this server runs ${pretty:-an unknown system}." >&2
    return 1
  fi
  major=$((10#${BASH_REMATCH[1]})); minor=$((10#${BASH_REMATCH[2]}))
  if [ "$major" -lt 22 ] || { [ "$major" -eq 22 ] && [ "$minor" -lt 4 ]; }; then
    echo "This installer needs Ubuntu 22.04 or newer (24.04 LTS recommended); this server runs ${pretty:-Ubuntu $version}." >&2
    return 1
  fi
  printf 'Ubuntu %s\n' "$version"
}

# check_resources <cpus> <memory kB> <free disk kB>
check_resources() {
  local cpus="$1" mem="$2" disk="$3" problems=0
  if [ "${cpus:-0}" -lt "$MIN_CPUS" ]; then
    echo "The server has ${cpus:-0} CPU; the portal needs at least $MIN_CPUS." >&2; problems=1
  fi
  if [ "${mem:-0}" -lt "$MIN_MEM_KB" ]; then
    echo "The server has $(( ${mem:-0} / 1024 )) MB of memory; the portal needs a 4 GB server (at least $(( MIN_MEM_KB / 1024 )) MB)." >&2
    problems=1
  fi
  if [ "${disk:-0}" -lt "$MIN_DISK_KB" ]; then
    echo "The disk has $(( ${disk:-0} / 1048576 )) GB free; the portal needs at least $(( MIN_DISK_KB / 1048576 )) GB." >&2
    problems=1
  fi
  return "$problems"
}

# can_reach <host> <port>: an outgoing TCP connection works within 5 seconds.
can_reach() {
  # shellcheck disable=SC2016  # expanded by the inner bash, from its own arguments
  timeout 5 bash -c 'exec 3<>"/dev/tcp/$1/$2"' _ "$1" "$2" >/dev/null 2>&1
}

# ssh_peers: the addresses connected to this server's SSH port, one per line.
ssh_peers() {
  local peer
  ss -Htn state established '( sport = :22 )' 2>/dev/null | while read -r _ _ _ peer _; do
    peer="${peer%:*}"; peer="${peer#[}"; peer="${peer%]}"
    if [[ "$peer" =~ ^[0-9a-fA-F.:]+$ ]]; then printf '%s\n' "$peer"; fi
  done
}

memory_kb() { awk '$1 == "MemTotal:" { print $2 + 0 }' "$MEMINFO" 2>/dev/null || echo 0; }

free_disk_kb() {
  local dir="$1"
  while [ ! -d "$dir" ]; do dir="$(dirname "$dir")"; done
  df -Pk "$dir" 2>/dev/null | awk 'NR == 2 { print $4 + 0 }'
}

preflight() {
  local os problems
  say "Checking the server (nothing is changed until these pass)"
  [ "$(id -u)" -eq 0 ] || fail "This needs administrator rights." "Run it again with sudo in front."
  [ -r "$OS_RELEASE" ] || fail "Cannot tell which system this server runs." "Use an Ubuntu 24.04 LTS server."
  os="$(check_os "$OS_RELEASE" 2>&1)" || fail "$os" "Create the server from Ubuntu 24.04 LTS (infra/poc/create-vm.sh on Proxmox does that)."
  note "System: $os"
  if [ "$INSTALL_STATE" = "fresh" ]; then
    problems="$(check_resources "$(nproc 2>/dev/null || echo 1)" "$(memory_kb)" "$(free_disk_kb "$APP_DIR")" 2>&1)" \
      || fail "The server is too small: $problems" "Give it at least 2 CPUs, 4 GB of memory and 40 GB of disk, then run this again."
  fi
  note "CPUs $(nproc 2>/dev/null || echo ?), memory $(( $(memory_kb) / 1024 )) MB, free disk $(( $(free_disk_kb "$APP_DIR") / 1048576 )) GB"
  can_reach github.com 443 \
    || fail "The server cannot reach GitHub (github.com, port 443)." "Check its internet access and DNS, then run this again."
  if [ "$INSTALL_STATE" = "fresh" ]; then
    can_reach download.docker.com 443 \
      || fail "The server cannot reach Docker's packages (download.docker.com, port 443)." "Check its internet access, then run this again."
  fi
  can_reach region1.v2.argotunnel.com 7844 \
    || warn "Outgoing port 7844 to Cloudflare looks closed; the tunnel needs it (allow outgoing TCP and UDP 7844 in the network's firewall)."
  note "Internet access: GitHub and Docker reachable"
  if ! step_done provision && [ -z "$ADMIN_CIDR" ] && [ "$CLOSE_SSH" -eq 0 ]; then
    local peers
    peers="$(ssh_peers | sort -u | head -3 | tr '\n' ' ')"
    if [ -n "$peers" ]; then
      fail "You are connected over SSH (from ${peers% }), and this install closes inbound SSH: the portal needs none, and you would use the server's console instead." \
        "Keep SSH from your address (keys only; passwords stop working):  add --admin-cidr ${peers%% *}/32" \
        "Or close it and use the console from now on:                     add --close-ssh" \
        "With curl, options go after 'bash -s --', e.g.  curl -fsSL <address> | sudo bash -s -- --close-ssh"
    fi
  fi
}

# --- Where things are, and what is already done ----------------------------------------

state_has() { [ -f "$STATE_FILE" ] && grep -qxF -- "$1" "$STATE_FILE"; }

mark_done() {
  mkdir -p "$STATE_DIR"; chmod 700 "$STATE_DIR"
  state_has "$1" || printf '%s\n' "$1" >>"$STATE_FILE"
  chmod 600 "$STATE_FILE"
}

unit_enabled() { systemctl is-enabled --quiet "$1" 2>/dev/null; }

# step_done <step>: recorded by an earlier run, or visibly done by the
# separate scripts before install.sh existed.
step_done() {
  state_has "$1" && return 0
  case "$1" in
    provision) [ -f /etc/ssh/sshd_config.d/10-hoa-hardening.conf ] && command -v docker >/dev/null 2>&1 ;;
    env) [ -f "$APP_DIR/.env" ] ;;
    harden) unit_enabled hoa-integrity-check.timer && unit_enabled hoa-egress-firewall.timer ;;
    start) unit_enabled hoa-portal-agent.timer ;;
    health_timer) unit_enabled hoa-health.timer ;;
    *) return 1 ;;
  esac
}

detect_state() {
  local step done=0
  if [ ! -d "$APP_DIR/.git" ] && [ ! -f "$APP_DIR/.env" ]; then INSTALL_STATE=fresh; return; fi
  for step in "${STEPS[@]}"; do step_done "$step" && done=$((done + 1)); done
  if [ "$done" -eq "${#STEPS[@]}" ]; then INSTALL_STATE=installed
  elif [ -f "$APP_DIR/.env" ] || [ -f "$STATE_FILE" ]; then INSTALL_STATE=partial
  else INSTALL_STATE=fresh
  fi
}

# --- Fetching the code (when run through curl, outside a checkout) ---------------------

# app_user: who owns the checkout. Existing checkout: its owner; else hoa
# (create-vm.sh makes it), else the person who ran sudo, else a new "hoa".
app_user() {
  if [ -n "${APP_USER:-}" ]; then printf '%s\n' "$APP_USER"
  elif [ -d "$APP_DIR" ]; then stat -c %U "$APP_DIR"
  elif id -u hoa >/dev/null 2>&1; then echo hoa
  elif [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != "root" ]; then printf '%s\n' "$SUDO_USER"
  else echo hoa
  fi
}

bootstrap() {
  local user newest script
  user="$(app_user)"
  if ! id -u "$user" >/dev/null 2>&1; then
    say "Creating the user $user, who owns the portal's code (no password; sign in through the console as before)"
    useradd --create-home --shell /bin/bash "$user"
    passwd -l "$user" >/dev/null
  fi
  if ! command -v git >/dev/null 2>&1; then
    say "Installing git"
    run_logged env DEBIAN_FRONTEND=noninteractive apt-get -y -q install git </dev/null \
      || fail "Could not install git." "Run: sudo apt-get update && sudo apt-get install -y git, then this again."
  fi
  if [ ! -d "$APP_DIR/.git" ]; then
    say "Fetching the portal's code into $APP_DIR"
    install -d -o "$user" -g "$(id -gn "$user")" -m 0750 "$APP_DIR"
    run_logged release_as "$user" git clone --quiet --no-checkout "$REPO_URL" "$APP_DIR" </dev/null \
      || fail "Could not fetch the code from $REPO_URL." "Check the server's internet access, then run this again."
  fi
  if [ ! -f "$APP_DIR/infra/poc/lib.sh" ]; then
    # Nothing checked out yet: the newest release's lib.sh does the checkout.
    # Only a release tag on main counts (D90, D94; review EXP-005, 0.44.0), as
    # in lib.sh's checkout_release: a v-tag on another branch is never run.
    run_logged release_as "$user" git -C "$APP_DIR" fetch --quiet --tags origin main </dev/null \
      || fail "Could not fetch the code from $REPO_URL." "Check the server's internet access, then run this again."
    newest="$(git -c safe.directory="$APP_DIR" -C "$APP_DIR" tag --merged origin/main --list 'v*' \
      | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | sort -V | tail -1)" || newest=""
    [ -n "$newest" ] || fail "GitHub has no release of the portal yet." "Wait for the first release (a v<VERSION> tag), then run this again."
    # shellcheck source=/dev/null
    source <(git -c safe.directory="$APP_DIR" -C "$APP_DIR" show "$newest:infra/poc/lib.sh")
    run_logged checkout_release "$APP_DIR" "$user" </dev/null \
      || fail "Could not check out the newest release." "Run this again; if it repeats, see the log."
  fi
  script="$APP_DIR/infra/poc/install.sh"
  [ -f "$script" ] || fail "The code in $APP_DIR is older than install.sh (0.44.0)." \
    "Bring it to the newest release first: sudo bash $APP_DIR/infra/poc/update-portal.sh, then run this again."
  say "Continuing with $(cat "$APP_DIR/VERSION" 2>/dev/null || echo the release)'s own install.sh"
  exec bash "$script" "$@"
}

release_as() {
  local user="$1"
  shift
  if [ "$user" = "root" ]; then "$@"; else sudo -u "$user" -H "$@"; fi
}

# --- What the person is asked ------------------------------------------------------------

read_token() {
  if [ "$TOKEN_FILE" = "-" ]; then
    IFS= read -r TOKEN || true
  elif [ -n "$TOKEN_FILE" ]; then
    [ -r "$TOKEN_FILE" ] || fail "Cannot read the token file $TOKEN_FILE." "Check the path (and that it is readable by root)."
    IFS= read -r TOKEN <"$TOKEN_FILE" || true
  elif have_tty; then
    cat >/dev/tty <<'TEXT'

The Cloudflare tunnel token: in the Cloudflare dashboard, Zero Trust ->
Networks -> Tunnels -> your tunnel -> Configure (or Create a tunnel ->
Cloudflared). Copy the install command it shows; pasting the whole command is
fine, only the token (the long text starting eyJ) is kept.

TEXT
    ask TOKEN "Paste it here (typing is hidden), then Enter: " hidden
  else
    fail "No tunnel token: there is no terminal to ask on." \
      "Run it from a terminal, or give the token in a file: --token-file /root/tunnel-token (or --token-file - with the token on stdin)."
  fi
  TOKEN="${TOKEN//[[:space:]]/}"
  if [[ "$TOKEN" =~ (eyJ[A-Za-z0-9+/=_-]{50,}) ]]; then TOKEN="${BASH_REMATCH[1]}"; fi
  local why
  why="$(tunnel_token_ok "$TOKEN" 2>&1)" \
    || fail "$why" "Copy the token again from the Cloudflare dashboard (Zero Trust -> Networks -> Tunnels), then run this again."
}

read_hostname() {
  if [ -n "$HOSTNAME_ARG" ]; then
    hostname_ok "$HOSTNAME_ARG" || fail "Not a web address: $HOSTNAME_ARG." "Give just the name, e.g. --hostname portal.example.org."
    CHECK_HOST="$HOSTNAME_ARG"
    return
  fi
  if [ -f "$APP_DIR/.env" ] || ! have_tty; then return 0; fi
  local answer
  ask answer "The portal's public address, as on the tunnel's Public Hostname (Enter: $DEFAULT_HOSTNAME, no outside check): "
  answer="${answer,,}"; answer="${answer#https://}"; answer="${answer%%/*}"
  if [ -n "$answer" ]; then
    hostname_ok "$answer" || fail "Not a web address: $answer." "Run this again and type just the name, e.g. portal.example.org."
    HOSTNAME_ARG="$answer"; CHECK_HOST="$answer"
  fi
}

read_officer() {
  [ "$SKIP_OFFICER" -eq 0 ] || return 0
  if [ -z "$OFFICER_NAME" ] && have_tty; then
    printf '\nThe first board officer, who is also the first sysadmin (you, usually). Enter to do it later.\n' >/dev/tty
    ask OFFICER_NAME "Your name: "
    [ -z "$OFFICER_NAME" ] || ask OFFICER_EMAIL "Your email: "
  fi
  [ -n "$OFFICER_NAME" ] || return 0
  if [ "${#OFFICER_NAME}" -gt 100 ] || [[ "$OFFICER_NAME" =~ [[:cntrl:]] ]]; then
    fail "That name is too long or has control characters." "Run this again with a plain name."
  fi
  [[ "$OFFICER_EMAIL" =~ ^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$ ]] \
    || fail "That is not an email address: the first officer's sign-in needs one." "Run this again with --officer-email you@example.org (your real address)."
}

# --- The steps ----------------------------------------------------------------------------

step_provision() {
  say "Step 1 of 5: updates, SSH, firewall and Docker (provision-vm.sh, several minutes)"
  run_logged env HOA_FROM_INSTALL=1 SKIP_CHECKOUT=1 APP_DIR="$APP_DIR" APP_USER="$USER_NAME" \
      ADMIN_CIDR="$ADMIN_CIDR" REPO_URL="$REPO_URL" bash "$POC/provision-vm.sh" </dev/null \
    || fail "Setting up the server's packages, SSH, firewall or Docker failed (see the lines above)." \
      "Run this again: finished steps are skipped. If it repeats, check the server's internet access."
}

# fill_missing_secrets: an .env made by an older make-env.sh gets any server
# secret make-env.sh now generates (the keys whose values differ between two
# renders), added once; nothing already in .env changes.
fill_missing_secrets() {
  local tmp line key added=() dummy
  tmp="$(mktemp -d)"
  chmod 700 "$tmp"
  printf -v dummy 'A%.0s' {1..120}
  printf '%s\n' "$dummy" | bash "$POC/make-env.sh" --token-file - --no-questions --output "$tmp/a" >/dev/null
  printf '%s\n' "$dummy" | bash "$POC/make-env.sh" --token-file - --no-questions --output "$tmp/b" >/dev/null
  while IFS= read -r line || [ -n "$line" ]; do
    [[ "$line" =~ ^([A-Z][A-Z0-9_]*)= ]] || continue
    key="${BASH_REMATCH[1]}"
    [ "${line#*=}" != "$(env_get "$tmp/b" "$key")" ] || continue
    env_get "$APP_DIR/.env" "$key" >/dev/null && continue
    env_add_missing "$APP_DIR/.env" "$key" "${line#*=}"
    added+=("$key")
  done <"$tmp/a"
  shred -u "$tmp/a" "$tmp/b" 2>/dev/null || rm -f "$tmp/a" "$tmp/b"
  rmdir "$tmp"
  if [ "${#added[@]}" -gt 0 ]; then note "Generated the secrets this .env did not have yet: ${added[*]}"; fi
  return 0
}

step_env() {
  local env="$APP_DIR/.env" current
  if [ -f "$env" ]; then
    say "Step 2 of 5: settings and secrets: .env exists, kept as it is (secrets are never regenerated)"
    fill_missing_secrets
    current="$(env_get "$env" TUNNEL_TOKEN || true)"
    if [ -n "$TOKEN" ] && { [ "$NEW_TOKEN" -eq 1 ] || [ -z "$current" ]; }; then
      env_update "$env" TUNNEL_TOKEN "$TOKEN"
      note "Saved the new tunnel token in .env"
    fi
  else
    say "Step 2 of 5: settings and secrets (make-env.sh: every server secret generated here)"
    printf '%s\n' "$TOKEN" | run_logged bash "$POC/make-env.sh" --token-file - --no-questions \
        --hostname "${HOSTNAME_ARG:-$DEFAULT_HOSTNAME}" \
      || fail "Could not create .env (see the lines above)." "Run this again."
  fi
  ensure_compose_file "$env" | log_tee
  chown "$USER_NAME:$(id -gn "$USER_NAME")" "$env" 2>/dev/null || true
  chmod 600 "$env"
}

step_harden() {
  say "Step 3 of 5: host security (harden-host.sh: automatic updates, auditd, AIDE, Falco, outbound firewall; a few minutes)"
  run_logged bash "$POC/harden-host.sh" </dev/null \
    || fail "Host security set-up failed (see the lines above)." \
      "Run this again: finished steps are skipped. A Falco key message names its own fix."
}

step_start() {
  say "Step 4 of 5: building and starting the portal (start-portal.sh: the first build takes 5-10 minutes)"
  run_logged bash "$POC/start-portal.sh" </dev/null \
    || fail "Starting the portal failed (see the lines above)." "Run this again: finished steps are skipped."
}

step_health_timer() {
  say "Step 5 of 5: the 10-minute health check (health-timer.sh)"
  run_logged bash "$POC/health-timer.sh" </dev/null \
    || fail "Installing the health check failed (see the lines above)." "Run this again."
}

# --- Checks at the end ----------------------------------------------------------------------

# services_state: OK, WAIT <what> or FAIL <what>, from `compose ps`.
services_state() {
  local out svc state health code seen="" waiting="" failed="" notes=""
  out="$("${COMPOSE[@]}" ps -a --format '{{.Service}}|{{.State}}|{{.Health}}|{{.ExitCode}}' </dev/null 2>/dev/null)" || out=""
  while IFS='|' read -r svc state health code; do
    [ -n "$svc" ] || continue
    seen="$seen $svc "
    case "$svc" in
      migrate|create-bucket)
        if [ "$state" = "exited" ] && [ "$code" = "0" ]; then :
        elif [ "$state" = "exited" ]; then failed="$failed $svc"
        else waiting="$waiting $svc"; fi ;;
      *)
        if [ "$state" != "running" ]; then failed="$failed $svc"
        elif [ "$health" = "starting" ] && [ "$svc" = "clamav" ]; then notes="clamav"
        elif [ "$health" = "starting" ]; then waiting="$waiting $svc"
        elif [ -n "$health" ] && [ "$health" != "healthy" ]; then failed="$failed $svc"; fi ;;
    esac
  done <<<"$out"
  for svc in postgres migrate web worker clamav cloudflared; do
    [[ "$seen" == *" $svc "* ]] || failed="$failed $svc(missing)"
  done
  if [ -n "$failed" ]; then echo "FAIL${failed}"
  elif [ -n "$waiting" ]; then echo "WAIT${waiting}"
  else echo "OK ${notes}"; fi
}

tunnel_state() {
  local logs
  logs="$("${COMPOSE[@]}" logs --no-log-prefix --tail 300 cloudflared </dev/null 2>&1)" || logs=""
  if grep -q "Registered tunnel connection" <<<"$logs"; then echo OK
  elif grep -qiE "unauthorized|invalid (tunnel )?(token|secret)|tunnel credentials" <<<"$logs"; then echo REFUSED
  else echo WAIT; fi
}

run_checks() {
  local deadline problems=() services tunnel extra code egress egress_nets
  say "Checking the portal"
  deadline=$(( $(date +%s) + CHECK_WAIT ))
  while :; do
    services="$(services_state)"
    tunnel="$(tunnel_state)"
    if [[ "$services" == OK* && "$tunnel" != WAIT ]] || [[ "$services" == FAIL* ]] || [ "$(date +%s)" -ge "$deadline" ]; then break; fi
    sleep 5
  done
  case "$services" in
    OK*) note "Containers: running and healthy"
      [[ "$services" == *clamav* ]] && note "(ClamAV is still loading its virus signatures, a few minutes on first start; uploads wait for it)" ;;
    WAIT*) problems+=("These containers are not ready yet:${services#WAIT}. Look again in a few minutes: sudo docker compose -f docker-compose.yml -f docker-compose.tunnel.yml ps") ;;
    *) problems+=("These containers are not running or not healthy:${services#FAIL}. See why: cd $APP_DIR && sudo docker compose logs --tail 50 <name>") ;;
  esac
  if "${COMPOSE[@]}" exec -T web python manage.py migrate --check </dev/null >/dev/null 2>&1; then
    note "Database: every migration applied"
  else
    problems+=("The database is not fully migrated. See why: cd $APP_DIR && sudo docker compose logs migrate")
  fi
  if [ -z "$(cloudflared_networks)" ]; then
    problems+=("cloudflared (the tunnel) is not running. See why: cd $APP_DIR && sudo docker compose logs cloudflared")
  else
    extra="$(cloudflared_extra_networks | tr '\n' ' ')"
    if [ -n "${extra// /}" ]; then
      problems+=("cloudflared is also on the network(s) ${extra% }; it belongs on edge only. Run: sudo bash $POC/start-portal.sh")
    else
      note "Tunnel network: cloudflared on the edge network only"
    fi
  fi
  # The outbound firewall (D101): applied by harden-host.sh, on the networks
  # start-portal.sh just made (re-applied here in case the timer has not run).
  egress_firewall_refresh
  read -r egress egress_nets <<<"$(egress_firewall_state)"
  if [ "$egress" = active ]; then
    note "Outbound firewall: on for $egress_nets portal networks (public HTTPS, mail and IMAP ports only; never the LAN)"
  else
    problems+=("The outbound firewall for the containers is not on (${egress:-unknown}). Run: sudo bash $POC/harden-host.sh, then: sudo hoa-egress-firewall status")
  fi
  case "$tunnel" in
    OK) note "Tunnel: connected to Cloudflare" ;;
    REFUSED) problems+=("Cloudflare refused the tunnel token. Copy it again (Zero Trust -> Networks -> Tunnels -> your tunnel -> Configure) and run: sudo bash $POC/install.sh --new-token") ;;
    *) problems+=("The tunnel has not connected to Cloudflare yet. Check outgoing port 7844 is open, then: cd $APP_DIR && sudo docker compose logs --tail 30 cloudflared") ;;
  esac
  if [ -n "$CHECK_HOST" ]; then
    code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 20 "https://$CHECK_HOST/accounts/login/" 2>/dev/null)" || code="${code:-000}"
    code="${code//[^0-9]/}"
    if [[ "$code" =~ ^[234][0-9][0-9]$ ]] && [ "$code" != "404" ]; then
      note "From outside: https://$CHECK_HOST answers (HTTP $code; 302 or 403 is Cloudflare Access asking who you are)"
    elif [[ "$code" =~ ^5 ]]; then
      problems+=("https://$CHECK_HOST answers $code: Cloudflare cannot reach the portal. On the tunnel's Public Hostname the service must be http://web:8000.")
    else
      problems+=("https://$CHECK_HOST does not answer (${code:-no reply}). Check the tunnel's Public Hostname in Cloudflare is exactly $CHECK_HOST.")
    fi
  fi
  if [ "${#problems[@]}" -gt 0 ]; then
    fail "The portal is installed, but these checks did not pass:" "${problems[@]}" \
      "Then run this again (sudo bash $POC/install.sh): it skips what is done and checks again."
  fi
}

# --- The first officer and sysadmin ---------------------------------------------------------

# manage <args...>: a management command in the web container; prints its output.
manage() {
  "${COMPOSE[@]}" exec -T web python manage.py "$@" </dev/null 2>&1 | { grep -v '"level"' || true; }
}

first_officer() {
  local status out email=""
  status="$(manage bootstrap_first_officer --status | tail -1 | tr -cd '[:lower:]')" || status=""
  case "$status" in
    none)
      if [ -z "$OFFICER_NAME" ] && [ "$YES" -eq 0 ]; then read_officer; fi
      if [ -z "$OFFICER_NAME" ]; then
        SIGN_IN="Create the first officer (you; also the first sysadmin) when ready:
    cd $APP_DIR && sudo docker compose exec web python manage.py bootstrap_first_officer --name \"Your Name\" --email you@example.org --also-sysadmin
  It prints a one-time sign-in link."
        return 0
      fi
      say "Creating the first board officer and sysadmin"
      out="$(manage bootstrap_first_officer --name "$OFFICER_NAME" --email "$OFFICER_EMAIL" --also-sysadmin)" \
        || fail "Creating the first officer failed: $(printf '%s' "$out" | grep -v '/invite/' | tail -1)" "Run this again."
      log_line "First officer created; the sign-in link was shown on screen only."
      SIGN_IN_LINK="$out" ;;
    invited)
      if have_tty && [ "$YES" -eq 0 ]; then
        ask email "Nobody has signed in yet. For a new sign-in link, type the first officer's email (Enter to skip): "
      fi
      if [ -n "$email" ]; then
        out="$(manage print_invitation_link --email "$email")" \
          || fail "No new link: $(printf '%s' "$out" | tail -1)" "Check the address and run this again."
        log_line "A new sign-in link was shown on screen only."
        SIGN_IN_LINK="$out"
      else
        SIGN_IN="Nobody has signed in yet. A new one-time link for the first officer:
    cd $APP_DIR && sudo docker compose exec web python manage.py print_invitation_link --email you@example.org"
      fi ;;
    done) SIGN_IN="Sign in at $(env_get "$APP_DIR/.env" PORTAL_BASE_URL || echo "the portal's address")." ;;
    *) warn "Could not ask the portal whether the first officer exists; see: cd $APP_DIR && sudo docker compose logs --tail 30 web" ;;
  esac
}

# --- Main ---------------------------------------------------------------------------------------

main() {
  parse_args "$@"
  [ "$(id -u)" -eq 0 ] || { echo "Run it with sudo: curl ... | sudo bash   (or sudo bash install.sh)" >&2; exit 1; }
  start_log
  # Any unexpected failure, also inside a function, ends with a plain message
  # (only in this shell: a failing check inside $(...) is handled where it is).
  MAIN_PID="$BASHPID"
  set -E
  trap '[ "$BASHPID" != "$MAIN_PID" ] || fail "install.sh stopped unexpectedly at line $LINENO." "Run it again: finished steps are skipped and nothing is regenerated."' ERR

  local script_path="" in_checkout=0
  if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
    script_path="$(readlink -f "${BASH_SOURCE[0]}")"
    POC="$(dirname "$script_path")"
    if [ -f "$POC/lib.sh" ] && [ -f "$POC/../../docker-compose.tunnel.yml" ] && [ -d "$POC/../../.git" ]; then
      in_checkout=1
    fi
  fi
  if [ "$in_checkout" -eq 1 ]; then
    APP_DIR="$(cd "$POC/../.." && pwd)"
  else
    APP_DIR="${APP_DIR:-/opt/hoa-portal}"
  fi

  detect_state
  if [ "$in_checkout" -eq 0 ]; then
    if [ -d "$APP_DIR/.git" ] && [ -f "$APP_DIR/infra/poc/install.sh" ]; then
      exec bash "$APP_DIR/infra/poc/install.sh" "$@"
    fi
    # The checks first (they need nothing installed), then the code.
    preflight
    bootstrap "$@"
  fi

  # shellcheck source=infra/poc/lib.sh
  source "$POC/lib.sh"
  COMPOSE=(docker compose --project-directory "$APP_DIR" -f docker-compose.yml -f docker-compose.tunnel.yml)
  USER_NAME="$(stat -c %U "$APP_DIR")"
  cd "$APP_DIR"

  preflight
  case "$INSTALL_STATE" in
    installed)
      say "The portal is already installed here. Running again changes nothing that is set up: secrets are kept and no data is touched; it checks everything."
      confirm "Continue?" || exit 0 ;;
    partial)
      say "An earlier install did not finish. Finished steps are skipped; existing secrets and data are kept."
      confirm "Continue where it stopped?" || exit 0 ;;
  esac

  if [ ! -f "$APP_DIR/.env" ] || [ "$NEW_TOKEN" -eq 1 ] || [ -z "$(env_get "$APP_DIR/.env" TUNNEL_TOKEN 2>/dev/null || true)" ]; then
    read_token
  fi
  read_hostname
  if [ ! -f "$APP_DIR/.env" ]; then read_officer; fi
  log_line "Answers: token given=$([ -n "$TOKEN" ] && echo yes || echo no), address=${CHECK_HOST:-not checked}, first officer=$([ -n "$OFFICER_NAME" ] && echo given || echo later)"
  if [ "$INSTALL_STATE" = "fresh" ]; then
    say "That is all the typing. The rest runs on its own (about 15-25 minutes)."
  fi

  local step
  for step in "${STEPS[@]}"; do
    if [ "$REPAIR" -eq 0 ] && step_done "$step" && [ "$step" != "env" ] \
       && ! { [ "$step" = "start" ] && [ "$NEW_TOKEN" -eq 1 ]; }; then
      note "Already done: $step"
      continue
    fi
    "step_$step"
    mark_done "$step"
  done
  TOKEN=""

  run_checks
  SIGN_IN=""; SIGN_IN_LINK=""
  first_officer
  mark_done complete

  local address
  address="$(env_get "$APP_DIR/.env" PORTAL_BASE_URL 2>/dev/null || echo "https://$DEFAULT_HOSTNAME")"
  say "Done: the portal $(cat "$APP_DIR/VERSION" 2>/dev/null || true) is installed and healthy at $address"
  if [ -n "$SIGN_IN_LINK" ]; then
    # On the screen only: never through log_tee, never in the log.
    printf '\n%s\n' "$SIGN_IN_LINK"
    printf '\nOpen the link above in your browser (works once; do not share it). Choose a password,\nthen add a passkey or an authenticator app and save the recovery codes.\n'
  elif [ -n "$SIGN_IN" ]; then
    printf '\n  %s\n' "$SIGN_IN"
  fi
  cat <<TEXT

Everything else is done in the browser, not here: Settings -> Community setup
walks through email (SMTP2Go), payments, the governing rules and the rest, and
System status installs updates and runs backups and drills.

Optional, on this server: off-site backups to a locked bucket that a
compromised portal cannot delete (kept separate from the portal on purpose):
  sudo bash $POC/setup-offsite-backups.sh

Keep a copy of $APP_DIR/.env in the HOA password manager.
The log of this install (no secrets): $LOG
TEXT
  log_line "=== install.sh finished"
}

# Run main only when executed (bash install.sh, or curl | bash), not when a
# test sources this file for its functions.
if [ -z "${BASH_SOURCE[0]:-}" ] || [ "${BASH_SOURCE[0]}" = "$0" ]; then
  main "$@"
fi
