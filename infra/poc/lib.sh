# Small helpers shared by the POC scripts. Sourced, never run; bash builtins
# only, so no value passed to them ever appears on a command line (ps, /proc).
# core/tests/test_infra_scripts.py runs each one.

# The address of the drill's own, empty file store (the scratch SeaweedFS).
DRILL_STORE_ENDPOINT="http://seaweedfs:8333"
DRILL_STORE_BUCKET="hoa-restore-drill"

# scratch_store_ok <drill endpoint> <drill bucket> <live endpoint> <live bucket>
# Succeeds only when the drill would write to its own scratch store: exactly
# the drill endpoint and bucket, and never the live endpoint and bucket.
scratch_store_ok() {
  local endpoint="$1" bucket="$2" live_endpoint="$3" live_bucket="$4"
  if [ "$endpoint" != "$DRILL_STORE_ENDPOINT" ] || [ "$bucket" != "$DRILL_STORE_BUCKET" ]; then
    echo "The drill's file store is $endpoint (bucket $bucket), not its own scratch store." >&2
    return 1
  fi
  if [ "$endpoint" = "$live_endpoint" ] && [ "$bucket" = "$live_bucket" ]; then
    echo "The drill's file store is the live one." >&2
    return 1
  fi
  return 0
}

# file_count <Exported|Imported>: read command output on stdin and print N
# from the last "<Verb> N files." line; fails when there is none.
file_count() {
  local verb="$1" line found=""
  while IFS= read -r line || [ -n "$line" ]; do
    if [[ "$line" =~ (^|[^A-Za-z])${verb}\ ([0-9]+)\ files\. ]]; then
      found="${BASH_REMATCH[2]}"
    fi
  done
  [ -n "$found" ] || return 1
  printf '%s\n' "$found"
}

# counts_match <expected> <actual> <what>: fail loudly unless equal.
counts_match() {
  local expected="$1" actual="$2" what="$3"
  if ! [[ "$expected" =~ ^[0-9]+$ && "$actual" =~ ^[0-9]+$ ]] || [ "$expected" -ne "$actual" ]; then
    echo "$what: expected ${expected:-?} files, got ${actual:-?}." >&2
    return 1
  fi
  return 0
}

# set_env <file> <key> <value>: replace the key's line (dropping repeats) or
# add it. printf and read are builtins, so the value (a secret) never reaches
# another process's command line, and backslashes are written unchanged.
set_env() {
  local file="$1" key="$2" value="$3" line found=0 tmp
  tmp="$(mktemp "${file}.new.XXXXXX")"
  chmod 600 "$tmp"
  while IFS= read -r line || [ -n "$line" ]; do
    if [[ "$line" == "${key}="* ]]; then
      [ "$found" -eq 1 ] || printf '%s=%s\n' "$key" "$value"
      found=1
    else
      printf '%s\n' "$line"
    fi
  done <"$file" >"$tmp"
  [ "$found" -eq 1 ] || printf '%s=%s\n' "$key" "$value" >>"$tmp"
  mv "$tmp" "$file"
}

# branch_allowed <branch> <ALLOW_BRANCH from .env>: installs come from main,
# or from the one other branch named in ALLOW_BRANCH (review R2-011).
branch_allowed() {
  local branch="$1" allow="$2"
  [ "$branch" = "main" ] && return 0
  [ -n "$allow" ] && [ "$branch" = "$allow" ] && return 0
  return 1
}

# pick_release_tag  (stdin: tag names, one per line, e.g. `git tag --merged origin/main`)
# Prints the newest release tag v<MAJOR>.<MINOR>.<PATCH> (D90: the VM installs
# only released versions); pre-releases (v1.2.0-beta.1) and other names are
# skipped. Prints nothing and fails when there is none.
pick_release_tag() {
  local tag best="" a b c x y z
  while IFS= read -r tag; do
    [[ "$tag" =~ ^v([0-9]{1,6})\.([0-9]{1,6})\.([0-9]{1,6})$ ]] || continue
    a=$((10#${BASH_REMATCH[1]})); b=$((10#${BASH_REMATCH[2]})); c=$((10#${BASH_REMATCH[3]}))
    if [ -n "$best" ]; then
      [[ "$best" =~ ^v([0-9]+)\.([0-9]+)\.([0-9]+)$ ]]
      x=$((10#${BASH_REMATCH[1]})); y=$((10#${BASH_REMATCH[2]})); z=$((10#${BASH_REMATCH[3]}))
      if [ "$a" -lt "$x" ] || { [ "$a" -eq "$x" ] && [ "$b" -lt "$y" ]; } \
         || { [ "$a" -eq "$x" ] && [ "$b" -eq "$y" ] && [ "$c" -le "$z" ]; }; then
        continue
      fi
    fi
    best="$tag"
  done
  [ -n "$best" ] || return 1
  printf '%s\n' "$best"
}

# offsite_pick <now stamp>  (stdin: backup names, oldest first, as pull_backup --list)
# Prints the newest well-formed name not dated after <now>. Names in the
# future or not shaped like a stamp are skipped with a warning, so one
# mislabelled set in the bucket cannot block every drill (R2-005); the
# chosen backup's signed stamp is still checked against its name.
offsite_pick() {
  local now="$1" name best=""
  while IFS= read -r name; do
    name="$(printf '%s' "$name" | tr -cd '0-9TZ-')"
    [ -n "$name" ] || continue
    if ! [[ "$name" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{4}Z$ ]]; then
      echo "WARNING: skipping off-site backup with an unexpected name: $name" >&2
      continue
    fi
    if [[ "$name" > "$now" ]]; then
      echo "WARNING: skipping off-site backup $name: it is dated in the future." >&2
      continue
    fi
    if [ -z "$best" ] || [[ "$name" > "$best" ]]; then best="$name"; fi
  done
  [ -n "$best" ] || return 1
  printf '%s\n' "$best"
}

# offsite_stamp_ok <stamp in the object names> <stamp inside the signed checksum> <now stamp>
# The off-site drill picks a backup by its object names; the keyed checksum
# says when it was really made. They must be the same, and not in the future,
# or an older signed backup copied under a newer name would pass (R2-002).
offsite_stamp_ok() {
  local remote="$1" signed="$2" now="$3"
  if [ -z "$signed" ] || [ "$signed" != "$remote" ]; then
    echo "The off-site backup is named $remote but its keyed checksum says ${signed:-nothing}." >&2
    return 1
  fi
  if [[ "$remote" > "$now" ]]; then
    echo "The off-site backup $remote is dated in the future." >&2
    return 1
  fi
  return 0
}

# demo_dataset_ok <name>: a demo dataset name is lower-case letters and _
# only, 1-40 characters (D53); nothing else reaches a command line.
demo_dataset_ok() {
  [[ "$1" =~ ^[a-z_]{1,40}$ ]]
}

# reseed_allowed <DEPLOYMENT_STAGE from .env> <SEED_DEMO_ALLOWED from .env>:
# a demo reseed runs only on the POC with demo data switched on (D53).
reseed_allowed() {
  local stage="$1" flag="${2,,}"
  if [ "$stage" != "poc" ] || [ "$flag" != "true" ]; then
    echo "Refusing: a demo reseed needs DEPLOYMENT_STAGE=poc and SEED_DEMO_ALLOWED=true in .env." >&2
    return 1
  fi
  return 0
}
