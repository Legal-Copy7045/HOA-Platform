#!/usr/bin/env bash
# Host hardening for the portal VM (Debian/Ubuntu). Run once; safe to re-run.
#
#   sudo bash /opt/hoa-portal/infra/poc/harden-host.sh
#
# Installs and configures:
#   1. Automatic security updates (unattended-upgrades). No automatic reboot.
#   2. auditd: records every change to sensitive files (.env, the backup key,
#      SSH keys and config, sudoers, users and groups, systemd units).
#   3. A nightly file-integrity check (AIDE) of /etc, /root, /usr/local, /boot
#      and the hoa user's SSH keys.
#   4. Falco (modern eBPF, no kernel module): watches what happens inside the
#      containers and on the host at runtime: shells in containers, writes
#      to system folders, reads of secrets, privilege changes.
#
# Alerts go by email to the portal's sysadmins, through the portal's own mail
# settings (python manage.py host_alert): Falco findings every 10 minutes at
# most, audit and integrity changes once a day. System status shows whether
# these services are running (via the update agent).
#
# Memory: Falco about 100-150 MB; the rest is negligible.
set -euo pipefail
trap 'echo "ERROR: harden-host.sh stopped at line $LINENO (exit $?)." >&2' ERR

[ "$(id -u)" -eq 0 ] || { echo "Run with sudo." >&2; exit 1; }
SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
APP_DIR="$(cd "$(dirname "$SCRIPT_PATH")/../.." && pwd)"
APP_USER="$(stat -c %U "$APP_DIR")"
APP_HOME="$(getent passwd "$APP_USER" | cut -d: -f6)"
KEY_FILE="${HOA_BACKUP_KEY:-/root/hoa-backup.key}"
STATE_DIR=/var/lib/hoa-host-security
mkdir -p "$STATE_DIR"; chmod 700 "$STATE_DIR"
export DEBIAN_FRONTEND=noninteractive

echo "==> 1. Automatic security updates"
apt-get update -qq
apt-get install -y -qq --no-install-recommends unattended-upgrades apt-listchanges python3 >/dev/null
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'CONF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
CONF
cat > /etc/apt/apt.conf.d/52hoa-unattended <<'CONF'
// Security updates only; never reboot on its own (the board decides when).
Unattended-Upgrade::Automatic-Reboot "false";
Unattended-Upgrade::Remove-Unused-Dependencies "true";
CONF
systemctl enable --now apt-daily-upgrade.timer >/dev/null
# Ubuntu Pro, when attached: ESM security updates arrive through the same
# automatic updates (Ubuntu's defaults already include them); Livepatch
# applies kernel fixes without a reboot.
if command -v pro >/dev/null 2>&1 && pro status --format json 2>/dev/null | grep -q '"attached": *true'; then
  pro enable livepatch --assume-yes >/dev/null 2>&1 || echo "    (Livepatch could not be enabled; see: pro status)"
fi

echo "==> 2. Audit log for sensitive files (auditd)"
apt-get install -y -qq --no-install-recommends auditd >/dev/null
{
  echo "# Portal VM: changes to sensitive files (infra/poc/harden-host.sh)."
  echo "-w $APP_DIR/.env -p wa -k hoa_env"
  echo "-w $KEY_FILE -p rwa -k hoa_backup_key"
  echo "-w /etc/ssh/sshd_config -p wa -k hoa_ssh"
  echo "-w /etc/ssh/sshd_config.d -p wa -k hoa_ssh"
  echo "-w /root/.ssh -p wa -k hoa_ssh"
  [ -n "$APP_HOME" ] && echo "-w $APP_HOME/.ssh -p wa -k hoa_ssh"
  echo "-w /etc/sudoers -p wa -k hoa_sudo"
  echo "-w /etc/sudoers.d -p wa -k hoa_sudo"
  echo "-w /etc/passwd -p wa -k hoa_identity"
  echo "-w /etc/shadow -p wa -k hoa_identity"
  echo "-w /etc/group -p wa -k hoa_identity"
  echo "-w /etc/systemd/system -p wa -k hoa_systemd"
  echo "-w /etc/docker -p wa -k hoa_docker"
} > /etc/audit/rules.d/hoa-portal.rules
augenrules --load >/dev/null 2>&1 || true
systemctl enable --now auditd >/dev/null

