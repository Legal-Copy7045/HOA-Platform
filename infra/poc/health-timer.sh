#!/usr/bin/env bash
# Installs a 10-minute health check on the VM that emails the sysadmins when
# something fails - including when the background worker has stopped (the
# worker can't report its own death). Run once:
#
#   sudo bash /opt/hoa-portal/infra/poc/health-timer.sh
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Run with sudo." >&2; exit 1; }
SCRIPT_PATH="$(readlink -f "$0")"
APP_DIR="$(cd "$(dirname "$SCRIPT_PATH")/../.." && pwd)"
COMPOSE="/usr/bin/docker compose --project-directory $APP_DIR -f docker-compose.yml -f docker-compose.tunnel.yml"

cat > /etc/systemd/system/hoa-health.service <<UNIT
[Unit]
Description=HOA portal health check (emails sysadmins on problems)
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
WorkingDirectory=$APP_DIR
# If the web container is down, the check can't run there: start a one-off
# container to report it instead.
ExecStart=/usr/bin/bash -c '$COMPOSE exec -T web python manage.py health_check --alert || $COMPOSE run --rm -T --no-deps web python manage.py health_check --alert || true'
UNIT
cat > /etc/systemd/system/hoa-health.timer <<'UNIT'
[Unit]
Description=HOA portal health check every 10 minutes

[Timer]
OnBootSec=5min
OnUnitActiveSec=10min

[Install]
WantedBy=timers.target
UNIT
systemctl daemon-reload
systemctl enable --now hoa-health.timer >/dev/null
echo "Health check installed: every 10 minutes; sysadmins are emailed about problems."
