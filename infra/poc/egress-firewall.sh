#!/usr/bin/env bash
# The outbound firewall for the portal's containers (D101). harden-host.sh
# installs this file as /usr/local/sbin/hoa-egress-firewall (root's copy: the
# systemd unit never runs code from the checkout) and its settings as
# /etc/hoa-portal/egress-firewall.conf (from infra/poc/egress-firewall.conf).
#
#   sudo hoa-egress-firewall apply    # (re)apply now; idempotent (--quiet: silent when current)
#   sudo hoa-egress-firewall status   # one line "<state> <networks>"; exit 0 only when active
#   sudo hoa-egress-firewall show     # the rules apply would load (iptables-restore format)
#   sudo hoa-egress-firewall remove   # troubleshooting only: take the rules out
#
# Docker's own forwarding rules bypass ufw, so the rules live in Docker's
# DOCKER-USER chain, which Docker evaluates first and never rewrites: one jump
# there to our chain HOA-EGRESS, which is rebuilt atomically on each apply
# (iptables-restore --noflush replaces only our chains), so re-applying never
# duplicates a rule. hoa-egress-firewall.service runs apply after Docker
# starts (and so after every reboot); hoa-egress-firewall.timer runs it every
# minute so a recreated compose network (a new bridge) is covered within a
# minute. Only traffic coming FROM the compose project's networks (found by
# Docker's compose label, never by subnet) is filtered: replies, traffic
# within one portal network, DNS to the host's resolvers, and the configured
# ports to public addresses pass; private and special ranges are dropped on
# every port; everything else is logged (rate-limited) and dropped. IPv6
# from those networks never leaves them. The VM's own outgoing traffic (the
# update agent, apt, GitHub) is not affected.
set -euo pipefail

CONF="${HOA_EGRESS_CONF:-/etc/hoa-portal/egress-firewall.conf}"
LOCAL_CONF="${HOA_EGRESS_LOCAL_CONF:-/etc/hoa-portal/egress-firewall.local.conf}"
LOCK="${HOA_EGRESS_LOCK:-/run/hoa-egress-firewall.lock}"
read -r -a RESOLV_FILES <<<"${HOA_EGRESS_RESOLV:-/run/systemd/resolve/resolv.conf /etc/resolv.conf}"
CHAIN=HOA-EGRESS
DROP_CHAIN=HOA-EGRESS-DROP
# Ranges no setting may leave out (the home LAN, Proxmox, cloud metadata, loopback).
REQUIRED_RANGES="10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 100.64.0.0/10 169.254.0.0/16 127.0.0.0/8"

die() { echo "hoa-egress-firewall: $*" >&2; exit 2; }

# --- Settings ------------------------------------------------------------------------------

PROJECT="" APP_TCP_PORTS="" TUNNEL_NETWORK="" TUNNEL_PORTS="" BLOCKED_RANGES="" DNS_SERVERS="" LOG_RATE=""

# read_conf <file>: KEY=value lines; only the known keys; nothing is executed.
read_conf() {
  local file="$1" line key value
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%%#*}"
    line="${line#"${line%%[![:space:]]*}"}"
    line="${line%"${line##*[![:space:]]}"}"
    [ -n "$line" ] || continue
    [[ "$line" =~ ^([A-Z0-9_]+)=(.*)$ ]] || die "$file: not a KEY=value line: ${line:0:80}"
    key="${BASH_REMATCH[1]}"
    value="${BASH_REMATCH[2]}"
    value="${value%"${value##*[![:space:]]}"}"
    case "$key" in
      PROJECT | APP_TCP_PORTS | TUNNEL_NETWORK | TUNNEL_PORTS | BLOCKED_RANGES | DNS_SERVERS | LOG_RATE)
        printf -v "$key" '%s' "$value" ;;
      *) die "$file: unknown setting $key" ;;
    esac
  done <"$file"
}

is_ipv4() {
  local ip="$1" part
  [[ "$ip" =~ ^([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})$ ]] || return 1
  for part in "${BASH_REMATCH[@]:1}"; do [ "$((10#$part))" -le 255 ] || return 1; done
}

