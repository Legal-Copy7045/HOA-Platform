#!/usr/bin/env bash
# Put the HOA's Stripe key into the POC's .env and restart the portal
# (decision D32-payments). The key is typed at a hidden prompt: it is not
# shown, logged or put on any command line, and never belongs in the
# repository or a chat.
#
#   sudo bash /opt/hoa-portal/infra/poc/set-stripe-key.sh
#
# Use a RESTRICTED key (rk_test_... while testing) created with only the
# rights in docs/runbook.md "Online payments (Stripe)". Test keys only on the
# POC: the portal refuses test keys in production and live keys anywhere
# without DEPLOYMENT_STAGE=production and PAYMENTS_LIVE_ALLOWED=true.
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "Run with sudo." >&2; exit 1; }
SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
APP_DIR="$(cd "$(dirname "$SCRIPT_PATH")/../.." && pwd)"
# shellcheck source=infra/poc/lib.sh
source "$APP_DIR/infra/poc/lib.sh"
cd "$APP_DIR"

read -r -s -p "Stripe restricted key (hidden): " KEY; echo
if [[ "$KEY" =~ ^(rk|sk)_live_ ]] && grep -q '^DEPLOYMENT_STAGE=poc' .env; then
  echo "This is the POC: use a test key (rk_test_...). Nothing was changed." >&2; exit 1
fi
[[ "$KEY" =~ ^(rk|sk)_(test|live)_[A-Za-z0-9]{10,}$ ]] || { echo "That is not a Stripe secret or restricted key. Nothing was changed." >&2; exit 1; }
[[ "$KEY" == sk_* ]] && echo "Note: this is a full secret key. A restricted key (rk_...) is safer; see the runbook."

cp -p .env .env.before-stripe
set_env .env PAYMENTS_PROVIDER stripe
set_env .env STRIPE_SECRET_KEY "$KEY"
unset KEY
echo "==> Restarting the portal"
bash "$APP_DIR/infra/poc/start-portal.sh" >/dev/null
echo "==> Checking the key with Stripe (read-only)"
COMPOSE=(docker compose --project-directory "$APP_DIR" -f docker-compose.yml -f docker-compose.tunnel.yml)
if ! "${COMPOSE[@]}" exec -T web python manage.py check_stripe; then
  echo "The check failed (above). To undo: cp .env.before-stripe .env && sudo bash infra/poc/start-portal.sh" >&2
  exit 1
fi
echo "Done. Switch on Online payments in Admin -> Features, then open Finance -> Online payments."
echo "To undo: cp .env.before-stripe .env && sudo bash infra/poc/start-portal.sh"
