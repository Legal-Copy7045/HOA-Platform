#!/usr/bin/env bash
# Put the login of the association's bounce mailbox into the POC's .env so
# the portal can read bounced email over IMAP over TLS (decision D74),
# restart the portal and check the login (sign in and open the folder only;
# no message is read). The password is typed at a hidden prompt: it is not
# shown, logged or put on any command line, and never belongs in the
# repository, a chat or the portal's web pages.
#
#   sudo bash /opt/hoa-portal/infra/poc/set-bounce-mailbox.sh
#
# Before running: a mailbox the association owns that receives bounces
# (ideally a dedicated address, e.g. bounces@<your domain>, with IMAP on and,
# where the provider needs it, an app password). docs/runbook.md "Bounced
# email".
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "Run with sudo." >&2; exit 1; }
SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
APP_DIR="$(cd "$(dirname "$SCRIPT_PATH")/../.." && pwd)"
# shellcheck source=infra/poc/lib.sh
source "$APP_DIR/infra/poc/lib.sh"
cd "$APP_DIR"
COMPOSE=(docker compose --project-directory "$APP_DIR" -f docker-compose.yml -f docker-compose.tunnel.yml)

read -r -p "IMAP server host name (e.g. imap.example.org): " HOST
HOST="$(printf '%s' "$HOST" | tr 'A-Z' 'a-z')"
[[ "$HOST" =~ ^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$ ]] \
  || { echo "That does not look like a host name. Nothing was changed." >&2; exit 1; }

read -r -p "IMAP port [993] (IMAP over TLS): " PORT
PORT="${PORT:-993}"
[[ "$PORT" =~ ^[0-9]{1,5}$ ]] && [ "$PORT" -ge 1 ] && [ "$PORT" -le 65535 ] \
  || { echo "That is not a port number. Nothing was changed." >&2; exit 1; }

read -r -p "Mailbox user name: " USERNAME
[[ "$USERNAME" =~ ^[A-Za-z0-9._@+-]{1,200}$ ]] \
  || { echo "That does not look like a mailbox user name. Nothing was changed." >&2; exit 1; }

read -r -p "Folder to read [INBOX]: " FOLDER
FOLDER="${FOLDER:-INBOX}"
[[ "$FOLDER" =~ ^[A-Za-z0-9._/\ -]{1,100}$ ]] \
  || { echo "Use letters, digits, spaces, dots, dashes or slashes for the folder. Nothing was changed." >&2; exit 1; }

read -r -p "The mailbox's own address, used as the envelope sender of outgoing mail (optional, Enter to skip): " RETURN_PATH
if [ -n "$RETURN_PATH" ]; then
  [[ "$RETURN_PATH" =~ ^[A-Za-z0-9._-]{1,60}@[A-Za-z0-9.-]{3,190}$ ]] \
    || { echo "That does not look like a plain address (no + in it). Nothing was changed." >&2; exit 1; }
fi

read -r -s -p "Mailbox password or app password (hidden): " PASSWORD; echo
[ -n "$PASSWORD" ] || { echo "No password entered. Nothing was changed." >&2; exit 1; }
# Saved in single quotes so docker compose reads it literally ($ and # are
# safe); a single quote or a control character cannot be stored that way.
if [[ "$PASSWORD" == *"'"* || "$PASSWORD" =~ [[:cntrl:]] ]]; then
  unset PASSWORD
  echo "The password contains a single quote or a control character, which .env cannot hold. Use an app password without them. Nothing was changed." >&2
  exit 1
fi

cp -p .env .env.before-bounce-mailbox
set_env .env BOUNCE_IMAP_HOST "$HOST"
set_env .env BOUNCE_IMAP_PORT "$PORT"
set_env .env BOUNCE_IMAP_USER "$USERNAME"
set_env .env BOUNCE_IMAP_FOLDER "'$FOLDER'"
set_env .env BOUNCE_IMAP_PASSWORD "'$PASSWORD'"
unset PASSWORD
set_env .env BOUNCE_RETURN_PATH "$RETURN_PATH"
echo "==> Restarting the portal"
bash "$APP_DIR/infra/poc/start-portal.sh" >/dev/null
echo "==> Signing in to the mailbox (no message is read)"
if ! "${COMPOSE[@]}" exec -T web python manage.py check_bounce_mailbox; then
  echo "The check failed (above). To undo: cp .env.before-bounce-mailbox .env && sudo bash infra/poc/start-portal.sh" >&2
  exit 1
fi
echo "Done. Switch on Bounced email in Admin -> Features; System status shows the Bounce mailbox check."
echo "To undo: cp .env.before-bounce-mailbox .env && sudo bash infra/poc/start-portal.sh"
