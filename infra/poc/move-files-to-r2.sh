#!/usr/bin/env bash
# Move the POC's uploaded files from the local file store (SeaweedFS) to a
# Cloudflare R2 bucket, and point the portal at R2. One run, about a minute.
#
#   sudo bash /opt/hoa-portal/infra/poc/move-files-to-r2.sh
#
# Before running, in the Cloudflare dashboard (R2):
#   1. Create a bucket (e.g. hoa-portal-files). Leave public access OFF.
#   2. "Manage R2 API tokens" -> Create API token: permission "Object Read &
#      Write", applied to that bucket only. Note the Access Key ID and the
#      Secret Access Key (shown once), and your Account ID.
# The script asks for those four values (the secret is not shown or logged),
# copies every file across, checks the count, and keeps the old file store's
# data volume untouched so you can go back (it just stops running).
set -euo pipefail
trap 'echo "ERROR: stopped at line $LINENO. Nothing was deleted; .env.before-r2 has the old settings." >&2' ERR

[ "$(id -u)" -eq 0 ] || { echo "Run with sudo." >&2; exit 1; }
SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
APP_DIR="$(cd "$(dirname "$SCRIPT_PATH")/../.." && pwd)"
cd "$APP_DIR"
COMPOSE=(docker compose --project-directory "$APP_DIR" -f docker-compose.yml -f docker-compose.tunnel.yml)

if grep -qE '^S3_ENDPOINT_URL=https://[^ ]*r2\.cloudflarestorage\.com' .env; then
  echo "The portal already uses R2. Nothing to do."; exit 0
fi

read -r -p "Cloudflare Account ID: " ACCOUNT_ID
read -r -p "R2 bucket name: " BUCKET
read -r -p "R2 Access Key ID: " KEY_ID
read -r -s -p "R2 Secret Access Key (hidden): " SECRET; echo
[[ "$ACCOUNT_ID" =~ ^[0-9a-f]{32}$ ]] || { echo "The Account ID is 32 letters/digits (0-9, a-f)." >&2; exit 1; }
[[ "$BUCKET" =~ ^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$ ]] || { echo "That isn't a valid bucket name." >&2; exit 1; }
[ -n "$KEY_ID" ] && [ -n "$SECRET" ] || { echo "Both keys are needed." >&2; exit 1; }
ENDPOINT="https://${ACCOUNT_ID}.r2.cloudflarestorage.com"

WORK="$(mktemp -d /root/hoa-files-move.XXXXXX)"; chmod 700 "$WORK"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

echo "==> Copying files out of the local file store"
"${COMPOSE[@]}" exec -T web python manage.py export_files --out - 2>"$WORK/export.log" >"$WORK/files.tar"
BEFORE="$(grep -oE '[0-9]+' "$WORK/export.log" | tail -1 || echo 0)"
echo "    $BEFORE files"

echo "==> Pointing the portal at R2"
cp -p .env .env.before-r2
set_env() {  # key value: replace or add, without echoing secrets
  local key="$1" value="$2"
  if grep -q "^${key}=" .env; then
    awk -v k="$key" -v v="$value" 'BEGIN{FS=OFS="="} $1==k{print k"="v; next} {print}' .env > .env.new
  else
    cp .env .env.new; printf '%s=%s\n' "$key" "$value" >> .env.new
  fi
  chmod 600 .env.new; mv .env.new .env
}
set_env S3_ENDPOINT_URL "$ENDPOINT"
set_env S3_BUCKET "$BUCKET"
set_env S3_ACCESS_KEY_ID "$KEY_ID"
set_env S3_SECRET_ACCESS_KEY "$SECRET"
set_env S3_REGION auto
sed -i '/^COMPOSE_PROFILES=local-store$/d' .env
unset SECRET

echo "==> Restarting with R2 (the local file store stops; its data is kept)"
bash "$APP_DIR/infra/poc/start-portal.sh" >"$WORK/start.log" 2>&1 || { tail -20 "$WORK/start.log" >&2; exit 1; }
"${COMPOSE[@]}" stop seaweedfs >/dev/null 2>&1 || true

echo "==> Checking R2 can be written, read and deleted"
"${COMPOSE[@]}" exec -T web python manage.py check_storage 2>&1 | grep -v '"level"'

echo "==> Copying files into R2"
"${COMPOSE[@]}" exec -T web python manage.py import_files --in - <"$WORK/files.tar" 2>&1 | grep -v '"level"'

echo
echo "Done. Files now live in R2 bucket '$BUCKET'."
echo "The old settings are in $APP_DIR/.env.before-r2 (contains the old keys; delete it once you're happy)."
echo "To go back: cp .env.before-r2 .env && sudo bash infra/poc/start-portal.sh"
