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
