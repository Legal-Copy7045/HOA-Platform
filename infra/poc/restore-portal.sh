#!/usr/bin/env bash
# Restore the live portal from one backup point (Backups -> Restore from a
# backup, decision D98). Run by the VM agent as the restore_backup server task
# (infra/poc/host-tasks.sh), which passes only a checked stamp; by hand:
#
#   sudo bash /opt/hoa-portal/infra/poc/restore-portal.sh --local 2026-11-26T0230Z
#   sudo bash /opt/hoa-portal/infra/poc/restore-portal.sh --offsite 2026-11-26T0230Z
#
# The stamp is backup.sh's folder name (YYYY-MM-DDTHHMMZ), never a path: it
# must name a backup that is really there (a folder under the backup root, or
# a complete set in the off-site bucket), and that backup's keyed checksum
# must say it was made then with this server's key (as the restore drill
# checks). Steps, each stopping the restore if it fails:
#   1. find the backup, copy it aside (a local one) or download it (off-site),
#      check its keyed checksum and that it decrypts;
#   2. refuse a backup made by a newer release than the installed code, or one
#      that does not say which release made it;
#   3. take a kept safety backup of the portal as it is now (backup.sh --keep):
#      nothing is changed if it fails;
#   4. stop web and worker; empty the database (empty-database.sql, table by
#      table); load the backup; migrate (brings an older backup up to this
#      release) and re-apply the runtime grants; end every session; mark the
#      tasks the backup was waiting on as not run; put its files back (files
#      already in the store are kept, never overwritten);
#   5. start web and worker (both compose files) and wait for the site to answer.
# On a failure after step 4 starts, the safety backup is put back the same way
# and the portal started again. Logins, passkeys and authenticator apps are
# the backup's (D98: the safest choice; see the restore page).
set -euo pipefail
trap 'echo "ERROR: restore-portal.sh stopped at line $LINENO (exit $?)." >&2' ERR

[ "$(id -u)" -eq 0 ] || { echo "Run with sudo." >&2; exit 1; }
SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
APP_DIR="$(cd "$(dirname "$SCRIPT_PATH")/../.." && pwd)"
# shellcheck source=infra/poc/lib.sh
source "$APP_DIR/infra/poc/lib.sh"
cd "$APP_DIR"
[ -f .env ] || { echo "No .env in $APP_DIR." >&2; exit 1; }
command -v python3 >/dev/null || { echo "python3 is needed (apt-get install python3)." >&2; exit 1; }

if [ "$#" -ne 2 ] || { [ "$1" != "--local" ] && [ "$1" != "--offsite" ]; }; then
  echo "Refusing: usage: restore-portal.sh --local|--offsite <YYYY-MM-DDTHHMMZ>." >&2
  exit 1
fi
WHERE="${1#--}"
STAMP="$2"
backup_stamp_ok "$STAMP" || { echo "Refusing: a backup point is a stamp like 2026-11-26T0230Z." >&2; exit 1; }

BACKUP_ROOT="${HOA_BACKUP_DIR:-/var/backups/hoa-portal}"
KEY_FILE="${HOA_BACKUP_KEY:-/root/hoa-backup.key}"
COMPOSE=(docker compose --project-directory "$APP_DIR" -f docker-compose.yml -f docker-compose.tunnel.yml)
env_value() { grep -E "^$1=" .env | tail -1 | cut -d= -f2- | tr -d '"'"'"; }
DB_NAME="$(env_value HOA_DB_NAME)"; DB_NAME="${DB_NAME:-hoa}"
OWNER_USER="$(env_value HOA_OWNER_DB_USER)"
OWNER_PW="$(env_value HOA_OWNER_DB_PASSWORD)"
APP_USER="$(env_value HOA_APP_DB_USER)"
[ -n "$OWNER_USER" ] && [ -n "$APP_USER" ] || { echo "Refusing: the database roles are not in .env." >&2; exit 1; }
[ -f "$KEY_FILE" ] || { echo "Refusing: the backup key $KEY_FILE is not on this server." >&2; exit 1; }

decrypt() { openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -pass "file:$KEY_FILE"; }
# SQL on stdin as the owner role; values as psql variables (-v name=value).
psql_owner() {
  PGPASSWORD="$OWNER_PW" "${COMPOSE[@]}" exec -T -e PGPASSWORD postgres \
    psql -h 127.0.0.1 -U "$OWNER_USER" -d "$DB_NAME" -tAq -v ON_ERROR_STOP=1 "$@"
}
# A management command with the owner role (the one-shot migrate service).
manage_owner() { "${COMPOSE[@]}" run --rm -T --no-deps migrate manage "$@"; }

