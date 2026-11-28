#!/usr/bin/env bash
# Optional since 0.44.0 (D93): the same keys can be entered in the browser on
# System status -> Keys and connections, which wins over .env; this script
# stays as the fallback.
# One-time set-up of the bank feed's Teller connection (decision D32-bankfeeds).
# Teller's certificate and private key identify the HOA to Teller, so they are
# secrets: they go into the server's .env here and never through the portal's
# web pages, a chat or the repository.
#
#   sudo bash /opt/hoa-portal/infra/poc/set-teller.sh
#
# Before running: in the HOA's Teller dashboard (teller.io, the HOA's shared
# account), note the Application ID and create a certificate; copy the two
# downloaded files (certificate.pem, private_key.pem) to /root/teller/ on this
# server. This script checks them, saves them, creates the key that encrypts
# Teller's access tokens if there is none, offers to delete the copied files
# and restarts the portal. Then: Settings → Features (board) -> Bank feed, and
# Finance -> Accounting -> Bank feed -> Connect a bank.
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "Run with sudo." >&2; exit 1; }
SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
APP_DIR="$(cd "$(dirname "$SCRIPT_PATH")/../.." && pwd)"
# shellcheck source=infra/poc/lib.sh
source "$APP_DIR/infra/poc/lib.sh"
cd "$APP_DIR"
COMPOSE=(docker compose --project-directory "$APP_DIR" -f docker-compose.yml -f docker-compose.tunnel.yml)

read -r -p "Teller Application ID (starts with app_): " APP_ID
[[ "$APP_ID" =~ ^app_[A-Za-z0-9]{6,}$ ]] || { echo "That does not look like a Teller Application ID. Nothing was changed." >&2; exit 1; }

read -r -p "Teller environment [development] (sandbox / development / production): " ENVIRONMENT
ENVIRONMENT="${ENVIRONMENT:-development}"
[[ "$ENVIRONMENT" =~ ^(sandbox|development|production)$ ]] || { echo "Choose sandbox, development or production. Nothing was changed." >&2; exit 1; }

CERT="" KEY=""
if [ "$ENVIRONMENT" != "sandbox" ]; then
  read -r -p "Certificate file [/root/teller/certificate.pem]: " CERT
  CERT="${CERT:-/root/teller/certificate.pem}"
  read -r -p "Private key file [/root/teller/private_key.pem]: " KEY
  KEY="${KEY:-/root/teller/private_key.pem}"
  [ -r "$CERT" ] && [ -r "$KEY" ] || { echo "Cannot read $CERT or $KEY. Nothing was changed." >&2; exit 1; }
  openssl x509 -in "$CERT" -noout 2>/dev/null || { echo "$CERT is not a certificate. Nothing was changed." >&2; exit 1; }
  openssl pkey -in "$KEY" -noout 2>/dev/null || { echo "$KEY is not a private key. Nothing was changed." >&2; exit 1; }
  if [ "$(openssl x509 -in "$CERT" -noout -pubkey | openssl sha256)" != \
       "$(openssl pkey -in "$KEY" -pubout | openssl sha256)" ]; then
    echo "The certificate and the private key do not belong together. Nothing was changed." >&2; exit 1
  fi
  if ! openssl x509 -in "$CERT" -noout -checkend 0 >/dev/null; then
    echo "The certificate has expired: create a new one in Teller's dashboard. Nothing was changed." >&2; exit 1
  fi
fi

# Only these keys change (env_update, infra/poc/lib.sh); a failed check puts
# back only these keys, never the whole old .env.
KEYS=(TELLER_APPLICATION_ID TELLER_ENVIRONMENT TELLER_CERTIFICATE_B64 TELLER_PRIVATE_KEY_B64 BANK_FEED_TOKEN_KEY)
BACKUP="$(env_backup .env teller)"
env_update .env TELLER_APPLICATION_ID "$APP_ID" TELLER_ENVIRONMENT "$ENVIRONMENT"
if [ -n "$CERT" ]; then
  env_update .env TELLER_CERTIFICATE_B64 "$(base64 -w0 "$CERT")" TELLER_PRIVATE_KEY_B64 "$(base64 -w0 "$KEY")"
fi
grep -q "^BANK_FEED_TOKEN_KEY=." .env || env_update .env BANK_FEED_TOKEN_KEY "$(openssl rand -hex 32)"

echo "==> Restarting the portal"
bash "$APP_DIR/infra/poc/start-portal.sh" >/dev/null
echo "==> Checking the set-up"
if ! "${COMPOSE[@]}" exec -T web python manage.py check_teller --server; then
  env_restore_keys .env "$BACKUP" "${KEYS[@]}"
  env_discard_backup "$BACKUP"
  echo "The check failed (above). The previous Teller settings are back in .env (nothing else in it was changed); restarting." >&2
  bash "$APP_DIR/infra/poc/start-portal.sh" >/dev/null || true
  exit 1
fi

if [ -n "$CERT" ]; then
  read -r -p "Delete the copied certificate and key files from this server now? (they are saved in .env) [Y/n]: " ANSWER
  if [[ ! "${ANSWER:-Y}" =~ ^[Nn] ]]; then
    shred -u "$CERT" "$KEY" && echo "Deleted."
  else
    echo "Kept. Delete them yourself: sudo shred -u $CERT $KEY"
  fi
fi
env_discard_backup "$BACKUP"
echo "Removed .env.before-teller (the copy of the previous settings, with the old secret)."
echo "Done. Switch on Bank feed in Settings → Features (board), then Finance -> Accounting -> Bank feed -> Connect a bank."
echo "Keep the certificate and key in the HOA's password manager; never send them by email or chat."