check_ports() {  # check_ports <name> <list>
  local port count=0
  [[ "$2" =~ ^[0-9]{1,5}(,[0-9]{1,5})*$ ]] || die "$1 must be ports separated by commas (got '${2:0:80}')"
  for port in ${2//,/ }; do
    count=$((count + 1))
    { [ "$((10#$port))" -ge 1 ] && [ "$((10#$port))" -le 65535 ]; } || die "$1: $port is not a port"
    [ "$((10#$port))" -ne 25 ] || die "$1: port 25 stays blocked (no direct-to-MX sending, D101)"
  done
  [ "$count" -le 15 ] || die "$1: at most 15 ports"
}

load_conf() {
  [ -f "$CONF" ] || die "no settings file $CONF (run: sudo bash infra/poc/harden-host.sh)"
  read_conf "$CONF"
  if [ -f "$LOCAL_CONF" ]; then read_conf "$LOCAL_CONF"; fi
  [[ "$PROJECT" =~ ^[a-z0-9][a-z0-9_-]{0,62}$ ]] || die "PROJECT must be a compose project name"
  [[ "$TUNNEL_NETWORK" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$ ]] || die "TUNNEL_NETWORK must be a network name"
  check_ports APP_TCP_PORTS "$APP_TCP_PORTS"
  check_ports TUNNEL_PORTS "$TUNNEL_PORTS"
  [[ "$LOG_RATE" =~ ^[0-9]{1,4}/(second|minute|hour|day)$ ]] || die "LOG_RATE must look like 6/minute"
  local range required
  for range in $BLOCKED_RANGES; do
    { [[ "$range" =~ ^[0-9.]+/([0-9]{1,2})$ ]] && [ "${BASH_REMATCH[1]}" -le 32 ] \
      && is_ipv4 "${range%/*}"; } || die "BLOCKED_RANGES: $range is not an IPv4 range"
  done
  for required in $REQUIRED_RANGES; do
    [[ " $BLOCKED_RANGES " == *" $required "* ]] || die "BLOCKED_RANGES must include $required"
  done
  local dns
  for dns in $DNS_SERVERS; do is_ipv4 "$dns" || die "DNS_SERVERS: $dns is not an IPv4 address"; done
}

# dns_servers: DNS_SERVERS, else the host's upstream resolvers (what Docker's
# embedded resolver forwards to; loopback left out), else Docker's fallback.
dns_servers() {
  local file found=""
  # shellcheck disable=SC2086 # a space-separated list, checked by load_conf
  if [ -n "$DNS_SERVERS" ]; then printf '%s\n' $DNS_SERVERS | sort -u | head -6; return 0; fi
  for file in "${RESOLV_FILES[@]}"; do
    [ -f "$file" ] || continue
    found="$(awk '$1 == "nameserver" { print $2 }' "$file" | while IFS= read -r ip; do
      if is_ipv4 "$ip" && [[ "$ip" != 127.* ]]; then echo "$ip"; fi
    done | sort -u | head -6)"
    if [ -n "$found" ]; then printf '%s\n' "$found"; return 0; fi
  done
  printf '8.8.4.4\n8.8.8.8\n'
}

# --- The portal's networks -------------------------------------------------------------------

# portal_networks: "<network name> <bridge interface>" per bridge network of
# the compose project, sorted. Fails (changing nothing) when Docker does not answer.
portal_networks() {
  local names name info driver id option bridge
  names="$(docker network ls --filter "label=com.docker.compose.project=$PROJECT" --format '{{.Name}}')" \
    || die "Docker is not answering (docker network ls failed); nothing changed"
  while IFS= read -r name; do
    [[ "$name" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$ ]] || continue
    info="$(docker network inspect -f '{{.Driver}} {{.Id}} {{index .Options "com.docker.network.bridge.name"}}' "$name")" \
      || die "could not read the network $name; nothing changed"
    read -r driver id option _ <<<"$info"
    [ "$driver" = bridge ] || continue
    [ "$option" != "<no value>" ] || option=""
    [[ "$id" =~ ^[0-9a-f]{12,}$ ]] || die "network $name has an unexpected id"
    bridge="${option:-br-${id:0:12}}"
    [[ "$bridge" =~ ^[A-Za-z0-9_.-]{1,15}$ ]] || die "network $name has an unexpected bridge name"
    printf '%s %s\n' "$name" "$bridge"
  done <<<"$names" | sort
}

