#!/usr/bin/env bash
# Restore drill: load a backup into a separate, throwaway copy of the portal
# on this VM, bring it up to the current code, and check it. The live portal
# is not touched: separate project name, separate volumes, no network ports,
# and its own empty file store, never the live bucket (BUG-003, R2-002).
#
#   sudo bash /opt/hoa-portal/infra/poc/restore-drill.sh                       # newest backup
#   sudo bash /opt/hoa-portal/infra/poc/restore-drill.sh /var/backups/hoa-portal/<date>
#   sudo bash /opt/hoa-portal/infra/poc/restore-drill.sh --keep ...           # leave it running
#   sudo bash /opt/hoa-portal/infra/poc/restore-drill.sh --from-offsite         # newest off-site backup
#   sudo bash /opt/hoa-portal/infra/poc/restore-drill.sh --allow-unsigned <dir>
#       (only for a backup made before stamped keyed checksums; prints a warning)
#
# To restore for real onto a new server, follow docs/runbook.md ("Backups and
# the restore drill"); this drill proves the backups and the key work before
# you need them.
set -euo pipefail
trap 'echo "ERROR: restore-drill.sh stopped at line $LINENO (exit $?)." >&2' ERR

[ "$(id -u)" -eq 0 ] || { echo "Run with sudo." >&2; exit 1; }
KEEP=0
FROM_OFFSITE=0
VERIFY_ARGS=()
SRC=""
for arg in "$@"; do
  case "$arg" in
    --keep) KEEP=1 ;;
    --from-offsite) FROM_OFFSITE=1 ;;
    --allow-unsigned) VERIFY_ARGS=(--allow-unsigned) ;;
    -*) echo "Unknown option $arg." >&2; exit 1 ;;
    *) SRC="$arg" ;;
  esac
done
SCRIPT_PATH="$(readlink -f "$0")"
APP_DIR="$(cd "$(dirname "$SCRIPT_PATH")/../.." && pwd)"
# shellcheck source=infra/poc/lib.sh
source "$APP_DIR/infra/poc/lib.sh"
cd "$APP_DIR"
command -v python3 >/dev/null || { echo "python3 is needed (apt-get install python3)." >&2; exit 1; }
BACKUP_ROOT="${HOA_BACKUP_DIR:-/var/backups/hoa-portal}"
KEY_FILE="${HOA_BACKUP_KEY:-/root/hoa-backup.key}"
[ "$FROM_OFFSITE" -eq 1 ] && [ -n "$SRC" ] && { echo "Use either --from-offsite or a folder, not both." >&2; exit 1; }
if [ "$FROM_OFFSITE" -eq 1 ]; then
  # Download the newest complete off-site backup (still encrypted) with the
  # live portal's off-site settings; it is then checked like any other.
  LIVE=(docker compose --project-directory "$APP_DIR" -f docker-compose.yml -f docker-compose.tunnel.yml)
  # The newest backup not dated in the future (others are skipped with a warning).
  REMOTE="$("${LIVE[@]}" exec -T web python manage.py pull_backup --list \
    | offsite_pick "$(date -u +%Y-%m-%dT%H%MZ)")" || REMOTE=""
  [ -n "$REMOTE" ] || { echo "No complete backup found in the off-site bucket." >&2; exit 1; }
  SRC="$(mktemp -d /root/hoa-offsite-drill.XXXXXX)"; chmod 700 "$SRC"
  OFFSITE_DIR="$SRC"
  trap 'rm -rf "$OFFSITE_DIR"' EXIT
  echo "==> Downloading off-site backup $REMOTE"
  for f in db.dump.enc files.tar.enc SHA256SUMS HMAC-SHA256; do
    "${LIVE[@]}" exec -T web python manage.py pull_backup --name "$REMOTE-$f" > "$SRC/$f"
  done
  # The version label (older off-site backups don't have one).
  "${LIVE[@]}" exec -T web python manage.py pull_backup --name "$REMOTE-APP_VERSION" \
    > "$SRC/APP_VERSION" 2>/dev/null || rm -f "$SRC/APP_VERSION"
fi
if [ -z "$SRC" ]; then
  SRC="$(ls -1d "$BACKUP_ROOT"/*/ 2>/dev/null | sort | tail -1)"
fi
SRC="${SRC%/}"
[ -n "$SRC" ] && [ -f "$SRC/db.dump.enc" ] || { echo "No backup found (looked in $BACKUP_ROOT)." >&2; exit 1; }
[ -f "$KEY_FILE" ] || { echo "Backup key $KEY_FILE not found." >&2; exit 1; }

echo "==> Checking $SRC was made with this key, at the time it says, and not altered"
# The key is read from its file inside python, never put on a command line.
STAMP="$(python3 -I "$APP_DIR/infra/poc/backup_mac.py" verify "$SRC" "$KEY_FILE" "${VERIFY_ARGS[@]}")" \
  || { echo "Not restoring $SRC." >&2; exit 1; }
echo "    Backup made at: ${STAMP:-unknown (no stamped keyed checksum)}"
if [ "$FROM_OFFSITE" -eq 1 ]; then
  # The signed stamp must be the backup the object names pointed at (R2-002).
  offsite_stamp_ok "$REMOTE" "$STAMP" "$(date -u +%Y-%m-%dT%H%MZ)" \
    || { echo "Not restoring the off-site backup $REMOTE." >&2; exit 1; }
