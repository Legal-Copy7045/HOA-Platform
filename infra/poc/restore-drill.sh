#!/usr/bin/env bash
# Restore drill: load a backup into a separate, throwaway copy of the portal
# on this VM, bring it up to the current code, and check it. The live portal
# is not touched (separate project name, separate volumes, no network ports).
#
#   sudo bash /opt/hoa-portal/infra/poc/restore-drill.sh                       # newest backup
#   sudo bash /opt/hoa-portal/infra/poc/restore-drill.sh /var/backups/hoa-portal/<date>
#   sudo bash /opt/hoa-portal/infra/poc/restore-drill.sh --keep ...           # leave it running
#
# To restore for real onto a new server, follow docs/runbook.md ("Restoring");
# this drill proves the backups and the key work before you need them.
set -euo pipefail
trap 'echo "ERROR: restore-drill.sh stopped at line $LINENO (exit $?)." >&2' ERR

[ "$(id -u)" -eq 0 ] || { echo "Run with sudo." >&2; exit 1; }
KEEP=0
if [ "${1:-}" = "--keep" ]; then KEEP=1; shift; fi
SCRIPT_PATH="$(readlink -f "$0")"
APP_DIR="$(cd "$(dirname "$SCRIPT_PATH")/../.." && pwd)"
cd "$APP_DIR"
BACKUP_ROOT="${HOA_BACKUP_DIR:-/var/backups/hoa-portal}"
KEY_FILE="${HOA_BACKUP_KEY:-/root/hoa-backup.key}"
SRC="${1:-$(ls -1d "$BACKUP_ROOT"/*/ 2>/dev/null | sort | tail -1)}"
SRC="${SRC%/}"
[ -n "$SRC" ] && [ -f "$SRC/db.dump.enc" ] || { echo "No backup found (looked in $BACKUP_ROOT)." >&2; exit 1; }
[ -f "$KEY_FILE" ] || { echo "Backup key $KEY_FILE not found." >&2; exit 1; }

echo "==> Checking $SRC"
( cd "$SRC" && sha256sum -c SHA256SUMS )

env_value() { grep -E "^$1=" .env | tail -1 | cut -d= -f2-; }
DB_NAME="$(env_value HOA_DB_NAME)"; DB_NAME="${DB_NAME:-hoa}"
OWNER_USER="$(env_value HOA_OWNER_DB_USER)"
OWNER_PW="$(env_value HOA_OWNER_DB_PASSWORD)"
decrypt() { openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -pass "file:$KEY_FILE"; }

# A separate project: its own containers and volumes, no published ports.
SCRATCH=(docker compose --project-name hoa-restore-drill --project-directory "$APP_DIR" -f docker-compose.yml)
cleanup() {
  if [ "$KEEP" -eq 0 ]; then
    echo "==> Removing the drill copy"
    "${SCRATCH[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
  else
    echo "Drill copy left running (project hoa-restore-drill). Remove it with:"
    echo "  docker compose --project-name hoa-restore-drill -f docker-compose.yml down -v"
  fi
}
trap cleanup EXIT

echo "==> Starting an empty database and file store"
"${SCRATCH[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
"${SCRATCH[@]}" up -d postgres seaweedfs
for _ in $(seq 1 60); do
  "${SCRATCH[@]}" exec -T postgres pg_isready -q -h 127.0.0.1 && break
  sleep 2
done

echo "==> Restoring the database"
decrypt < "$SRC/db.dump.enc" | "${SCRATCH[@]}" exec -T -e PGPASSWORD="$OWNER_PW" postgres \
  pg_restore -h 127.0.0.1 -U "$OWNER_USER" -d "$DB_NAME" --no-owner --role="$OWNER_USER" --exit-on-error

echo "==> Bringing it up to the current code (migrations) and creating the bucket"
"${SCRATCH[@]}" run --rm -T migrate >/dev/null
"${SCRATCH[@]}" run --rm -T create-bucket >/dev/null

echo "==> Restoring uploaded files"
decrypt < "$SRC/files.tar.enc" | "${SCRATCH[@]}" run --rm -T --no-deps web python manage.py import_files --in -

echo "==> Checking the restored copy"
"${SCRATCH[@]}" run --rm -T --no-deps web python manage.py check_storage
"${SCRATCH[@]}" run --rm -T --no-deps web python manage.py data_status
echo
echo "Restore drill passed for $SRC (backup made at version $(cat "$SRC/APP_VERSION" 2>/dev/null || echo unknown))."