# --- The rules -------------------------------------------------------------------------------

NETS=""

# render_body <4|6>: the rules of our two chains, without the fingerprint.
render_body() {
  local family="$1" name bridge dns range edge=""
  local -a bridges=() servers=()
  while read -r name bridge; do
    [ -n "$name" ] || continue
    bridges+=("$bridge")
    if [ "$name" = "${PROJECT}_${TUNNEL_NETWORK}" ]; then edge="$bridge"; fi
  done <<<"$NETS"
  echo "-A $CHAIN -m conntrack --ctstate RELATED,ESTABLISHED -j RETURN"
  for bridge in "${bridges[@]}"; do
    echo "-A $CHAIN -i $bridge -o $bridge -j RETURN"
  done
  if [ "$family" = 4 ]; then
    mapfile -t servers < <(dns_servers)
    for bridge in "${bridges[@]}"; do
      for dns in "${servers[@]}"; do
        echo "-A $CHAIN -i $bridge -d $dns/32 -p udp -m udp --dport 53 -j RETURN"
        echo "-A $CHAIN -i $bridge -d $dns/32 -p tcp -m tcp --dport 53 -j RETURN"
      done
    done
    for bridge in "${bridges[@]}"; do
      for range in $BLOCKED_RANGES; do
        echo "-A $CHAIN -i $bridge -d $range -j $DROP_CHAIN"
      done
    done
    for bridge in "${bridges[@]}"; do
      echo "-A $CHAIN -i $bridge -p tcp -m multiport --dports $APP_TCP_PORTS -j RETURN"
    done
    if [ -n "$edge" ]; then
      echo "-A $CHAIN -i $edge -p tcp -m multiport --dports $TUNNEL_PORTS -j RETURN"
      echo "-A $CHAIN -i $edge -p udp -m multiport --dports $TUNNEL_PORTS -j RETURN"
    fi
  fi
  for bridge in "${bridges[@]}"; do
    echo "-A $CHAIN -i $bridge -j $DROP_CHAIN"
  done
  echo "-A $DROP_CHAIN -m limit --limit $LOG_RATE --limit-burst 10 -j LOG --log-prefix \"hoa-egress-drop: \" --log-level 4"
  echo "-A $DROP_CHAIN -j DROP"
}

fingerprint() {
  { render_body 4; render_body 6; } | sha256sum | cut -c1-16
}

# render <4|6> <fingerprint>: an iptables-restore file for our chains only.
render() {
  local count
  count="$(grep -c . <<<"$NETS" || true)"
  echo "*filter"
  echo ":$CHAIN - [0:0]"
  echo ":$DROP_CHAIN - [0:0]"
  render_body "$1" | sed "1s/^-A $CHAIN /-A $CHAIN -m comment --comment \"hoa-egress $2 networks=$count\" /"
  echo "COMMIT"
}

# hook <iptables|ip6tables> <parent chain>: the jump to our chain is the
# parent's first rule, exactly once.
hook() {
  local ipt="$1" parent="$2"
  if [ "$("$ipt" -w -S "$parent" | sed -n 2p)" = "-A $parent -j $CHAIN" ] \
    && [ "$("$ipt" -w -S "$parent" | grep -cxF -- "-A $parent -j $CHAIN")" -eq 1 ]; then
    return 0
  fi
  while "$ipt" -w -D "$parent" -j "$CHAIN" 2>/dev/null; do :; done
  "$ipt" -w -I "$parent" 1 -j "$CHAIN"
}

hooked() {  # hooked <iptables|ip6tables> <parent chain>
  [ "$("$1" -w -S "$2" 2>/dev/null | sed -n 2p)" = "-A $2 -j $CHAIN" ]
}

has_ip6() { command -v ip6tables >/dev/null 2>&1 && ip6tables -w -n -L FORWARD >/dev/null 2>&1; }

# The IPv6 parent: Docker's DOCKER-USER when Docker manages IPv6, else FORWARD.
ip6_parent() {
  if ip6tables -w -n -L DOCKER-USER >/dev/null 2>&1; then echo DOCKER-USER; else echo FORWARD; fi
}