echo "==> 3. Nightly file-integrity check (AIDE)"
apt-get install -y -qq --no-install-recommends aide >/dev/null
# Our own timer and config replace Ubuntu's daily AIDE job (which would mail
# root through a local mail server this VM doesn't need).
systemctl disable --now dailyaidecheck.timer >/dev/null 2>&1 || true
cat > "$STATE_DIR/aide.conf" <<CONF
# Portal VM file-integrity check (infra/poc/harden-host.sh).
database_in=file:$STATE_DIR/aide.db
database_out=file:$STATE_DIR/aide.db.new
gzip_dbout=no
HOA = p+i+n+u+g+s+m+c+sha256
/etc HOA
!/etc/mtab\$
!/etc/adjtime\$
!/etc/resolv.conf\$
!/etc/.*~\$
/root HOA
!/root/.cache
!/root/hoa-files-move
/usr/local HOA
/boot HOA
CONF
[ -n "$APP_HOME" ] && [ -d "$APP_HOME/.ssh" ] && echo "$APP_HOME/.ssh HOA" >> "$STATE_DIR/aide.conf"
if [ ! -f "$STATE_DIR/aide.db" ]; then
  echo "    building the first baseline (a minute or two)"
  aide --config "$STATE_DIR/aide.conf" --init >/dev/null 2>&1 || true
  mv -f "$STATE_DIR/aide.db.new" "$STATE_DIR/aide.db"
fi
install -m 0755 /dev/stdin /usr/local/sbin/hoa-integrity-check <<'SCRIPT'
#!/usr/bin/env bash
# Nightly: report changed system files and sensitive-file audit events. Only
# once the report reached a sysadmin (host_alert exits 0 only when it sent to
# at least one) is today's state accepted as the new baseline; otherwise the
# old baseline stays and the change is reported again next night (R2-006).
set -uo pipefail
STATE_DIR=/var/lib/hoa-host-security
APP_DIR="$(cat "$STATE_DIR/app_dir")"
COMPOSE=(docker compose --project-directory "$APP_DIR" -f docker-compose.yml -f docker-compose.tunnel.yml)
alert() { "${COMPOSE[@]}" exec -T web python manage.py host_alert --kind "$1" >/dev/null 2>&1; }
status=0
report="$(aide --config "$STATE_DIR/aide.conf" --check 2>&1)"; rc=$?
# AIDE exits 1-7 when files were added, removed or changed; 0 when none.
if [ "$rc" -ge 1 ] && [ "$rc" -le 7 ]; then
  # Cut to 150 lines first: under pipefail a head inside the pipe could fail
  # it (SIGPIPE) even after a successful send.
  report="$(printf '%s\n' "$report" | head -150)"
  if printf '%s\n' "$report" | alert integrity; then
    aide --config "$STATE_DIR/aide.conf" --update >/dev/null 2>&1 || true
    [ -f "$STATE_DIR/aide.db.new" ] && mv -f "$STATE_DIR/aide.db.new" "$STATE_DIR/aide.db"
  else
    echo "Integrity report not delivered; keeping the old baseline to report it again." >&2
    rm -f "$STATE_DIR/aide.db.new"
    status=1
  fi
