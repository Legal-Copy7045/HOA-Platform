#!/usr/bin/env bash
# Builds and starts the portal behind the Cloudflare tunnel on the POC VM, and
# installs the daily cloudflared update timer.
#
#   cd /opt/hoa-portal && sudo bash infra/poc/start-portal.sh
#
# Safe to re-run (e.g. after `git pull`): it rebuilds and restarts what changed.
set -euo pipefail
trap 'echo "ERROR: start-portal.sh stopped at line $LINENO (exit $?)." >&2' ERR

[ "$(id -u)" -eq 0 ] || { echo "Run with sudo." >&2; exit 1; }
APP_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$APP_DIR"
[ -f .env ] || { echo "No .env yet: run infra/poc/make-env.sh first." >&2; exit 1; }
if [ "$(stat -c %a .env)" != "600" ]; then
  echo "Fixing .env permissions to 600"
  chmod 600 .env
fi

COMPOSE=(docker compose --project-directory "$APP_DIR" -f docker-compose.yml -f docker-compose.tunnel.yml)

echo "==> Building the portal image"
"${COMPOSE[@]}" build --pull
echo "==> Starting (migrations run first, as the owner role)"
"${COMPOSE[@]}" up -d --remove-orphans

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

echo "==> Waiting for the services"
sleep 20
"${COMPOSE[@]}" ps --format 'table {{.Service}}\t{{.Status}}'
echo
echo "Tunnel log (last lines; look for 'Registered tunnel connection'):"
"${COMPOSE[@]}" logs --tail 5 --no-log-prefix cloudflared | grep -v -i token || true
echo
echo "Next cloudflared update check: $(systemctl show hoa-cloudflared-update.timer -p NextElapseUSecRealtime --value)"
