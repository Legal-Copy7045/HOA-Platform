#!/usr/bin/env bash
# Send a test email through the portal's own mail settings (.env), to check
# EMAIL_* credentials.
#
#   sudo bash /opt/hoa-portal/infra/poc/test-email.sh                 # to EMAIL_REDIRECT_ALL_TO
#   sudo bash /opt/hoa-portal/infra/poc/test-email.sh you@example.com # to a given address
#
# After editing .env, restart first (update-portal.sh --force): the portal
# reads .env only when it starts.
set -euo pipefail

SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
APP_DIR="$(cd "$(dirname "$SCRIPT_PATH")/../.." && pwd)"
COMPOSE=(docker compose --project-directory "$APP_DIR" -f docker-compose.yml -f docker-compose.tunnel.yml)

TO="${1:-}"
if [ -z "$TO" ]; then
  TO="$(grep -E '^EMAIL_REDIRECT_ALL_TO=' "$APP_DIR/.env" | tail -1 | cut -d= -f2- | tr -d '"'"'"' ')"
fi
if [ -z "$TO" ]; then
  echo "Give an address: sudo bash $SCRIPT_PATH you@example.com" >&2
  exit 2
fi

echo "Mail server: $(grep -E '^EMAIL_HOST=' "$APP_DIR/.env" | tail -1 | cut -d= -f2-)" \
     "port $(grep -E '^EMAIL_PORT=' "$APP_DIR/.env" | tail -1 | cut -d= -f2-)"
echo "Sending a test email to $TO ..."
if "${COMPOSE[@]}" exec -T web python manage.py sendtestemail "$TO"; then
  echo "Sent. Check the inbox (and spam). With EMAIL_REDIRECT_ALL_TO set, the subject starts with [to: ...]."
else
  cat >&2 <<'MSG'
Not sent. Read the last error line above:
  SMTPAuthenticationError  -> EMAIL_HOST_USER / EMAIL_HOST_PASSWORD are wrong
  timed out / refused      -> EMAIL_HOST or EMAIL_PORT wrong; try 465 + EMAIL_USE_SSL=true,
                              or 587 + EMAIL_USE_TLS=true (never both true)
  Name or service unknown  -> EMAIL_HOST is misspelled (it must not be "mailpit" here)
Then restart (update-portal.sh --force) and run this again.
MSG
  exit 1
fi