fi
# Audit events on the watched files since the last run (ausearch remembers
# where it stopped in the checkpoint file; the previous checkpoint is put back
# when the report could not be delivered, so the events are sent next time).
[ -f "$STATE_DIR/audit.checkpoint" ] && cp -p "$STATE_DIR/audit.checkpoint" "$STATE_DIR/audit.checkpoint.prev"
events="$(ausearch --checkpoint "$STATE_DIR/audit.checkpoint" -m SYSCALL -i 2>/dev/null \
  | grep -E ' key=hoa_' | sed -E 's/.* comm=([^ ]+).* exe=([^ ]+).* key=([^ ]+).*/\3 by \1 (\2)/' \
  | sort | uniq -c | sort -rn | head -40)"
if [ -n "$events" ] && ! printf 'Changes to watched files since the last report (count, watch, program):\n%s\n' "$events" | alert audit; then
  echo "Audit report not delivered; it will be sent again next time." >&2
  if [ -f "$STATE_DIR/audit.checkpoint.prev" ]; then
    mv -f "$STATE_DIR/audit.checkpoint.prev" "$STATE_DIR/audit.checkpoint"
  else
    rm -f "$STATE_DIR/audit.checkpoint"
  fi
  status=1
fi
rm -f "$STATE_DIR/audit.checkpoint.prev"
exit "$status"
SCRIPT
echo "$APP_DIR" > "$STATE_DIR/app_dir"
cat > /etc/systemd/system/hoa-integrity-check.service <<'UNIT'
[Unit]
Description=HOA portal: nightly file-integrity and audit report
After=docker.service

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/hoa-integrity-check
Nice=10
IOSchedulingClass=idle
UNIT
cat > /etc/systemd/system/hoa-integrity-check.timer <<'UNIT'
[Unit]
Description=HOA portal: nightly file-integrity and audit report

[Timer]
OnCalendar=*-*-* 03:30:00
RandomizedDelaySec=20min
Persistent=true

[Install]
WantedBy=timers.target
UNIT

echo "==> 4. Falco (runtime monitoring of the containers)"
# Falco's package-signing key is trusted only with the fingerprint Falco
# publishes (R2-009): the "Trusted" key in the table at
# https://falco.org/docs/setup/packages/ (source: github.com/falcosecurity/
# falco-website, content/en/docs/setup/packages.md), checked 2026-11-26.
# When Falco rotates its key, check that page and re-run with
# HOA_FALCO_KEY_FPR=<the new fingerprint>. The downloaded bundle also holds
# revoked keys; only the pinned key goes into the keyring apt uses.
FALCO_KEY_FPR_PUBLISHED="478B2FBBC75F4237B731DA4365106822B35B1B1F"
FALCO_KEY_FPR="$(printf '%s' "${HOA_FALCO_KEY_FPR:-$FALCO_KEY_FPR_PUBLISHED}" | tr -d ' ' | tr 'a-f' 'A-F')"
FALCO_KEYRING=/usr/share/keyrings/falco-archive-keyring.gpg
apt-get install -y -qq --no-install-recommends curl gnupg >/dev/null
# On a re-run, a keyring that holds exactly the pinned key (checked here, not
# trusted by its name) is kept, so the step does not need falco.org (R2-006).
FALCO_INSTALLED="$(gpg --show-keys --with-colons "$FALCO_KEYRING" 2>/dev/null \
  | awk -F: '$1 == "pub" {p = 1; next} p && $1 == "fpr" {print $10; p = 0}' || true)"
if [ -s "$FALCO_KEYRING" ] && [ "$FALCO_INSTALLED" = "$FALCO_KEY_FPR" ]; then
  echo "    Falco signing key $FALCO_KEY_FPR already installed and verified"