prepare() {
  load_conf
  NETS="$(portal_networks)"
  FPR="$(fingerprint)"
}

apply() {  # apply [--quiet]: --quiet says nothing when the rules are already current
  local quiet="${1:-}"
  exec 8>"$LOCK"
  flock -w 60 8 || die "another apply is still running"
  prepare
  command -v iptables-restore >/dev/null 2>&1 || die "iptables-restore is not installed"
  local count
  count="$(grep -c . <<<"$NETS" || true)"
  # Already in place for exactly these networks and settings: nothing to do
  # (the timer runs this every minute).
  if [ "$(current_state)" = active ]; then
    [ "$quiet" = --quiet ] || echo "Outbound firewall already on and current ($count portal network(s))."
    return 0
  fi
  # Docker makes DOCKER-USER when it starts; made here if Docker has not yet.
  iptables -w -n -L DOCKER-USER >/dev/null 2>&1 || iptables -w -N DOCKER-USER
  render 4 "$FPR" | iptables-restore -w --noflush
  hook iptables DOCKER-USER
  if has_ip6; then
    render 6 "$FPR" | ip6tables-restore -w --noflush
    hook ip6tables "$(ip6_parent)"
  fi
  if [ "$count" -eq 0 ]; then
    echo "Outbound firewall loaded; no portal networks exist yet (they are covered within a minute of the portal starting)."
  else
    echo "Outbound firewall on for $count portal network(s): $(cut -d' ' -f1 <<<"$NETS" | tr '\n' ' ')"
  fi
}

# current_state: active (hooked first in DOCKER-USER, and in IPv6 too, with
# rules for exactly these networks and settings), stale (ours, but for other
# networks or settings) or missing. Needs prepare.
current_state() {
  if ! iptables -w -C FORWARD -j DOCKER-USER 2>/dev/null || ! hooked iptables DOCKER-USER \
    || ! iptables -w -S "$CHAIN" >/dev/null 2>&1; then
    echo missing
  elif ! iptables -w -S "$CHAIN" | grep -qF "hoa-egress $FPR "; then
    echo stale
  elif has_ip6 && ! hooked ip6tables "$(ip6_parent)"; then
    echo missing
  elif has_ip6 && ! ip6tables -w -S "$CHAIN" 2>/dev/null | grep -qF "hoa-egress $FPR "; then
    echo stale
  else
    echo active
  fi
}

# status: "active <n>", "stale <n>", "missing <n>" or "no-networks 0"; exit 0 only when active.
status() {
  prepare
  local count state
  count="$(grep -c . <<<"$NETS" || true)"
  state="$(current_state)"
  if [ "$state" = active ] && [ "$count" -eq 0 ]; then state=no-networks; fi
  echo "$state $count"
  [ "$state" = active ]
}

remove() {
  local ipt parent
  systemctl disable --now hoa-egress-firewall.timer >/dev/null 2>&1 || true
  for ipt in iptables ip6tables; do
    command -v "$ipt" >/dev/null 2>&1 || continue
    for parent in DOCKER-USER FORWARD; do
      while "$ipt" -w -D "$parent" -j "$CHAIN" 2>/dev/null; do :; done
    done
    "$ipt" -w -F "$CHAIN" 2>/dev/null || true
    "$ipt" -w -X "$CHAIN" 2>/dev/null || true
    "$ipt" -w -F "$DROP_CHAIN" 2>/dev/null || true
    "$ipt" -w -X "$DROP_CHAIN" 2>/dev/null || true
  done
  echo "Outbound firewall removed: the portal's containers can connect anywhere until"
  echo "System status -> Server tasks -> Re-apply host security puts it back (the health check fails meanwhile)."
}

case "${1:-}" in
  apply) apply "${2:-}" ;;
  status) status ;;
  show) prepare; render 4 "$FPR"; if has_ip6; then echo "# IPv6"; render 6 "$FPR"; fi ;;
  remove) remove ;;
  *) echo "Usage: hoa-egress-firewall apply|status|show|remove" >&2; exit 2 ;;
esac
