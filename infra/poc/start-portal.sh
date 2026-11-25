#!/usr/bin/env bash
# Builds and starts the portal behind the Cloudflare tunnel on the POC VM, and
# installs the daily cloudflared update timer.
#
#   cd /opt/hoa-portal && sudo bash infra/poc/start-portal.sh                 # normal
#   cd /opt/hoa-portal && sudo bash infra/poc/start-portal.sh --full-restart  # stop everything first
#   cd /opt/hoa-portal && sudo bash infra/poc/start-portal.sh --pull          # also refresh base images
#
# Safe to re-run (e.g. after `git pull`): it rebuilds and restarts only what
# changed. Postgres, the file store, the scanner and the tunnel keep running
# unless their own settings changed, so an update means seconds of downtime.
set -euo pipefail
trap 'echo "ERROR: start-portal.sh stopped at line $LINENO (exit $?)." >&2' ERR

FULL_RESTART=0
PULL=()
for arg in "$@"; do
  case "$arg" in
    --full-restart) FULL_RESTART=1 ;;
    --pull) PULL=(--pull) ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done

[ "$(id -u)" -eq 0 ] || { echo "Run with sudo." >&2; exit 1; }
APP_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$APP_DIR"
[ -f .env ] || { echo "No .env yet: run infra/poc/make-env.sh first." >&2; exit 1; }
if [ "$(stat -c %a .env)" != "600" ]; then
  echo "Fixing .env permissions to 600"
  chmod 600 .env
fi

COMPOSE=(docker compose --project-directory "$APP_DIR" -f docker-compose.yml -f docker-compose.tunnel.yml)

# Settings added since this .env was made (decision D21). Only missing keys
# are added; anything already set is left as it is.
add_setting() {
  local key="$1" value="$2"
  if ! grep -q "^${key}=" .env; then
    echo "Adding ${key}=${value} to .env"
    printf '%s=%s\n' "$key" "$value" >> .env
  fi
}
if grep -q '^DEPLOYMENT_STAGE=poc' .env; then
  add_setting BOARD_SEAT_SECOND_APPROVAL false
fi
if grep -q '^DOCUMENT_SCANNER=none' .env; then
  echo "Switching malware scanning on (ClamAV container)"
  sed -i 's/^DOCUMENT_SCANNER=none/DOCUMENT_SCANNER=clamd/' .env
  NEEDS_RESCAN=1
fi
# The local file store is used unless .env points at Cloudflare R2.
if ! grep -qE '^S3_ENDPOINT_URL=https://[^ ]*r2\.cloudflarestorage\.com' .env; then
  add_setting COMPOSE_PROFILES local-store
fi
add_setting DOCUMENT_SCANNER clamd
add_setting DOCUMENT_CLAMD_ADDRESS clamav:3310
sed -i 's/^DOCUMENT_CLAMD_ADDRESS=$/DOCUMENT_CLAMD_ADDRESS=clamav:3310/' .env
sed -i 's/^DOCUMENT_REQUIRE_CLEAN_SCAN=false/DOCUMENT_REQUIRE_CLEAN_SCAN=true/' .env
add_setting DOCUMENT_REQUIRE_CLEAN_SCAN true
sed -i 's/^LEDGER_LATE_FEES_ENABLED=false/LEDGER_LATE_FEES_ENABLED=true/' .env

# Show the deployed commit on the sysadmin status page.
export APP_VERSION="$(git -C "$APP_DIR" rev-parse --short HEAD 2>/dev/null || echo unknown)"

# Base images are refreshed weekly (or with --pull), not on every update:
# a moved base image would otherwise force a full rebuild each time.
STAMP=/var/lib/hoa-portal/last-base-pull
mkdir -p "$(dirname "$STAMP")"
if [ ${#PULL[@]} -eq 0 ] && { [ ! -f "$STAMP" ] || [ -n "$(find "$STAMP" -mtime +6)" ]; }; then
  PULL=(--pull)
fi
echo "==> Building the portal image${PULL:+ (refreshing base images)}"
DOCKER_BUILDKIT=1 "${COMPOSE[@]}" build "${PULL[@]}" web
[ ${#PULL[@]} -eq 0 ] || touch "$STAMP"
if [ "$FULL_RESTART" -eq 1 ]; then
  echo "==> Stopping the old containers (data volumes are kept)"
  "${COMPOSE[@]}" down --remove-orphans
fi
echo "==> Starting (migrations run first, as the owner role; unchanged services keep running)"
"${COMPOSE[@]}" up -d --remove-orphans --wait --wait-timeout 300 \
  || { echo "Some services are not healthy yet; status below." >&2; }

echo "==> Installing daily cloudflared update"
cat > /etc/systemd/system/hoa-cloudflared-update.service <<UNIT
[Unit]
Description=Update the HOA portal's cloudflared container if a new release exists
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
WorkingDirectory=$APP_DIR
ExecStart=/usr/bin/docker compose --project-directory $APP_DIR -f docker-compose.yml -f docker-compose.tunnel.yml pull cloudflared
ExecStart=/usr/bin/docker compose --project-directory $APP_DIR -f docker-compose.yml -f docker-compose.tunnel.yml up -d --no-deps cloudflared
ExecStartPost=/usr/bin/docker image prune -f
UNIT
cat > /etc/systemd/system/hoa-cloudflared-update.timer <<'UNIT'
[Unit]
Description=Daily cloudflared update check for the HOA portal

[Timer]
OnCalendar=*-*-* 04:00:00
RandomizedDelaySec=1h
Persistent=true

[Install]
WantedBy=timers.target
UNIT
systemctl daemon-reload
systemctl enable --now hoa-cloudflared-update.timer >/dev/null

echo "==> Installing the update agent (System status: update available / install)"
cat > /etc/systemd/system/hoa-portal-agent.service <<UNIT
[Unit]
Description=HOA portal update agent (checks GitHub; installs updates requested on System status)
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
ExecStart=/usr/bin/bash $APP_DIR/infra/poc/portal-agent.sh
TimeoutStartSec=30min
UNIT
cat > /etc/systemd/system/hoa-portal-agent.timer <<'UNIT'
[Unit]
Description=Run the HOA portal update agent every minute

[Timer]
OnBootSec=2min
OnUnitInactiveSec=1min
AccuracySec=10s

[Install]
WantedBy=timers.target
UNIT
systemctl daemon-reload
systemctl enable --now hoa-portal-agent.timer >/dev/null

"${COMPOSE[@]}" ps --format 'table {{.Service}}\t{{.Status}}'
if [ "${NEEDS_RESCAN:-0}" = "1" ]; then
  echo
  echo "Malware scanning is now on. Files uploaded before today can't be downloaded"
  echo "until they are scanned once. Run this, with your sysadmin login email:"
  echo
  echo "  docker compose -f docker-compose.yml -f docker-compose.tunnel.yml exec web \\"
  echo "    python manage.py rescan_files --actor you@example.org --reason \"malware scanning switched on\""
fi
echo
echo "File store check:"
"${COMPOSE[@]}" exec -T web python manage.py check_storage 2>&1 | grep -v '"level"' || true
echo
echo "Tunnel log (last lines; look for 'Registered tunnel connection'):"
"${COMPOSE[@]}" logs --tail 5 --no-log-prefix cloudflared | grep -v -i token || true
echo
echo "Next cloudflared update check: $(systemctl show hoa-cloudflared-update.timer -p NextElapseUSecRealtime --value)"
