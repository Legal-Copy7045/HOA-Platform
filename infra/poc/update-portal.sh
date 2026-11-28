#!/usr/bin/env bash
# Move the portal on the POC VM to the latest release and restart it, in one go.
#
#   sudo bash /opt/hoa-portal/infra/poc/update-portal.sh                # the newest release tag (v<VERSION>)
#   sudo bash /opt/hoa-portal/infra/poc/update-portal.sh --force        # rebuild and restart anyway
#   sudo bash /opt/hoa-portal/infra/poc/update-portal.sh --ref <branch> # deploy a branch's tip on purpose
#
# Optional shortcut, once:  sudo ln -sf /opt/hoa-portal/infra/poc/update-portal.sh /usr/local/bin/hoa-update
# then just:                sudo hoa-update
#
# What it does: checks for local edits (refuses if any), fetches from GitHub,
# moves the checkout to the newest release tag on main (a merged version bump
# is the release, D37/D90: nothing deploys from an unreleased commit; the
# local main branch is fast-forwarded to the tag, never past it), or with
# --ref to the tip of that branch, shows what changed, then runs
# start-portal.sh (the VM builds its own image, then restarts, migrates and
# checks storage and the tunnel) and prints the record counts. Data volumes
# are never touched.
set -euo pipefail
trap 'echo "ERROR: update-portal.sh stopped at line $LINENO (exit $?)." >&2' ERR

FORCE=0
REF=""
while [ $# -gt 0 ]; do
  case "$1" in
    --force) FORCE=1 ;;
    --ref)
      [ $# -ge 2 ] || { echo "--ref needs a branch name." >&2; exit 2; }
      REF="$2"; shift ;;
    -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1 (try --help)" >&2; exit 2 ;;
  esac
  shift
done
# A branch name only: letters, digits and . _ / - (never an option or a revision expression).
if [ -n "$REF" ] && { ! [[ "$REF" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$ ]] || [[ "$REF" == *..* ]]; }; then
  echo "Not a branch name: $REF" >&2; exit 2
fi

[ "$(id -u)" -eq 0 ] || { echo "Run with sudo." >&2; exit 1; }
SCRIPT_PATH="$(readlink -f "$0")"
APP_DIR="$(cd "$(dirname "$SCRIPT_PATH")/../.." && pwd)"
# shellcheck source=infra/poc/lib.sh
source "$APP_DIR/infra/poc/lib.sh"
cd "$APP_DIR"
[ -d .git ] || { echo "$APP_DIR is not a git checkout." >&2; exit 1; }

# First, even when there is nothing to install (D91): plain `docker compose`
# here uses both compose files, and cloudflared is on the edge network only
# (security review E3/E9).
COMPOSE=(docker compose --project-directory "$APP_DIR" -f docker-compose.yml -f docker-compose.tunnel.yml)
if [ -f .env ]; then ensure_compose_file .env; fi
cloudflared_edge_only

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

# Releases are tags on main; --ref follows the named branch instead.
WANT="${REF:-main}"
OLD="$(g rev-parse --short HEAD)"
echo "==> Fetching $WANT and the release tags (now at $OLD on $BRANCH)"
for attempt in 1 2 3 4; do
  if g fetch --quiet --tags origin "$WANT"; then break; fi
  [ "$attempt" -lt 4 ] || { echo "Could not reach GitHub after 4 tries." >&2; exit 1; }
  sleep $((attempt * 2))
done

if [ -z "$REF" ]; then
  TAG="$(g tag --merged "origin/main" --list 'v*' | pick_release_tag)" || {
    echo "No release tag (v<MAJOR>.<MINOR>.<PATCH>) on main yet; nothing to install." >&2
    echo "Deploy main's tip on purpose with --ref main." >&2
    exit 1
  }
  TARGET="$TAG"
  LABEL="release $TAG"
else
  TARGET="origin/$REF"
  LABEL="branch $REF (not a release)"
fi

if [ "$BRANCH" != "$WANT" ]; then
  echo "==> Switching the checkout from $BRANCH to $WANT"
  if g show-ref --verify --quiet "refs/heads/$WANT"; then
    g checkout --quiet "$WANT"
  else
    g checkout --quiet -b "$WANT" "$TARGET"
  fi
  BRANCH="$WANT"
fi

if [ -z "$REF" ] && [ "$(g rev-parse HEAD)" != "$(g rev-parse "$TARGET^{commit}")" ] \
   && g merge-base --is-ancestor "$TARGET" HEAD; then
  # main's tip was deployed on purpose (--ref main) after the newest release.
  echo "The checkout ($OLD) is newer than $LABEL (deployed on purpose with --ref); leaving it." >&2
  echo "It moves on with the next release, or run again with --ref main." >&2
  exit 0
fi

if ! g merge-base --is-ancestor HEAD "$TARGET"; then
  echo "The server's copy has commits that $LABEL doesn't; not overwriting them." >&2
  echo "Ask for help before going further (git log $TARGET..HEAD shows them)." >&2
  exit 1
fi

g merge --ff-only --quiet "$TARGET"
NEW="$(g rev-parse --short HEAD)"

if [ "$OLD" = "$NEW" ]; then
  echo "Already at $LABEL ($NEW)."
  if [ "$FORCE" -eq 0 ]; then
    echo "Nothing to do. Use --force to rebuild and restart anyway."
    exit 0
  fi
else
  echo "==> Updated $OLD -> $NEW ($LABEL):"
  g log --oneline --no-decorate "$OLD..$NEW" | sed 's/^/    /'
  # Only documentation or the regression suite changed: nothing to restart.
  if [ "$FORCE" -eq 0 ] && ! g diff --name-only "$OLD" "$NEW" \
       | grep -qvE '^(docs/|regression/|[^/]*\.md$|[^/]+/[^/]*\.md$)'; then
    echo "Only documentation changed; the running portal is unaffected."
    echo "Done: the portal code is at $NEW ($LABEL)."
    exit 0
  fi
fi

echo
START=$(date +%s)
bash "$APP_DIR/infra/poc/start-portal.sh"

echo
echo "Record counts (no personal data):"
"${COMPOSE[@]}" exec -T web python manage.py data_status 2>&1 | grep -v '"level"' || true
echo
echo "Done: the portal is running $NEW, $LABEL (update took $(( $(date +%s) - START ))s)."
