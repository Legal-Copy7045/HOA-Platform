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
# Only for plain SMTP: with SMTP2Go as the relay (set-smtp2go.sh) bounces
# come from SMTP2Go's reports and no bounce mailbox is needed.
#
# Before running: a mailbox the association owns that receives bounces
# (ideally a dedicated address, e.g. bounces@<your domain>, with IMAP on and,
# where the provider needs it, an app password). docs/runbook.md "Bounced
# email".
#
# This script changes only the BOUNCE_* keys of .env (env_update in lib.sh),
# never EMAIL_* or anything another script set. An earlier version broke
# outgoing email in two ways, both fixed here: the optional "envelope sender"
# (BOUNCE_RETURN_PATH) changes the MAIL FROM of every outgoing email to
# bounces+<token>@..., which an authenticated SMTP relay refuses unless it
# allows that sender, and nothing then checked that email still went out;
# and the failed-check advice to copy .env.before-bounce-mailbox over .env put back
# the WHOLE old file, undoing any EMAIL_ change made since. Now a test email
# is sent after the change, and a failure puts back only this script's keys.
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "Run with sudo." >&2; exit 1; }
SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
APP_DIR="$(cd "$(dirname "$SCRIPT_PATH")/../.." && pwd)"
# shellcheck source=infra/poc/lib.sh
source "$APP_DIR/infra/poc/lib.sh"
cd "$APP_DIR"
COMPOSE=(docker compose --project-directory "$APP_DIR" -f docker-compose.yml -f docker-compose.tunnel.yml)

PROVIDER="$(env_get .env EMAIL_PROVIDER || true)"
PROVIDER="${PROVIDER//[\"\']/}"
if [ "${PROVIDER,,}" = "smtp2go" ]; then
  echo "Email goes through SMTP2Go (EMAIL_PROVIDER=smtp2go): SMTP2Go reports bounces to the portal and no bounce mailbox is needed."
  read -r -p "Set up a bounce mailbox anyway? [y/N]: " ANSWER
  [[ "${ANSWER:-N}" =~ ^[Yy] ]] || { echo "Nothing was changed."; exit 0; }
fi

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

RETURN_PATH=""
if [ "${PROVIDER,,}" != "smtp2go" ]; then
  echo "Optional: send outgoing mail with the mailbox's address as the envelope sender (VERP),"
  echo "so bounces reach it. Only if your SMTP server allows that sender for the portal's login"
  echo "(many refuse it, and then NO email goes out; a test email below checks it)."
  read -r -p "The mailbox's own address (Enter to skip): " RETURN_PATH
  if [ -n "$RETURN_PATH" ]; then
    [[ "$RETURN_PATH" =~ ^[A-Za-z0-9._-]{1,60}@[A-Za-z0-9.-]{3,190}$ ]] \
      || { echo "That does not look like a plain address (no + in it). Nothing was changed." >&2; exit 1; }
  fi
fi
read -r -p "Send a test email afterwards to (an address you can read; Enter for EMAIL_REDIRECT_ALL_TO): " TEST_TO
if [ -n "$TEST_TO" ]; then
  [[ "$TEST_TO" =~ ^[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{3,190}$ ]] \
    || { echo "That does not look like an email address. Nothing was changed." >&2; exit 1; }
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

KEYS=(BOUNCE_IMAP_HOST BOUNCE_IMAP_PORT BOUNCE_IMAP_USER BOUNCE_IMAP_FOLDER BOUNCE_IMAP_PASSWORD BOUNCE_RETURN_PATH)
BACKUP="$(env_backup .env bounce-mailbox)"
trap 'env_discard_backup "$BACKUP"' EXIT
env_update .env \
  BOUNCE_IMAP_HOST "$HOST" \
  BOUNCE_IMAP_PORT "$PORT" \
  BOUNCE_IMAP_USER "$USERNAME" \
  BOUNCE_IMAP_FOLDER "'$FOLDER'" \
  BOUNCE_IMAP_PASSWORD "'$PASSWORD'" \
  BOUNCE_RETURN_PATH "$RETURN_PATH"
unset PASSWORD

undo() {
  env_restore_keys .env "$BACKUP" "${KEYS[@]}"
  echo "$1 The previous bounce settings are back in .env (nothing else in it was changed); restarting." >&2
  bash "$APP_DIR/infra/poc/start-portal.sh" >/dev/null || true
  exit 1
}

echo "==> Restarting the portal"
bash "$APP_DIR/infra/poc/start-portal.sh" >/dev/null
echo "==> Signing in to the mailbox (no message is read)"
# Only the worker container has the password (EXP-003): check from there.
if ! "${COMPOSE[@]}" exec -T worker python manage.py check_bounce_mailbox; then
  undo "The check failed (above)."
fi
echo "==> Sending a test email (outgoing mail must still work)"
if ! bash "$APP_DIR/infra/poc/test-email.sh" ${TEST_TO:+"$TEST_TO"}; then
  if [ -n "$RETURN_PATH" ]; then
    env_restore_keys .env "$BACKUP" BOUNCE_RETURN_PATH
    bash "$APP_DIR/infra/poc/start-portal.sh" >/dev/null || true
    echo "Outgoing email failed with the envelope sender $RETURN_PATH, so it was switched off again" >&2
    echo "(BOUNCE_RETURN_PATH restored; the mailbox login is kept). Bounces then reach the From" >&2
    echo "address's mailbox instead. Run test-email.sh to confirm email works." >&2
    exit 1
  fi
  echo "The test email failed, but this script did not change any EMAIL_ setting: check them with test-email.sh." >&2
  exit 1
fi
env_discard_backup "$BACKUP"
echo "Removed .env.before-bounce-mailbox (the copy of the previous settings, with the old secret)."
echo "Done. Switch on Bounced email in Admin -> Features; System status shows the Bounce mailbox check."
echo "To change the mailbox, run this script again."
