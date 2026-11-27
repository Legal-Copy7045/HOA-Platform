#!/usr/bin/env bash
# Send the portal's email through SMTP2Go's HTTP API (owner's decision, 0.43.0)
# and read delivery and bounce reports back from SMTP2Go (pull only: no
# webhook, no bounce mailbox). Asks for the API key at a hidden prompt (not
# shown, logged or put on any command line; never in the repository, a chat
# or the portal's web pages), checks it with a harmless API call that sends
# nothing, sets the sender, changes only its own keys in .env, restarts web
# and worker and sends a test email.
#
#   sudo bash /opt/hoa-portal/infra/poc/set-smtp2go.sh
#
# Before running (SMTP2Go dashboard, see infra/poc/README.md "SMTP2Go"):
#   1. Sending -> Verified Senders -> add the sender domain and publish the
#      DNS records it shows (SPF include, DKIM CNAME, return-path CNAME).
#   2. Sending -> API Keys -> Add API Key, with the permissions "Emails"
#      (send), "Activity" (search) and "Statistics" (the key check) only.
#
# Changes only EMAIL_PROVIDER, SMTP2GO_API_KEY, SMTP2GO_REGION and
# DEFAULT_FROM_EMAIL (env_update, infra/poc/lib.sh). The EMAIL_HOST settings
# are kept, so going back to plain SMTP is: set EMAIL_PROVIDER=smtp with
#   sudo bash infra/poc/set-smtp2go.sh --off
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "Run with sudo." >&2; exit 1; }
SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
APP_DIR="$(cd "$(dirname "$SCRIPT_PATH")/../.." && pwd)"
# shellcheck source=infra/poc/lib.sh
source "$APP_DIR/infra/poc/lib.sh"
cd "$APP_DIR"
[ -f .env ] || { echo "No .env in $APP_DIR." >&2; exit 1; }
COMPOSE=(docker compose --project-directory "$APP_DIR" -f docker-compose.yml -f docker-compose.tunnel.yml)
KEYS=(EMAIL_PROVIDER SMTP2GO_API_KEY SMTP2GO_REGION DEFAULT_FROM_EMAIL)

restart() {
  "${COMPOSE[@]}" up -d --wait --wait-timeout 180 web worker >/dev/null
}

if [ "${1:-}" = "--off" ]; then
  BACKUP="$(env_backup .env smtp2go)"
  trap 'env_discard_backup "$BACKUP"' EXIT
  env_update .env EMAIL_PROVIDER smtp SMTP2GO_API_KEY ""
  echo "==> Restarting web and worker"
  restart
  echo "Email goes through the SMTP server in the EMAIL_ settings again (the SMTP2Go key was removed)."
  exit 0
fi
[ -z "${1:-}" ] || { echo "Usage: set-smtp2go.sh [--off]" >&2; exit 2; }

read -r -p "SMTP2Go region [global] (global, us, eu or au; the API host your account uses): " REGION
REGION="${REGION:-global}"
REGION="${REGION,,}"
case "$REGION" in
  global) API_HOST=api.smtp2go.com ;;
  us) API_HOST=us-api.smtp2go.com ;;
  eu) API_HOST=eu-api.smtp2go.com ;;
  au) API_HOST=au-api.smtp2go.com ;;
  *) echo "Choose global, us, eu or au. Nothing was changed." >&2; exit 1 ;;
esac