else
  FALCO_TMP="$(mktemp -d)"; chmod 700 "$FALCO_TMP"
  curl -fsSL https://falco.org/repo/falcosecurity-packages.asc -o "$FALCO_TMP/falco.asc"
  FALCO_FOUND="$(gpg --show-keys --with-colons "$FALCO_TMP/falco.asc" 2>/dev/null | awk -F: '$1 == "fpr" {print $10}' || true)"
  if ! printf '%s\n' "$FALCO_FOUND" | grep -qxF "$FALCO_KEY_FPR"; then
    echo "Falco's signing key does not have the expected fingerprint." >&2
    echo "  expected: $FALCO_KEY_FPR" >&2
    echo "  found:    $(printf '%s' "$FALCO_FOUND" | tr '\n' ' ')" >&2
    echo "Not installing Falco. If Falco has rotated its key, compare with the table at" >&2
    echo "https://falco.org/docs/setup/packages/ and re-run with HOA_FALCO_KEY_FPR=<fingerprint>." >&2
    rm -rf "$FALCO_TMP"
    exit 1
  fi
  mkdir -p -m 700 "$FALCO_TMP/gnupg"
  GNUPGHOME="$FALCO_TMP/gnupg" gpg --batch --quiet --import "$FALCO_TMP/falco.asc"
  GNUPGHOME="$FALCO_TMP/gnupg" gpg --batch --export "$FALCO_KEY_FPR" > "$FALCO_TMP/falco.gpg"
  [ -s "$FALCO_TMP/falco.gpg" ] || { echo "Could not export Falco's key $FALCO_KEY_FPR." >&2; rm -rf "$FALCO_TMP"; exit 1; }
  install -m 0644 "$FALCO_TMP/falco.gpg" "$FALCO_KEYRING"
  GNUPGHOME="$FALCO_TMP/gnupg" gpgconf --kill all >/dev/null 2>&1 || true
  rm -rf "$FALCO_TMP"
  echo "    Falco signing key $FALCO_KEY_FPR verified"
fi
echo "deb [signed-by=/usr/share/keyrings/falco-archive-keyring.gpg] https://download.falco.org/packages/deb stable main" \
  > /etc/apt/sources.list.d/falcosecurity.list
apt-get update -qq
# Modern eBPF: no kernel module or headers needed; no automatic rule updates.
FALCO_FRONTEND=noninteractive FALCO_DRIVER_CHOICE=modern_ebpf FALCOCTL_ENABLED=no \
  apt-get install -y -qq --no-install-recommends falco >/dev/null
mkdir -p /etc/falco/config.d /etc/falco/rules.d /var/log/falco
cat > /etc/falco/config.d/hoa-portal.yaml <<'CONF'
# Portal VM (infra/poc/harden-host.sh): JSON alerts of warning and above to a
# file; a timer emails a summary to the sysadmins.
engine:
  kind: modern_ebpf
json_output: true
priority: warning
file_output:
  enabled: true
  keep_alive: false
  filename: /var/log/falco/alerts.jsonl
stdout_output:
  enabled: false
CONF
cat > /etc/falco/rules.d/hoa-portal.yaml <<'RULES'
# Portal-specific additions. The default Falco rules already cover shells in
# containers, writes below /etc and /root, sensitive file reads, privilege
# escalation and package managers in containers.

- list: hoa_portal_images
  items: [oak-creek-portal]

- rule: HOA portal container wrote a file outside /tmp
  desc: The portal containers run read-only; any write outside /tmp is unexpected.
  condition: >
    open_write and container and container.image.repository in (hoa_portal_images)
    and not fd.name startswith /tmp/ and not fd.name startswith /dev/
    and not fd.name startswith /proc/ and not fd.name startswith /sys/
  output: >
    Portal container wrote a file (file=%fd.name process=%proc.name
    container=%container.name image=%container.image.repository)
  priority: WARNING
  tags: [hoa_portal]

- rule: HOA portal container started an unexpected program
  desc: The portal image runs python, gunicorn and its entrypoint; anything else is suspicious.
  condition: >
    spawned_process and container and container.image.repository in (hoa_portal_images)
    and not proc.name in (python, python3, gunicorn, sh, entrypoint.sh)
  output: >
    Unexpected program in a portal container (process=%proc.name cmdline=%proc.cmdline
    parent=%proc.pname container=%container.name)
  priority: WARNING
  tags: [hoa_portal]
