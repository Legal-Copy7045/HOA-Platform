#!/usr/bin/env bash
# Nightly encrypted backup of the portal: the database and every uploaded file.
#
#   sudo bash /opt/hoa-portal/infra/poc/backup.sh             # back up now
#   sudo bash /opt/hoa-portal/infra/poc/backup.sh --install   # also install the nightly timer
#
# Writes to /var/backups/hoa-portal/<date>/, encrypted with the key in
# /root/hoa-backup.key (created on first run - COPY IT SOMEWHERE SAFE AND
# OFFLINE: without it the backups cannot be restored). Keeps 14 days locally.
# If BACKUP_S3_BUCKET is set in .env, the encrypted files are also copied
# off-site. Nothing is ever decrypted on the way out.
set -euo pipefail
trap 'echo "ERROR: backup.sh stopped at line $LINENO (exit $?)." >&2' ERR

[ "$(id -u)" -eq 0 ] || { echo "Run with sudo." >&2; exit 1; }
SCRIPT_PATH="$(readlink -f "$0")"
APP_DIR="$(cd "$(dirname "$SCRIPT_PATH")/../.." && pwd)"
cd "$APP_DIR"
[ -f .env ] || { echo "No .env in $APP_DIR." >&2; exit 1; }
command -v python3 >/dev/null || { echo "python3 is needed (apt-get install python3)." >&2; exit 1; }

BACKUP_ROOT="${HOA_BACKUP_DIR:-/var/backups/hoa-portal}"
KEY_FILE="${HOA_BACKUP_KEY:-/root/hoa-backup.key}"
KEEP_DAYS="${HOA_BACKUP_KEEP_DAYS:-14}"
COMPOSE=(docker compose --project-directory "$APP_DIR" -f docker-compose.yml -f docker-compose.tunnel.yml)

env_value() { grep -E "^$1=" .env | tail -1 | cut -d= -f2-; }

if [ "${1:-}" = "--install" ]; then
  cat > /etc/systemd/system/hoa-backup.service <<UNIT
[Unit]
Description=Nightly encrypted backup of the HOA portal
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
ExecStart=/usr/bin/bash $APP_DIR/infra/poc/backup.sh
UNIT
  cat > /etc/systemd/system/hoa-backup.timer <<'UNIT'
[Unit]
Description=Nightly HOA portal backup

[Timer]
OnCalendar=*-*-* 02:30:00
RandomizedDelaySec=20m
Persistent=true

[Install]
WantedBy=timers.target
UNIT
  systemctl daemon-reload
  systemctl enable --now hoa-backup.timer >/dev/null
  echo "Nightly backup installed (next run: $(systemctl show hoa-backup.timer -p NextElapseUSecRealtime --value))."
fi

if [ ! -f "$KEY_FILE" ]; then
  umask 077
  openssl rand -base64 48 > "$KEY_FILE"
  chmod 600 "$KEY_FILE"
  echo "Created the backup key $KEY_FILE."
  echo ">>> Copy it somewhere safe and offline now (password manager or printed)."
  echo ">>> Without it, these backups cannot be restored."
fi

STAMP="$(date -u +%Y-%m-%dT%H%MZ)"
DEST="$BACKUP_ROOT/$STAMP"
mkdir -p "$DEST"
chmod 700 "$BACKUP_ROOT" "$DEST"
encrypt() { openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -salt -pass "file:$KEY_FILE"; }

DB_NAME="$(env_value HOA_DB_NAME)"; DB_NAME="${DB_NAME:-hoa}"
OWNER_USER="$(env_value HOA_OWNER_DB_USER)"
OWNER_PW="$(env_value HOA_OWNER_DB_PASSWORD)"

echo "==> Database"
# The password goes through the environment, not the command line, so it
# never shows in the host's process list (SEC-008).
PGPASSWORD="$OWNER_PW" "${COMPOSE[@]}" exec -T -e PGPASSWORD postgres \
  pg_dump -h 127.0.0.1 -U "$OWNER_USER" -d "$DB_NAME" -Fc --no-owner \
  | encrypt > "$DEST/db.dump.enc"

echo "==> Uploaded files"
"${COMPOSE[@]}" exec -T web python manage.py export_files --out - 2>/dev/null \
  | encrypt > "$DEST/files.tar.enc"

( cd "$DEST" && sha256sum db.dump.enc files.tar.enc > SHA256SUMS )
# A keyed checksum over the stamp, the name and the bytes of each file, with a
# key derived from the backup key: someone who can write to the off-site
# bucket cannot swap in, rename or replay an older file without the key
# (SEC-008, R2-003). The key is read from its file inside python, never put
# on a command line (R2-004). Checked before every restore.
python3 -I "$APP_DIR/infra/poc/backup_mac.py" sign "$DEST" "$KEY_FILE" "$STAMP"
echo "$(git -c safe.directory="$APP_DIR" -C "$APP_DIR" rev-parse --short HEAD 2>/dev/null || echo unknown)" > "$DEST/APP_VERSION"
du -sh "$DEST" | awk '{print "    size: "$1}'
# System status and the health alerts read these (core/health.py "Backups").
"${COMPOSE[@]}" exec -T web python manage.py backup_heartbeat --kind local

if [ -n "$(env_value BACKUP_S3_BUCKET)" ]; then
  echo "==> Copying off-site"
  # APP_VERSION is a label only (which code made the backup), not covered
  # by the keyed checksum; the restore drill shows it sanitised.
  for f in db.dump.enc files.tar.enc SHA256SUMS HMAC-SHA256 APP_VERSION; do
    "${COMPOSE[@]}" exec -T web python manage.py push_backup --name "$STAMP-$f" < "$DEST/$f"
  done
  "${COMPOSE[@]}" exec -T web python manage.py backup_heartbeat --kind offsite
else
  echo "    (no off-site bucket set: BACKUP_S3_BUCKET in .env; local copy only)"
fi

echo "==> Removing local backups older than $KEEP_DAYS days"
find "$BACKUP_ROOT" -mindepth 1 -maxdepth 1 -type d -mtime "+$KEEP_DAYS" -print -exec rm -rf {} +

echo "Backup done: $DEST"