CURRENT_FROM="$(env_get .env DEFAULT_FROM_EMAIL || true)"
CURRENT_ADDR="$(printf '%s' "$CURRENT_FROM" | sed -n 's/.*<\([^>]*\)>.*/\1/p')"
read -r -p "Sender address, on the domain verified in SMTP2Go [${CURRENT_ADDR:-portal@example.org}]: " SENDER
SENDER="${SENDER:-$CURRENT_ADDR}"
[[ "$SENDER" =~ ^[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$ ]] \
  || { echo "That does not look like an email address. Nothing was changed." >&2; exit 1; }
read -r -p "Sender name [Oak Creek Farms HOA]: " SENDER_NAME
SENDER_NAME="${SENDER_NAME:-Oak Creek Farms HOA}"
[[ "$SENDER_NAME" =~ ^[A-Za-z0-9\ .,\&()-]{1,80}$ ]] \
  || { echo "Use letters, digits, spaces and . , & ( ) - in the name. Nothing was changed." >&2; exit 1; }
FROM="${SENDER_NAME} <${SENDER}>"
echo "Outgoing mail will come from: $FROM (domain ${SENDER#*@})"

read -r -p "Send the test email to (an address you can read; Enter for EMAIL_REDIRECT_ALL_TO): " TEST_TO
if [ -n "$TEST_TO" ]; then
  [[ "$TEST_TO" =~ ^[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{3,190}$ ]] \
    || { echo "That does not look like an email address. Nothing was changed." >&2; exit 1; }
fi

read -r -s -p "SMTP2Go API key (hidden, starts with api-): " KEY; echo
[[ "$KEY" =~ ^api-[A-Za-z0-9]{16,64}$ ]] \
  || { unset KEY; echo "That is not an SMTP2Go API key (api- and letters/digits). Nothing was changed." >&2; exit 1; }

echo "==> Checking the key with SMTP2Go (a usage summary; no email is sent)"
# The key reaches curl on its standard input (a config file read from "-"),
# never on a command line; printf is a bash builtin.
ANSWER_FILE="$(mktemp)"; chmod 600 "$ANSWER_FILE"
CODE="$(printf 'header = "X-Smtp2go-Api-Key: %s"\n' "$KEY" | curl -sS --proto '=https' --max-redirs 0 \
  --max-time 20 --config - -o "$ANSWER_FILE" -w '%{http_code}' \
  -H 'Content-Type: application/json' -H 'Accept: application/json' \
  --data '{}' "https://${API_HOST}/v3/stats/email_summary" || echo 000)"
rm -f "$ANSWER_FILE"
case "$CODE" in
  200) echo "    The key works." ;;
  401|403) unset KEY; echo "SMTP2Go refused the key (HTTP $CODE): check it, its region and that it has the Statistics permission. Nothing was changed." >&2; exit 1 ;;
  000) unset KEY; echo "SMTP2Go could not be reached ($API_HOST). Nothing was changed." >&2; exit 1 ;;
  *) unset KEY; echo "SMTP2Go answered HTTP $CODE. Nothing was changed." >&2; exit 1 ;;
esac

BACKUP="$(env_backup .env smtp2go)"
trap 'env_discard_backup "$BACKUP"' EXIT
env_update .env EMAIL_PROVIDER smtp2go SMTP2GO_API_KEY "$KEY" SMTP2GO_REGION "$REGION" DEFAULT_FROM_EMAIL "$FROM"
unset KEY

undo() {
  env_restore_keys .env "$BACKUP" "${KEYS[@]}"
  echo "$1 The previous email settings are back in .env (nothing else in it was changed); restarting." >&2
  restart || true
  exit 1
}

echo "==> Restarting web and worker"
restart || undo "The portal did not come back up."
echo "==> Checking the key from the portal"
"${COMPOSE[@]}" exec -T worker python manage.py check_smtp2go || undo "The portal's check failed (above)."
echo "==> Sending a test email"
bash "$APP_DIR/infra/poc/test-email.sh" ${TEST_TO:+"$TEST_TO"} || undo "The test email was not accepted."
env_discard_backup "$BACKUP"
echo "Removed .env.before-smtp2go (the copy of the previous settings)."
echo
echo "Done. Email now goes through SMTP2Go; System status -> Email shows the last send and the"
echo "last read of SMTP2Go's delivery reports (every 5 minutes). Switch on Bounced email in"
echo "Admin -> Features so bouncing addresses are flagged. No bounce mailbox is needed."
echo "DNS: SPF, DKIM and return-path records for ${SENDER#*@} must be verified in SMTP2Go"
echo "(Sending -> Verified Senders), or mail lands in spam. To change the key, run this again."
