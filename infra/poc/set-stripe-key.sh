#!/usr/bin/env bash
# Optional since 0.44.0 (D93): the same keys can be entered in the browser on
# System status -> Keys and connections, which wins over .env; this script
# stays as the fallback.
# Put the HOA's Stripe key into the POC's .env and restart the portal
# (decision D32-payments). The key is typed at a hidden prompt: it is not
# shown, logged or put on any command line, and never belongs in the
# repository or a chat.
#
#   sudo bash /opt/hoa-portal/infra/poc/set-stripe-key.sh
#
# Only a RESTRICTED key (rk_test_... while testing, rk_live_... in
# production) created with only the rights in docs/runbook.md "Online
# payments (Stripe)" and no refund rights: the portal refuses a full secret
# key (sk_...) everywhere (D87). Test keys only on the POC: the portal refuses
# test keys in production and live keys anywhere without
# DEPLOYMENT_STAGE=production and PAYMENTS_LIVE_ALLOWED=true.
#
# The previous .env is copied to .env.before-stripe (mode 600) while the key
# is checked, and removed once the check passes.
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
if [[ "$KEY" == sk_* ]]; then
  unset KEY
  echo "That is a full secret key (sk_...). The portal accepts only a restricted key (rk_...) with the rights in docs/runbook.md \"Online payments (Stripe)\". Nothing was changed." >&2
  exit 1
fi
[[ "$KEY" =~ ^rk_(test|live)_[A-Za-z0-9]{10,}$ ]] || { unset KEY; echo "That is not a Stripe restricted key (rk_...). Nothing was changed." >&2; exit 1; }

# Only these keys change (env_update, infra/poc/lib.sh).
KEYS=(PAYMENTS_PROVIDER STRIPE_SECRET_KEY)
BACKUP="$(env_backup .env stripe)"
env_update .env PAYMENTS_PROVIDER stripe STRIPE_SECRET_KEY "$KEY"
unset KEY
echo "==> Restarting the portal"
bash "$APP_DIR/infra/poc/start-portal.sh" >/dev/null
echo "==> Checking the key with Stripe (read-only)"
COMPOSE=(docker compose --project-directory "$APP_DIR" -f docker-compose.yml -f docker-compose.tunnel.yml)
if ! "${COMPOSE[@]}" exec -T web python manage.py check_stripe --server; then
  env_restore_keys .env "$BACKUP" "${KEYS[@]}"
  env_discard_backup "$BACKUP"
  echo "The check failed (above). The previous Stripe settings are back in .env (nothing else in it was changed); restarting." >&2
  bash "$APP_DIR/infra/poc/start-portal.sh" >/dev/null || true
  exit 1
fi
env_discard_backup "$BACKUP"
echo "Removed .env.before-stripe (the copy of the previous settings, with the old secret)."
echo "Done. Switch on Online payments in Settings → Features (board), then open Finance -> Online payments."
echo "To change the key, run this script again."
