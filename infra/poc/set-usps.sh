#!/usr/bin/env bash
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

cp -p .env .env.before-usps
set_env .env USPS_IV_ENVIRONMENT "$ENVIRONMENT"
set_env .env USPS_IV_USERNAME "$USERNAME"
set_env .env USPS_IV_PASSWORD "'$PASSWORD'"
unset PASSWORD
echo "==> Restarting the portal"
bash "$APP_DIR/infra/poc/start-portal.sh" >/dev/null
echo "==> Signing in to USPS (no mail is looked up)"
if ! "${COMPOSE[@]}" exec -T web python manage.py check_usps_iv; then
  echo "The check failed (above). To undo: cp .env.before-usps .env && sudo bash infra/poc/start-portal.sh" >&2
  exit 1
fi
echo "Done. Switch on USPS tracking in Admin -> Features; Settings -> Paper mail settings shows the connection."
echo "To undo: cp .env.before-usps .env && sudo bash infra/poc/start-portal.sh"