fi

env_value() { grep -E "^$1=" .env | tail -1 | cut -d= -f2-; }
DB_NAME="$(env_value HOA_DB_NAME)"; DB_NAME="${DB_NAME:-hoa}"
OWNER_USER="$(env_value HOA_OWNER_DB_USER)"
OWNER_PW="$(env_value HOA_OWNER_DB_PASSWORD)"
LIVE_ENDPOINT="$(env_value S3_ENDPOINT_URL)"; LIVE_ENDPOINT="${LIVE_ENDPOINT:-$DRILL_STORE_ENDPOINT}"
LIVE_BUCKET="$(env_value S3_BUCKET)"; LIVE_BUCKET="${LIVE_BUCKET:-hoa-private}"
decrypt() { openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -pass "file:$KEY_FILE"; }

# The drill's own file store. Shell variables win over .env in docker compose,
# so every drill container gets the scratch SeaweedFS with throwaway keys,
# whatever .env says (it may point at the live R2 bucket), and no off-site
# backup bucket.
export COMPOSE_PROFILES=local-store
export S3_ENDPOINT_URL="$DRILL_STORE_ENDPOINT" S3_BUCKET="$DRILL_STORE_BUCKET" S3_REGION=us-east-1
S3_ACCESS_KEY_ID="drill$(openssl rand -hex 8)"; export S3_ACCESS_KEY_ID
S3_SECRET_ACCESS_KEY="$(openssl rand -hex 24)"; export S3_SECRET_ACCESS_KEY
export BACKUP_S3_BUCKET="" BACKUP_S3_ENDPOINT_URL="" BACKUP_S3_ACCESS_KEY_ID="" BACKUP_S3_SECRET_ACCESS_KEY=""

# A separate project: its own containers and volumes, no published ports.
SCRATCH=(docker compose --project-name hoa-restore-drill --project-directory "$APP_DIR" -f docker-compose.yml)

echo "==> Checking the drill uses its own empty file store"
EFFECTIVE_ENDPOINT=""; EFFECTIVE_BUCKET=""
read -r EFFECTIVE_ENDPOINT EFFECTIVE_BUCKET < <("${SCRATCH[@]}" config --format json | python3 -I -c '
import json, sys
env = json.load(sys.stdin)["services"]["web"]["environment"]
print(env.get("S3_ENDPOINT_URL") or "-", env.get("S3_BUCKET") or "-")
') || true
scratch_store_ok "$EFFECTIVE_ENDPOINT" "$EFFECTIVE_BUCKET" "$LIVE_ENDPOINT" "$LIVE_BUCKET" \
  || { echo "Refusing to run the drill against that file store." >&2; exit 1; }

cleanup() {
  if [ -n "${OFFSITE_DIR:-}" ]; then rm -rf "$OFFSITE_DIR"; fi
  if [ "$KEEP" -eq 0 ]; then
    echo "==> Removing the drill copy"
    "${SCRATCH[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
  else
    echo "Drill copy left running (project hoa-restore-drill). Remove it with:"
    echo "  docker compose --project-name hoa-restore-drill -f docker-compose.yml --profile local-store down -v"
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
decrypt < "$SRC/db.dump.enc" | PGPASSWORD="$OWNER_PW" "${SCRATCH[@]}" exec -T -e PGPASSWORD postgres \
  pg_restore -h 127.0.0.1 -U "$OWNER_USER" -d "$DB_NAME" --no-owner --role="$OWNER_USER" --exit-on-error

echo "==> Bringing it up to the current code (migrations) and creating the bucket"
"${SCRATCH[@]}" run --rm -T migrate >/dev/null
"${SCRATCH[@]}" run --rm -T create-bucket >/dev/null

echo "==> Restoring uploaded files"
IN_ARCHIVE="$(decrypt < "$SRC/files.tar.enc" | tar -tvf - | { grep -c '^-' || true; })"
IMPORT_OUT="$(decrypt < "$SRC/files.tar.enc" | "${SCRATCH[@]}" run --rm -T --no-deps web python manage.py import_files --in -)"
IMPORTED="$(file_count Imported <<<"$IMPORT_OUT")" || { echo "import_files did not report a count." >&2; exit 1; }
echo "    $IMPORTED of $IN_ARCHIVE files in the backup restored"
# Into an empty store every file in the archive must be written.
counts_match "$IN_ARCHIVE" "$IMPORTED" "Restored files" || exit 1

echo "==> Checking the restored copy"
"${SCRATCH[@]}" run --rm -T --no-deps web python manage.py check_storage
"${SCRATCH[@]}" run --rm -T --no-deps web python manage.py data_status
echo
# The version label is not covered by the keyed checksum: show letters and
# digits only, so a tampered label cannot send anything to the terminal.
VERSION="$(head -c 40 "$SRC/APP_VERSION" 2>/dev/null | tr -cd 'a-z0-9' | cut -c1-12)"
echo "Restore drill passed for $SRC (made at ${STAMP:-unknown}, version ${VERSION:-unknown})."