RULES
install -m 0755 /dev/stdin /usr/local/sbin/hoa-host-alerts <<'SCRIPT'
#!/usr/bin/env bash
# Every 10 minutes: email new Falco alerts (summarised) to the sysadmins.
set -uo pipefail
STATE_DIR=/var/lib/hoa-host-security
APP_DIR="$(cat "$STATE_DIR/app_dir")"
LOG=/var/log/falco/alerts.jsonl
[ -f "$LOG" ] || exit 0
size="$(stat -c %s "$LOG")"
offset="$(cat "$STATE_DIR/falco_offset" 2>/dev/null || echo 0)"
[ "$size" -lt "$offset" ] && offset=0   # rotated
[ "$size" -eq "$offset" ] && exit 0
summary="$(tail -c +"$((offset + 1))" "$LOG" | head -c "$((size - offset))" | python3 -c '
import collections, json, sys
counts = collections.Counter()
examples = {}
for line in sys.stdin:
    try:
        a = json.loads(line)
    except ValueError:
        continue
    key = (a.get("priority", "?"), a.get("rule", "?"))
    counts[key] += 1
    examples.setdefault(key, (a.get("output") or "")[:300])
for (prio, rule), n in counts.most_common(20):
    print(f"{n} x {prio}: {rule}\n    e.g. {examples[(prio, rule)]}")
')"
# The offset moves past these alerts only once they reached a sysadmin
# (host_alert exits 0 only then); otherwise they are sent again next run (R2-006).
if [ -n "$summary" ]; then
  COMPOSE=(docker compose --project-directory "$APP_DIR" -f docker-compose.yml -f docker-compose.tunnel.yml)
  if ! printf 'New Falco alerts (count x priority: rule):\n\n%s\n' "$summary" \
    | "${COMPOSE[@]}" exec -T web python manage.py host_alert --kind falco >/dev/null 2>&1; then
    echo "Falco alerts not delivered; they will be sent again next run." >&2
    exit 1
  fi
fi
echo "$size" > "$STATE_DIR/falco_offset"
exit 0
SCRIPT
cat > /etc/systemd/system/hoa-host-alerts.service <<'UNIT'
[Unit]
Description=HOA portal: email new Falco alerts
After=docker.service

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/hoa-host-alerts
UNIT
cat > /etc/systemd/system/hoa-host-alerts.timer <<'UNIT'
[Unit]
Description=HOA portal: email new Falco alerts every 10 minutes

[Timer]
OnBootSec=5min
OnUnitActiveSec=10min

[Install]
WantedBy=timers.target
UNIT
cat > /etc/logrotate.d/hoa-falco <<'CONF'
/var/log/falco/alerts.jsonl {
  weekly
  rotate 8
  compress
  missingok
  notifempty
  copytruncate
}
CONF

systemctl daemon-reload
systemctl enable --now falco-modern-bpf.service >/dev/null
systemctl enable --now hoa-integrity-check.timer hoa-host-alerts.timer >/dev/null

echo
echo "Status:"
if command -v canonical-livepatch >/dev/null 2>&1; then
  printf '  %-28s %s\n' "livepatch" "$(canonical-livepatch status 2>/dev/null | grep -m1 -oE 'running: *(true|false)' || echo unknown)"
fi
for unit in apt-daily-upgrade.timer auditd.service hoa-integrity-check.timer falco-modern-bpf.service hoa-host-alerts.timer; do
  printf '  %-28s %s\n' "$unit" "$(systemctl is-active "$unit" 2>/dev/null || true)"
done
echo
echo "Done. Alerts are emailed to the portal's sysadmins. System status shows these services"
echo "within a minute. For the first day Falco may report things that are normal here;"
echo "send them to Claude to tune the rules (/etc/falco/rules.d/hoa-portal.yaml)."
