#!/usr/bin/env bash
# Optional since 0.44.0 (D93): the same keys can be entered in the browser on
# System status -> Keys and connections, which wins over .env; this script
# stays as the fallback.
# Make the vendor document key on this server and put it into the POC's .env
# (decision D79), restart the portal and check it. The key encrypts vendors'
# W-9s and insurance certificates inside the portal (AES-256-GCM). It is
# generated here from the kernel's random source: nothing is typed, shown,
# logged or put on a command line, and it never belongs in the repository, a
# chat or the portal's web pages.
#
#   sudo bash /opt/hoa-portal/infra/poc/set-vendor-key.sh            # first time
#   sudo bash /opt/hoa-portal/infra/poc/set-vendor-key.sh --rotate   # new key
#
# --rotate keeps the old key in VENDOR_DOCUMENT_KEY_FALLBACKS, makes a new
# one and re-encrypts every stored vendor document under it.
#
# Keep a copy of the key APART from the backups (like DJANGO_SECRET_KEY, D68):
# the backups hold the encrypted documents only, so a backup without the key
# cannot be read, and a stolen backup alone reveals nothing. See
# docs/runbook.md "Vendor onboarding".
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "Run with sudo." >&2; exit 1; }
SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
APP_DIR="$(cd "$(dirname "$SCRIPT_PATH")/../.." && pwd)"
# shellcheck source=infra/poc/lib.sh
source "$APP_DIR/infra/poc/lib.sh"
cd "$APP_DIR"
COMPOSE=(docker compose --project-directory "$APP_DIR" -f docker-compose.yml -f docker-compose.tunnel.yml)

ROTATE=0
case "${1:-}" in
  "") ;;
  --rotate) ROTATE=1 ;;
  *) echo "Usage: set-vendor-key.sh [--rotate]" >&2; exit 2 ;;
esac

current="$(grep -E '^VENDOR_DOCUMENT_KEY=' .env | tail -1 | cut -d= -f2- || true)"
if [ -n "$current" ] && [ "$ROTATE" -eq 0 ]; then
  echo "A vendor document key is already set. Run with --rotate to replace it (the old one is kept as a fallback). Nothing was changed."
  unset current
  "${COMPOSE[@]}" exec -T web python manage.py check_vendor_key || true
  exit 0
fi

# 48 random bytes, URL-safe base64 (no characters .env or compose treat specially).
NEW_KEY="$(head -c 48 /dev/urandom | base64 -w0 | tr '+/' '-_' | tr -d '=')"
[ "${#NEW_KEY}" -ge 60 ] || { echo "Could not make a key. Nothing was changed." >&2; exit 1; }

# Only the two vendor keys change (env_update, infra/poc/lib.sh). The copy of
# the old .env is removed whatever happens (EXP-004): on --rotate the old key
# stays in VENDOR_DOCUMENT_KEY_FALLBACKS, so documents re-encrypted part-way
# remain readable, and the copy would only be a second plaintext of every
# server secret.
BACKUP="$(env_backup .env vendor-key)"
trap 'env_discard_backup "$BACKUP"' EXIT
if [ -n "$current" ]; then
  old_fallbacks="$(grep -E '^VENDOR_DOCUMENT_KEY_FALLBACKS=' .env | tail -1 | cut -d= -f2- || true)"
  env_update .env VENDOR_DOCUMENT_KEY_FALLBACKS "${current}${old_fallbacks:+,$old_fallbacks}" \
    VENDOR_DOCUMENT_KEY "$NEW_KEY"
  unset old_fallbacks
else
  env_update .env VENDOR_DOCUMENT_KEY "$NEW_KEY"
fi
unset NEW_KEY current
echo "==> Restarting the portal"
bash "$APP_DIR/infra/poc/start-portal.sh" >/dev/null
echo "==> Checking the key (a test value is encrypted and opened; nothing is shown)"
CHECK=(python manage.py check_vendor_key)
[ "$ROTATE" -eq 1 ] && CHECK+=(--reencrypt)
if ! "${COMPOSE[@]}" exec -T web "${CHECK[@]}"; then
  echo "The check failed (above). The new key is kept in .env (the old one, if any, is in VENDOR_DOCUMENT_KEY_FALLBACKS); run this script with --rotate again once the cause is fixed." >&2
  exit 1
fi
env_discard_backup "$BACKUP"
echo "Removed .env.before-vendor-key (the copy of the previous settings)."
echo "Done. Now keep a copy of the key apart from the backups (the HOA's password manager):"
echo "  sudo grep -E '^VENDOR_DOCUMENT_KEY' $APP_DIR/.env"
echo "Switch on Vendor onboarding in Settings → Features (board) (Settings -> Community setup -> Vendors)."