WORK="$(mktemp -d /root/hoa-restore.XXXXXX)"
chmod 700 "$WORK"
SRC="$WORK/backup"
mkdir -m 700 "$SRC"
STOPPED=0
SAFETY=""

remove_work() {
  # Only our own mktemp folder, by its fixed prefix (encrypted copies inside).
  if [[ "$WORK" == /root/hoa-restore.* ]] && [ -d "$WORK" ]; then
    find "$WORK" -mindepth 1 -delete 2>/dev/null || true
    rmdir "$WORK" 2>/dev/null || true
  fi
}

# load_backup <folder>: empty the database and load the folder's backup into
# it, then migrate, grants, sessions, waiting tasks and files. Every step is
# checked here (|| return 1), so it also works from the EXIT trap.
load_backup() {
  local dir="$1" out in_archive imported
  echo "==> Emptying the database (table by table)"
  psql_owner -v app="$APP_USER" >/dev/null <"$APP_DIR/infra/poc/empty-database.sql" || return 1
  echo "==> Loading the database from the backup"
  decrypt <"$dir/db.dump.enc" | PGPASSWORD="$OWNER_PW" "${COMPOSE[@]}" exec -T -e PGPASSWORD postgres \
    pg_restore -h 127.0.0.1 -U "$OWNER_USER" -d "$DB_NAME" --no-owner --role="$OWNER_USER" \
    --exit-on-error || return 1
  echo "==> Bringing it up to the installed release (migrations) and the runtime grants"
  "${COMPOSE[@]}" run --rm -T --no-deps migrate >/dev/null || return 1
  manage_owner apply_db_grants >/dev/null || return 1
  echo "==> Ending every session; tasks the backup was waiting on are marked not run"
  psql_owner >/dev/null <<'SQL' || return 1
DELETE FROM django_session;
UPDATE core_hosttask
   SET state = 'failed', finished_at = now(), notified_at = now(),
       summary = 'Not run: it was waiting or running when this backup was taken (restored later).'
 WHERE state IN ('requested', 'running');
UPDATE core_updatestatus
   SET state = 'idle', scheduled_for = NULL, message = 'Reset by a restore from a backup.'
 WHERE state IN ('requested', 'running', 'scheduled');
SQL
  echo "==> Putting the backup's files back (files already in the store are kept)"
  in_archive="$(decrypt <"$dir/files.tar.enc" | tar -tvf - | { grep -c '^-' || true; })" || return 1
  out="$(decrypt <"$dir/files.tar.enc" \
    | "${COMPOSE[@]}" run --rm -T --no-deps web python manage.py import_files --in -)" || return 1
  imported="$(file_count Imported <<<"$out")" || { echo "ERROR: import_files did not report a count." >&2; return 1; }
  echo "    $in_archive files in the backup, $imported written (the rest were already there)"
  "${COMPOSE[@]}" run --rm -T --no-deps web python manage.py check_storage || return 1
  return 0
}

start_portal() {
  local web state=""
  "${COMPOSE[@]}" start web worker 2>&1 || return 1
  web="$("${COMPOSE[@]}" ps -q web 2>/dev/null | head -1 | tr -cd '0-9a-f')"
  [ -n "$web" ] || return 1
  echo "==> Waiting for the web server to answer"
  for _ in $(seq 1 36); do
    state="$(docker inspect -f '{{.State.Health.Status}}' "$web" 2>/dev/null | LC_ALL=C tr -cd '[:lower:]')" || true
    [ "$state" = "healthy" ] && break
    sleep 5
  done
  cloudflared_edge_only
  [ "$state" = "healthy" ]
}

finish() {
  local rc=$?
  trap - ERR
  if [ "$STOPPED" -eq 1 ]; then
    # After this marker only the rollback and restart lines follow: the
    # server task's summary takes the real error above it (D98).
    echo "==> Restarting after a failure (exit $rc); the error is above"
    if [ -n "$SAFETY" ] && load_backup "$BACKUP_ROOT/$SAFETY"; then
      echo "Put back: the safety backup $SAFETY (the portal as it was before the restore)."
    else
      echo "NOT put back: restore the safety backup ${SAFETY:-?} by hand (restore-portal.sh --local ${SAFETY:-<stamp>}); see docs/runbook.md."
    fi
    start_portal || echo "The portal did not answer after the restart; see docker compose logs web."
  fi
  remove_work
}
trap finish EXIT
trap 'exit 143' TERM INT

