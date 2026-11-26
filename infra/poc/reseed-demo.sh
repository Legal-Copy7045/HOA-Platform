#!/usr/bin/env bash
# Replace everything in the POC portal with a fictional demo dataset (D53).
#
# Started from the portal (Settings -> Demo data, a sysadmin types RESEED):
# the web app only inserts a "reseed_demo" server task; the VM agent
# (portal-agent.sh, host-tasks.sh) checks the dataset name and runs this
# script with it. By hand, on the VM:
#
#   sudo bash /opt/hoa-portal/infra/poc/reseed-demo.sh fresh_start
#   sudo bash /opt/hoa-portal/infra/poc/reseed-demo.sh fresh_start --attach-requester <task id>
#
# --attach-requester (passed by the agent when the box on Demo data was
# ticked): after seeding, the person who asked gets a board seat and a demo
# home in the new data (demo_attach_requester; POC only).
#
# Steps, each stopping the reseed if it fails:
#   1. refuse unless .env says DEPLOYMENT_STAGE=poc and SEED_DEMO_ALLOWED=true,
#      and the dataset is one this portal's seed_demo lists;
#   2. take a backup (backup.sh): nothing is changed if it fails;
#   3. save the real (non-demo) logins with their passkeys, app codes,
#      recovery codes, person and sysadmin role (keep_logins export, owner
#      role) into a root-only temporary file, shredded on exit;
#   4. stop web and worker; empty the database (drop and recreate the public
#      schema as the owner role: tables, append-only triggers and all);
#   5. migrate and re-apply the runtime grants;
#   6. put the real logins back (same ids: a passkey names its login by id),
#      seed the dataset, remove the old data's files from the file store;
#   7. start web and worker (also on any failure, so the site comes back).
# A failure after step 4 leaves a partly empty portal: restore the backup
# from step 2 (docs/runbook.md, "Reseed the POC with demo data").
set -euo pipefail
trap 'echo "ERROR: reseed-demo.sh stopped at line $LINENO (exit $?)." >&2' ERR

[ "$(id -u)" -eq 0 ] || { echo "Run with sudo." >&2; exit 1; }
SCRIPT_PATH="$(readlink -f "$0")"
APP_DIR="$(cd "$(dirname "$SCRIPT_PATH")/../.." && pwd)"
# shellcheck source=infra/poc/lib.sh
source "$APP_DIR/infra/poc/lib.sh"
cd "$APP_DIR"
[ -f .env ] || { echo "No .env in $APP_DIR." >&2; exit 1; }

DATASET="${1:-}"
demo_dataset_ok "$DATASET" \
  || { echo "Refusing: a dataset name is lower-case letters and _ only." >&2; exit 1; }
ATTACH_TASK=""
if [ "$#" -gt 1 ]; then
  [ "$#" -eq 3 ] && [ "$2" = "--attach-requester" ] && [[ "$3" =~ ^[0-9]{1,18}$ ]] \
    || { echo "Refusing: usage: reseed-demo.sh <dataset> [--attach-requester <task id>]." >&2; exit 1; }
  ATTACH_TASK="$3"
fi

env_value() { grep -E "^$1=" .env | tail -1 | cut -d= -f2- | tr -d '"'"'"; }
reseed_allowed "$(env_value DEPLOYMENT_STAGE)" "$(env_value SEED_DEMO_ALLOWED)" || exit 1

COMPOSE=(docker compose --project-directory "$APP_DIR" -f docker-compose.yml -f docker-compose.tunnel.yml)
DB_NAME="$(env_value HOA_DB_NAME)"; DB_NAME="${DB_NAME:-hoa}"
OWNER_USER="$(env_value HOA_OWNER_DB_USER)"
OWNER_PW="$(env_value HOA_OWNER_DB_PASSWORD)"
APP_USER="$(env_value HOA_APP_DB_USER)"
[ -n "$OWNER_USER" ] && [ -n "$APP_USER" ] || { echo "The database roles are not in .env." >&2; exit 1; }

# A management command with the owner role (the one-shot migrate service's
# credentials; web and worker never get them).
manage_owner() { "${COMPOSE[@]}" run --rm -T --no-deps migrate manage "$@"; }
# SQL on stdin as the owner role; values as psql variables (-v name=value).
psql_owner() {
  PGPASSWORD="$OWNER_PW" "${COMPOSE[@]}" exec -T -e PGPASSWORD postgres \
    psql -h 127.0.0.1 -U "$OWNER_USER" -d "$DB_NAME" -tAq -v ON_ERROR_STOP=1 "$@"
}

echo "==> Checking the dataset"
NAMES="$("${COMPOSE[@]}" exec -T web python manage.py seed_demo --list-datasets)"
grep -Fxq -- "$DATASET" <<<"$NAMES" \
  || { echo "Refusing: $DATASET is not a dataset this portal has." >&2; exit 1; }

echo "==> Taking a backup first"
bash "$APP_DIR/infra/poc/backup.sh" \
  || { echo "The backup failed: nothing was changed." >&2; exit 1; }

KEEP_DIR="$(mktemp -d /root/hoa-keep-logins.XXXXXX)"
chmod 700 "$KEEP_DIR"
KEEP="$KEEP_DIR/logins.json"
STOPPED=0
finish() {
  # The saved logins hold password hashes and MFA secrets: never left behind.
  if [ -f "$KEEP" ]; then shred -u "$KEEP" 2>/dev/null || rm -f "$KEEP"; fi
  rm -rf "$KEEP_DIR"
  if [ "$STOPPED" -eq 1 ]; then
    echo "==> Starting the portal"
    "${COMPOSE[@]}" start web worker >/dev/null || true
  fi
}
trap finish EXIT
trap 'exit 143' TERM INT

echo "==> Saving the real logins"
( umask 077; manage_owner keep_logins export --out - >"$KEEP" )
python3 -I -c 'import json, sys; json.load(open(sys.argv[1]))' "$KEEP" \
  || { echo "The saved logins are not readable: nothing was changed." >&2; exit 1; }

echo "==> Stopping the portal"
STOPPED=1
"${COMPOSE[@]}" stop web worker >/dev/null

echo "==> Emptying the database"
psql_owner -v app="$APP_USER" >/dev/null <<'SQL'
DROP SCHEMA public CASCADE;
CREATE SCHEMA public;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO :"app";
SQL

echo "==> Creating the tables (migrations) and the runtime grants"
"${COMPOSE[@]}" run --rm -T --no-deps migrate >/dev/null
manage_owner apply_db_grants >/dev/null

echo "==> Putting the real logins back"
KEPT="$(manage_owner keep_logins import --in - <"$KEEP" | tail -1)"
echo "    $KEPT"
shred -u "$KEEP" 2>/dev/null || rm -f "$KEEP"

echo "==> Seeding the $DATASET dataset"
manage_owner seed_demo --dataset "$DATASET"

ATTACHED=""
if [ -n "$ATTACH_TASK" ]; then
  echo "==> Making the requester a board member and a demo home owner"
  # The data and the logins are in by now: a refusal here is reported, not fatal.
  if OUT="$(manage_owner demo_attach_requester --task "$ATTACH_TASK")"; then
    ATTACHED=" $(tail -1 <<<"$OUT")"
  else
    ATTACHED=" Requester NOT attached (see the task log)."
  fi
  echo "   $ATTACHED"
fi

echo "==> Removing the replaced data's files"
manage_owner clean_demo_files

STOPPED=0
echo "==> Starting the portal"
"${COMPOSE[@]}" start web worker >/dev/null
echo "Reseeded with the $DATASET dataset. $KEPT$ATTACHED"
