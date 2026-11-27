#!/usr/bin/env bash
# Send the nightly encrypted backups to an off-site bucket that cannot delete
# them (Cloudflare R2 with a bucket lock). One run, about two minutes.
#
#   sudo bash /opt/hoa-portal/infra/poc/setup-offsite-backups.sh
#
# Before running, in the Cloudflare dashboard (R2):
#   1. Create a bucket (e.g. oak-creek-backups), Standard storage class,
#      public access OFF. Not the bucket that holds the portal's files.
#   2. Bucket -> Settings -> Bucket lock rules: prefix "backups/", retention
#      90 days. Nothing under backups/ can then be deleted or overwritten for
#      90 days, even with valid keys.
#   3. Bucket -> Settings -> Object lifecycle rules: delete "backups/" objects
#      after 365 days.
#   4. Manage R2 API tokens -> Create API token: "Object Read & Write", applied
#      to that bucket only. Note the Access Key ID, the Secret Access Key
#      (shown once) and your Account ID.
# The script asks for those values (the secret is not shown, logged or put on
# any command line), restarts the portal with them, proves the lock refuses a
# delete, and takes a backup that goes off-site.
set -euo pipefail
trap 'echo "ERROR: stopped at line $LINENO. Only the BACKUP_S3_ settings in .env may have changed." >&2' ERR

[ "$(id -u)" -eq 0 ] || { echo "Run with sudo." >&2; exit 1; }
SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
APP_DIR="$(cd "$(dirname "$SCRIPT_PATH")/../.." && pwd)"
# shellcheck source=infra/poc/lib.sh
source "$APP_DIR/infra/poc/lib.sh"
cd "$APP_DIR"
COMPOSE=(docker compose --project-directory "$APP_DIR" -f docker-compose.yml -f docker-compose.tunnel.yml)

read -r -p "Cloudflare Account ID: " ACCOUNT_ID
read -r -p "Backup bucket name: " BUCKET
read -r -p "R2 Access Key ID: " KEY_ID
read -r -s -p "R2 Secret Access Key (hidden): " SECRET; echo
[[ "$ACCOUNT_ID" =~ ^[0-9a-f]{32}$ ]] || { echo "The Account ID is 32 letters/digits (0-9, a-f)." >&2; exit 1; }
[[ "$BUCKET" =~ ^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$ ]] || { echo "That isn't a valid bucket name." >&2; exit 1; }
[ -n "$KEY_ID" ] && [ -n "$SECRET" ] || { echo "Both keys are needed." >&2; exit 1; }
FILES_BUCKET="$(grep -E '^S3_BUCKET=' .env | tail -1 | cut -d= -f2-)"
[ "$BUCKET" != "$FILES_BUCKET" ] || { echo "Use a separate bucket from the portal's files ($FILES_BUCKET)." >&2; exit 1; }

echo "==> Saving the off-site settings"
# env_update (infra/poc/lib.sh) changes only these keys and uses bash builtins
# only: the secret never reaches another program's command line. The copy of
# the old .env is removed when the script ends (it holds every old secret).
BACKUP="$(env_backup .env offsite)"
trap 'rm -f "${LOG:-}"; env_discard_backup "$BACKUP"' EXIT
env_update .env \
  BACKUP_S3_ENDPOINT_URL "https://${ACCOUNT_ID}.r2.cloudflarestorage.com" \
  BACKUP_S3_BUCKET "$BUCKET" \
  BACKUP_S3_ACCESS_KEY_ID "$KEY_ID" \
  BACKUP_S3_SECRET_ACCESS_KEY "$SECRET" \
  BACKUP_S3_REGION auto
unset SECRET

echo "==> Restarting the portal with them"
LOG="$(mktemp)"
bash "$APP_DIR/infra/poc/start-portal.sh" >"$LOG" 2>&1 || { tail -20 "$LOG" >&2; exit 1; }

echo "==> Checking the bucket refuses to delete backups"
if ! "${COMPOSE[@]}" exec -T web python manage.py check_offsite_lock; then
  echo >&2
  echo "Backups are NOT protected yet. Fix the bucket lock rule (step 2 above) and run" >&2
  echo "  sudo docker compose -f docker-compose.yml -f docker-compose.tunnel.yml exec web python manage.py check_offsite_lock" >&2
  echo "The settings are saved; nightly backups already go off-site." >&2
  exit 1
fi

echo "==> Taking a backup now (it goes off-site too)"
bash "$APP_DIR/infra/poc/backup.sh" --install

echo
echo "Done. Nightly backups now go to R2 bucket '$BUCKET', where they cannot be"
echo "deleted for the lock period. System status -> Health checks -> Backups shows"
echo "the last off-site copy. Prove it restores with:"
echo "  sudo bash $APP_DIR/infra/poc/restore-drill.sh --from-offsite"