echo "==> Finding the backup $STAMP ($WHERE)"
if [ "$WHERE" = local ]; then
  DIR="$BACKUP_ROOT/$STAMP"
  if [ ! -d "$DIR" ] || [ -L "$DIR" ] || [ ! -f "$DIR/db.dump.enc" ] || [ ! -f "$DIR/files.tar.enc" ]; then
    echo "Refusing: there is no complete backup $STAMP in $BACKUP_ROOT." >&2
    exit 1
  fi
  # A copy, so the safety backup's clean-up of old backups cannot remove it.
  for f in db.dump.enc files.tar.enc SHA256SUMS HMAC-SHA256 APP_VERSION; do
    if [ -f "$DIR/$f" ]; then cp -- "$DIR/$f" "$SRC/$f"; fi
  done
else
  "${COMPOSE[@]}" exec -T web python manage.py pull_backup --list | LC_ALL=C tr -cd '0-9TZ\n-' \
    | grep -Fxq -- "$STAMP" \
    || { echo "Refusing: there is no complete backup $STAMP in the off-site bucket." >&2; exit 1; }
  echo "==> Downloading the off-site backup $STAMP"
  for f in db.dump.enc files.tar.enc SHA256SUMS HMAC-SHA256; do
    "${COMPOSE[@]}" exec -T web python manage.py pull_backup --name "$STAMP-$f" >"$SRC/$f"
  done
  "${COMPOSE[@]}" exec -T web python manage.py pull_backup --name "$STAMP-APP_VERSION" \
    >"$SRC/APP_VERSION" 2>/dev/null || rm -f "$SRC/APP_VERSION"
fi

echo "==> Checking it was made with this server's key, at the time it says, and not altered"
SIGNED="$(python3 -I "$APP_DIR/infra/poc/backup_mac.py" verify "$SRC" "$KEY_FILE")" \
  || { echo "Refusing: the backup $STAMP failed its keyed checksum." >&2; exit 1; }
offsite_stamp_ok "$STAMP" "$SIGNED" "$(date -u +%Y-%m-%dT%H%MZ)" \
  || { echo "Refusing: the backup $STAMP is not the one its keyed checksum names." >&2; exit 1; }

LABEL="$(head -c 64 "$SRC/APP_VERSION" 2>/dev/null | LC_ALL=C tr -cd 'a-z0-9.-' | cut -c1-48)" || LABEL=""
INSTALLED="$(LC_ALL=C tr -cd '0-9.' <"$APP_DIR/VERSION" | cut -c1-20)"
echo "    Made by release ${LABEL:-unknown}; installed release ${INSTALLED:-unknown}"
backup_release_ok "$LABEL" "$INSTALLED" || exit 1

echo "==> Checking it decrypts"
decrypt <"$SRC/db.dump.enc" | "${COMPOSE[@]}" exec -T postgres pg_restore -l >/dev/null \
  || { echo "Refusing: the database in the backup does not decrypt or read." >&2; exit 1; }
decrypt <"$SRC/files.tar.enc" | tar -tf - >/dev/null \
  || { echo "Refusing: the files in the backup do not decrypt or read." >&2; exit 1; }

# backup.sh names its folder by the minute: never the chosen backup's own.
while [ "$(date -u +%Y-%m-%dT%H%MZ)" = "$STAMP" ]; do sleep 5; done
echo "==> Taking a safety backup of the portal as it is now (kept)"
SAFETY_OUT="$(bash "$APP_DIR/infra/poc/backup.sh" --keep 2>&1)" \
  || { printf '%s\n' "$SAFETY_OUT" | tail -5; echo "Refusing: the safety backup failed: nothing was changed." >&2; exit 1; }
printf '%s\n' "$SAFETY_OUT" | sed 's/^/    /'
SAFETY="$(sed -n 's|^Backup done: .*/||p' <<<"$SAFETY_OUT" | tail -1)"
backup_stamp_ok "$SAFETY" && [ -f "$BACKUP_ROOT/$SAFETY/KEEP" ] \
  || { SAFETY=""; echo "Refusing: the safety backup could not be found: nothing was changed." >&2; exit 1; }

echo "==> Stopping the portal"
STOPPED=1
"${COMPOSE[@]}" stop web worker >/dev/null 2>&1

load_backup "$SRC" || { echo "ERROR: loading the backup $STAMP failed." >&2; exit 1; }

echo "==> Starting the portal"
STOPPED=0
start_portal || { echo "ERROR: the portal did not answer after the restore; see docker compose logs web." >&2; exit 1; }
"${COMPOSE[@]}" exec -T web python manage.py backup_heartbeat --kind local >/dev/null 2>&1 || true
echo "Restored the backup of $STAMP (release ${LABEL}, $([ "$WHERE" = local ] && echo "on the server" || echo "off-site copy")). Safety backup of the state before: $SAFETY (kept). Everyone signs in again."
