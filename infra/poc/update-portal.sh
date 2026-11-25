#!/usr/bin/env bash
# Pull the latest code and restart the portal on the POC VM, in one go.
#
#   sudo bash /opt/hoa-portal/infra/poc/update-portal.sh            # update if there is anything new
#   sudo bash /opt/hoa-portal/infra/poc/update-portal.sh --force    # rebuild and restart anyway
#
# Optional shortcut, once:  sudo ln -sf /opt/hoa-portal/infra/poc/update-portal.sh /usr/local/bin/hoa-update
# then just:                sudo hoa-update
#
# What it does: checks for local edits (refuses if any), fast-forwards the
# checked-out branch from GitHub, shows what changed, then runs
# start-portal.sh (rebuild, restart, migrations, storage and tunnel checks)
# and prints the record counts. Data volumes are never touched.
set -euo pipefail
trap 'echo "ERROR: update-portal.sh stopped at line $LINENO (exit $?)." >&2' ERR

FORCE=0
for arg in "$@"; do
  case "$arg" in
    --force) FORCE=1 ;;
    -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
    *) echo "Unknown option: $arg (try --help)" >&2; exit 2 ;;
  esac
done

[ "$(id -u)" -eq 0 ] || { echo "Run with sudo." >&2; exit 1; }
SCRIPT_PATH="$(readlink -f "$0")"
APP_DIR="$(cd "$(dirname "$SCRIPT_PATH")/../.." && pwd)"
cd "$APP_DIR"
[ -d .git ] || { echo "$APP_DIR is not a git checkout." >&2; exit 1; }

# Run git as the user who owns the checkout (avoids git's "dubious ownership"
# refusal and keeps file ownership unchanged).
REPO_OWNER="$(stat -c %U "$APP_DIR")"
as_owner() { if [ "$REPO_OWNER" = "root" ]; then "$@"; else sudo -u "$REPO_OWNER" -H "$@"; fi; }
g() { as_owner git -C "$APP_DIR" "$@"; }

BRANCH="$(g rev-parse --abbrev-ref HEAD)"
[ "$BRANCH" != "HEAD" ] || { echo "The checkout is not on a branch (detached HEAD)." >&2; exit 1; }

if [ -n "$(g status --porcelain --untracked-files=no)" ]; then
  echo "There are local edits in $APP_DIR; refusing to pull over them:" >&2
  g status --short --untracked-files=no >&2
  echo "Commit or discard them (git stash / git checkout -- <file>), then run again." >&2
  exit 1
fi

OLD="$(g rev-parse --short HEAD)"
echo "==> Fetching $BRANCH (now at $OLD)"
for attempt in 1 2 3 4; do
  if g fetch --quiet origin "$BRANCH"; then break; fi
  [ "$attempt" -lt 4 ] || { echo "Could not reach GitHub after 4 tries." >&2; exit 1; }
  sleep $((attempt * 2))
done

if ! g merge-base --is-ancestor HEAD "origin/$BRANCH"; then
  echo "The server's copy has commits that GitHub doesn't; not overwriting them." >&2
  echo "Ask for help before going further (git log origin/$BRANCH..HEAD shows them)." >&2
  exit 1
fi

g merge --ff-only --quiet "origin/$BRANCH"
NEW="$(g rev-parse --short HEAD)"

if [ "$OLD" = "$NEW" ]; then
  echo "Already up to date ($NEW)."
  if [ "$FORCE" -eq 0 ]; then
    echo "Nothing to do. Use --force to rebuild and restart anyway."
    exit 0
  fi
else
  echo "==> Updated $OLD -> $NEW:"
  g log --oneline --no-decorate "$OLD..$NEW" | sed 's/^/    /'
  # Only documentation or the regression suite changed: nothing to restart.
  if [ "$FORCE" -eq 0 ] && ! g diff --name-only "$OLD" "$NEW" \
       | grep -qvE '^(docs/|regression/|[^/]*\.md$|[^/]+/[^/]*\.md$)'; then
    echo "Only documentation changed; the running portal is unaffected."
    echo "Done: the portal code is at $NEW."
    exit 0
  fi
fi

echo
START=$(date +%s)
bash "$APP_DIR/infra/poc/start-portal.sh"

COMPOSE=(docker compose --project-directory "$APP_DIR" -f docker-compose.yml -f docker-compose.tunnel.yml)
echo
echo "Record counts (no personal data):"
"${COMPOSE[@]}" exec -T web python manage.py data_status 2>&1 | grep -v '"level"' || true
echo
echo "Done: the portal is running $NEW (update took $(( $(date +%s) - START ))s)."
