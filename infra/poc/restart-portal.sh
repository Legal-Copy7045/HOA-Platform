#!/usr/bin/env bash
# Restarts the portal's web server and background worker (System status ->
# Restart the portal, decision D91). Run by the VM agent as the restart_app
# server task (infra/poc/host-tasks.sh); safe to run by hand:
#
#   sudo bash /opt/hoa-portal/infra/poc/restart-portal.sh
#
# Always with both compose files. The same containers and image are restarted
# (`docker compose restart`): nothing is rebuilt, migrated or pulled. Only a
# web container that lost the tunnel's edge network (recreated by hand with
# one compose file) is recreated from both files. Afterwards cloudflared is
# put back on the edge network only (security review E3/E9). Postgres, the
# file store, the scanner and the tunnel keep running.
set -euo pipefail
trap 'echo "ERROR: restart-portal.sh stopped at line $LINENO (exit $?)." >&2' ERR

[ "$(id -u)" -eq 0 ] || { echo "Run with sudo." >&2; exit 1; }
SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
APP_DIR="$(cd "$(dirname "$SCRIPT_PATH")/../.." && pwd)"
# shellcheck source=infra/poc/lib.sh
source "$APP_DIR/infra/poc/lib.sh"
cd "$APP_DIR"
COMPOSE=(docker compose --project-directory "$APP_DIR" -f docker-compose.yml -f docker-compose.tunnel.yml)
if [ -f .env ]; then ensure_compose_file .env; fi

web_id() { "${COMPOSE[@]}" ps -q web 2>/dev/null | head -1 | tr -cd '0-9a-f'; }
web_networks() {
  docker inspect -f '{{range $name, $_ := .NetworkSettings.Networks}}{{println $name}}{{end}}' \
    "$1" 2>/dev/null | network_names || true
}

echo "==> Restarting the web server and the background worker"
"${COMPOSE[@]}" restart web worker

WEB="$(web_id)"
[ -n "$WEB" ] || { echo "The web container is not running." >&2; exit 1; }
if ! web_networks "$WEB" | grep -q '_edge$'; then
  echo "==> The web server is not on the tunnel's edge network: recreating it from both compose files"
  "${COMPOSE[@]}" up -d --no-deps --force-recreate web
  WEB="$(web_id)"
fi

echo "==> Waiting for the web server to answer"
STATE=""
for _ in $(seq 1 36); do
  STATE="$(docker inspect -f '{{.State.Health.Status}}' "$WEB" 2>/dev/null | tr -cd 'a-z')" || true
  [ "$STATE" = "healthy" ] && break
  sleep 5
done

cloudflared_edge_only

if [ "$STATE" != "healthy" ]; then
  echo "Restarted, but the web server is not answering yet (${STATE:-unknown}); see docker compose logs web." >&2
  exit 1
fi
echo "Restarted the web server and the background worker; the site answers."
