#!/usr/bin/env bash
# Optional since 0.44.0 (D93): the same keys can be entered in the browser on
# System status -> Keys and connections, which wins over .env; this script
# stays as the fallback.
# Put the HOA's USPS Business Customer Gateway login into the POC's .env so
# the portal can read Informed Visibility scans of barcoded paper mail
# (decision D60), restart the portal and check the login (sign-in only, no
# piece is looked up). The password is typed at a hidden prompt: it is not
# shown, logged or put on any command line, and never belongs in the
# repository, a chat or the portal's web pages.
#
#   sudo bash /opt/hoa-portal/infra/poc/set-usps.sh
#
# Before running: the BCG user needs IV-MTR access, given by the account's
# Business Service Administrator (BSA) or a BSA Delegate. Start with USPS's
# test environment (CAT; ask InformedVisibility@usps.gov for a CAT account),
# then run this again with "production". docs/runbook.md "USPS tracking".
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "Run with sudo." >&2; exit 1; }
SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
APP_DIR="$(cd "$(dirname "$SCRIPT_PATH")/../.." && pwd)"
# shellcheck source=infra/poc/lib.sh
source "$APP_DIR/infra/poc/lib.sh"
cd "$APP_DIR"
COMPOSE=(docker compose --project-directory "$APP_DIR" -f docker-compose.yml -f docker-compose.tunnel.yml)

read -r -p "USPS environment [cat] (cat = USPS test environment / production): " ENVIRONMENT
ENVIRONMENT="${ENVIRONMENT:-cat}"
[[ "$ENVIRONMENT" =~ ^(cat|production)$ ]] || { echo "Choose cat or production. Nothing was changed." >&2; exit 1; }

read -r -p "Business Customer Gateway user name: " USERNAME
[[ "$USERNAME" =~ ^[A-Za-z0-9._@+-]{3,100}$ ]] || { echo "That does not look like a BCG user name. Nothing was changed." >&2; exit 1; }

read -r -s -p "BCG password (hidden): " PASSWORD; echo
[ -n "$PASSWORD" ] || { echo "No password entered. Nothing was changed." >&2; exit 1; }
# Saved in single quotes so docker compose reads it literally ($ and # are
# safe); a single quote or a control character cannot be stored that way.
if [[ "$PASSWORD" == *"'"* || "$PASSWORD" =~ [[:cntrl:]] ]]; then
  unset PASSWORD
  echo "The password contains a single quote or a control character, which .env cannot hold. Change it at USPS first. Nothing was changed." >&2
  exit 1
fi

# Only these keys change (env_update, infra/poc/lib.sh).
KEYS=(USPS_IV_ENVIRONMENT USPS_IV_USERNAME USPS_IV_PASSWORD)
BACKUP="$(env_backup .env usps)"
env_update .env USPS_IV_ENVIRONMENT "$ENVIRONMENT" USPS_IV_USERNAME "$USERNAME" USPS_IV_PASSWORD "'$PASSWORD'"
unset PASSWORD
echo "==> Restarting the portal"
bash "$APP_DIR/infra/poc/start-portal.sh" >/dev/null
echo "==> Signing in to USPS (no mail is looked up)"
# Only the worker container has the password (D87): check from there.
if ! "${COMPOSE[@]}" exec -T worker python manage.py check_usps_iv; then
  env_restore_keys .env "$BACKUP" "${KEYS[@]}"
  env_discard_backup "$BACKUP"
  echo "The check failed (above). The previous USPS settings are back in .env (nothing else in it was changed); restarting." >&2
  bash "$APP_DIR/infra/poc/start-portal.sh" >/dev/null || true
  exit 1
fi
env_discard_backup "$BACKUP"
echo "Removed .env.before-usps (the copy of the previous settings, with the old secret)."
echo "Done. Switch on USPS tracking in Settings → Features (board); Settings -> Paper mail settings shows the connection."
echo "To change the login, run this script again."
