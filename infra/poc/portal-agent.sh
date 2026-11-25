#!/usr/bin/env bash
# The VM's update agent (decision D25). Run every minute by the systemd timer
# hoa-portal-agent.timer (installed by start-portal.sh); safe to run by hand:
#
#   sudo bash /opt/hoa-portal/infra/poc/portal-agent.sh
#
# Every 15 minutes, or when "Check now" is pressed on System status, it asks
# GitHub whether the branch has newer commits and records the answer in the
# portal's database. When "Install update" is pressed, it runs
# update-portal.sh and records the outcome. The web app itself never gets
# any access to this machine: it can only write the request row, and this
# script decides what to do with it. Nothing typed on the website reaches
# a shell: values written back go through psql variables, never the command.
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "Run with sudo." >&2; exit 1; }
SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
APP_DIR="$(cd "$(dirname "$SCRIPT_PATH")/../.." && pwd)"
cd "$APP_DIR"
LOG=/var/log/hoa-portal-update.log
COMPOSE=(docker compose --project-directory "$APP_DIR" -f docker-compose.yml -f docker-compose.tunnel.yml)

exec 9>/run/hoa-portal-agent.lock
flock -n 9 || exit 0  # a previous run (e.g. an update) is still going

env_value() { grep -E "^$1=" "$APP_DIR/.env" | tail -1 | cut -d= -f2- | tr -d '"'"'"; }
DB_NAME="$(env_value HOA_DB_NAME)"; DB_NAME="${DB_NAME:-hoa}"
OWNER_USER="$(env_value HOA_OWNER_DB_USER)"
OWNER_PW="$(env_value HOA_OWNER_DB_PASSWORD)"

# SQL on stdin; values as psql variables (-v name=value, used as :'name').
psql_run() {
  PGPASSWORD="$OWNER_PW" "${COMPOSE[@]}" exec -T -e PGPASSWORD postgres \
    psql -h 127.0.0.1 -U "$OWNER_USER" -d "$DB_NAME" -tAq -v ON_ERROR_STOP=1 "$@"
}

# The portal may be mid-restart; try again next minute.
"${COMPOSE[@]}" exec -T postgres pg_isready -q 2>/dev/null || exit 0
psql_run <<'SQL' >/dev/null 2>&1 || exit 0
SELECT 1 FROM core_updatestatus LIMIT 1;
SQL

psql_run <<'SQL' >/dev/null
INSERT INTO core_updatestatus (id, branch, running_commit, latest_commit, behind, changes, state, message)
VALUES (1, '', '', '', 0, '', 'idle', '') ON CONFLICT (id) DO NOTHING;
UPDATE core_updatestatus SET agent_seen_at = now() WHERE id = 1;
SQL

REPO_OWNER="$(stat -c %U "$APP_DIR")"
g() { if [ "$REPO_OWNER" = "root" ]; then git -C "$APP_DIR" "$@"; else sudo -u "$REPO_OWNER" -H git -C "$APP_DIR" "$@"; fi; }
BRANCH="$(g rev-parse --abbrev-ref HEAD)"

check() {
  g fetch --quiet origin "$BRANCH" || return 0
  local running latest behind changes
  running="$(g rev-parse --short HEAD)"
  latest="$(g rev-parse --short "origin/$BRANCH")"
  behind="$(g rev-list --count "HEAD..origin/$BRANCH")"
  changes="$(g log --format='%s' "HEAD..origin/$BRANCH" | head -20 | cut -c1-150)"
  psql_run -v branch="$BRANCH" -v running="$running" -v latest="$latest" \
    -v behind="$behind" -v changes="$changes" >/dev/null <<'SQL'
UPDATE core_updatestatus
   SET branch = :'branch', running_commit = :'running', latest_commit = :'latest',
       behind = :'behind'::int, changes = :'changes', checked_at = now()
 WHERE id = 1;
SQL
}

read -r STATE DUE <<<"$(psql_run <<'SQL' | tr '|' ' '
SELECT state,
       (checked_at IS NULL
        OR checked_at < now() - interval '15 minutes'
        OR (check_requested_at IS NOT NULL AND check_requested_at > coalesce(checked_at, 'epoch')))
  FROM core_updatestatus WHERE id = 1;
SQL
)"

if [ "$STATE" = "requested" ]; then
  psql_run >/dev/null <<'SQL'
UPDATE core_updatestatus SET state = 'running', state_changed_at = now(), message = '' WHERE id = 1;
SQL
  {
    echo "=== $(date -u +%FT%TZ) update requested from System status"
  } >>"$LOG"
  if bash "$APP_DIR/infra/poc/update-portal.sh" >>"$LOG" 2>&1; then
    RESULT=succeeded; MESSAGE="Running $(g rev-parse --short HEAD)"
  else
    RESULT=failed; MESSAGE="See $LOG on the VM"
  fi
  # The database may still be starting after the update.
  for _ in 1 2 3 4 5 6; do "${COMPOSE[@]}" exec -T postgres pg_isready -q 2>/dev/null && break; sleep 5; done
  check
  psql_run -v result="$RESULT" -v message="$MESSAGE" >/dev/null <<'SQL'
UPDATE core_updatestatus SET state = :'result', state_changed_at = now(), message = :'message'
 WHERE id = 1;
SQL
elif [ "$DUE" = "t" ]; then
  check
fi
