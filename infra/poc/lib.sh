# Small helpers shared by the POC scripts. Sourced, never run; bash builtins
# only, so no value passed to them ever appears on a command line (ps, /proc).
# The cloudflared network helpers at the end (D91) call docker, with container
# and network names only. core/tests/test_infra_scripts.py runs each one.

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

# --- Editing .env safely (0.43.0) ------------------------------------------------
# Every guided set-*.sh script changes .env only through these helpers:
#
#   backup="$(env_backup .env smtp2go)"        # .env.before-smtp2go, mode 600
#   env_update .env KEY1 "$v1" KEY2 "$v2"     # only these keys, in place
#   ...check...
#   env_restore_keys .env "$backup" KEY1 KEY2  # on failure: only these keys back
#   env_discard_backup "$backup"              # on success: shredded
#
# env_update rewrites the file once: each named key's first line is replaced
# where it stands (repeats of it dropped, keys not there appended), every
# other line is kept byte for byte (comments, blank lines, other scripts'
# settings, a missing final newline is added), the file keeps its owner and
# group and is mode 600. A failed check never restores the whole old file:
# that would undo whatever changed in .env since (another script's settings,
# a fixed EMAIL_ password) - how an earlier bounce-mailbox run broke email.
# printf and read are builtins, so no value (a secret) reaches another
# process's command line, and backslashes are written unchanged.

# env_key_ok <key>: a plain upper-case variable name.
env_key_ok() {
  [[ "$1" =~ ^[A-Z][A-Z0-9_]{0,63}$ ]]
}

# env_update <file> <key> <value> [<key> <value> ...]
env_update() {
  local file="$1" line key i n found tmp
  shift
  if [ $(( $# % 2 )) -ne 0 ] || [ $# -eq 0 ]; then
    echo "env_update: give key value pairs." >&2
    return 2
  fi
  local -a keys=() values=() seen=()
  while [ $# -gt 0 ]; do
    env_key_ok "$1" || { echo "env_update: not a setting name: $1" >&2; return 2; }
    case "$2" in
      *$'\n'*|*$'\r'*) echo "env_update: $1 may not contain a line break." >&2; return 2 ;;
    esac
    keys+=("$1"); values+=("$2"); seen+=(0)
    shift 2
  done
  n=${#keys[@]}
  [ -f "$file" ] || { echo "env_update: $file does not exist." >&2; return 1; }
  tmp="$(mktemp "${file}.new.XXXXXX")"
  chmod 600 "$tmp"
  while IFS= read -r line || [ -n "$line" ]; do
    found=-1
    for ((i = 0; i < n; i++)); do
      if [[ "$line" == "${keys[i]}="* ]]; then found=$i; break; fi
    done
    if [ "$found" -lt 0 ]; then
      printf '%s\n' "$line"
    elif [ "${seen[found]}" -eq 0 ]; then
      printf '%s=%s\n' "${keys[found]}" "${values[found]}"
      seen[found]=1
    fi
  done <"$file" >"$tmp"
  for ((i = 0; i < n; i++)); do
    [ "${seen[i]}" -eq 1 ] || printf '%s=%s\n' "${keys[i]}" "${values[i]}" >>"$tmp"
  done
  chown --reference="$file" "$tmp" 2>/dev/null || true
  chmod 600 "$tmp"
  mv -f "$tmp" "$file"
}

# set_env <file> <key> <value>: one key (kept for older callers).
set_env() {
  env_update "$1" "$2" "$3"
}

# env_add_missing <file> <key> <value>: add the key only when .env has no
# line for it (start-portal.sh's new settings); never changes a set value.
env_add_missing() {
  local file="$1" key="$2" value="$3" line
  env_key_ok "$key" || return 2
  while IFS= read -r line || [ -n "$line" ]; do
    [[ "$line" == "${key}="* ]] && return 0
  done <"$file"
  env_update "$file" "$key" "$value"
}

# env_ensure_secret <file> <key> [bytes]: give the key a fresh random hex
# value (openssl rand, default 32 bytes) when .env has no line for it or an
# empty one; never changes a value that is set (D93: CREDENTIALS_KEK and
# BANK_FEED_TOKEN_KEY appear on their own at install, no manual step). Prints
# the key's name only, never the value.
env_ensure_secret() {
  local file="$1" key="$2" bytes="${3:-32}" current value
  env_key_ok "$key" || return 2
  [[ "$bytes" =~ ^[0-9]{2,3}$ ]] || return 2
  current="$(env_get "$file" "$key" || true)"
  current="${current//[\"\']/}"
  [ -z "$current" ] || return 0
  value="$(openssl rand -hex "$bytes")"
  [ "${#value}" -ge 32 ] || { echo "env_ensure_secret: could not make a value." >&2; return 1; }
  echo "Adding a new random ${key} to .env"
  env_update "$file" "$key" "$value"
}

# env_get <file> <key>: the value of the key's first line (quotes kept).
env_get() {
  local file="$1" key="$2" line
  while IFS= read -r line || [ -n "$line" ]; do
    if [[ "$line" == "${key}="* ]]; then printf '%s\n' "${line#*=}"; return 0; fi
  done <"$file"
  return 1
}

# env_backup <file> <label>: copy the file to <file>.before-<label> (mode 600,
# same owner) and print its name. One backup per script: a new run replaces it.
env_backup() {
  local file="$1" label="$2" backup
  [[ "$label" =~ ^[a-z0-9-]{1,40}$ ]] || { echo "env_backup: bad label" >&2; return 2; }
  backup="${file}.before-${label}"
  rm -f "$backup"
  ( umask 077; cp "$file" "$backup" )
  chmod 600 "$backup"
  chown --reference="$file" "$backup" 2>/dev/null || true
  printf '%s\n' "$backup"
}

# env_restore_keys <file> <backup> <key>...: put back only these keys as the
# backup had them (a key the backup did not have is removed). Everything else
# in the file stays as it is now.
env_restore_keys() {
  local file="$1" backup="$2" line key i n found tmp
  shift 2
  local -a keys=("$@") old=() had=() written=()
  n=${#keys[@]}
  for ((i = 0; i < n; i++)); do
    env_key_ok "${keys[i]}" || { echo "env_restore_keys: bad key ${keys[i]}" >&2; return 2; }
    had+=(0); old+=(""); written+=(0)
  done
  while IFS= read -r line || [ -n "$line" ]; do
    for ((i = 0; i < n; i++)); do
      if [ "${had[i]}" -eq 0 ] && [[ "$line" == "${keys[i]}="* ]]; then
        old[i]="$line"; had[i]=1
      fi
    done
  done <"$backup"
  tmp="$(mktemp "${file}.new.XXXXXX")"
  chmod 600 "$tmp"
  while IFS= read -r line || [ -n "$line" ]; do
    found=-1
    for ((i = 0; i < n; i++)); do
      if [[ "$line" == "${keys[i]}="* ]]; then found=$i; break; fi
    done
    if [ "$found" -lt 0 ]; then
      printf '%s\n' "$line"
    elif [ "${had[found]}" -eq 1 ] && [ "${written[found]}" -eq 0 ]; then
      printf '%s\n' "${old[found]}"
      written[found]=1
    fi
  done <"$file" >"$tmp"
  for ((i = 0; i < n; i++)); do
    if [ "${had[i]}" -eq 1 ] && [ "${written[i]}" -eq 0 ]; then
      printf '%s\n' "${old[i]}" >>"$tmp"
    fi
  done
  chown --reference="$file" "$tmp" 2>/dev/null || true
  chmod 600 "$tmp"
  mv -f "$tmp" "$file"
}

# env_discard_backup <backup>: remove the copy (it holds the superseded secrets).
env_discard_backup() {
  [ -n "$1" ] || return 0
  shred -u "$1" 2>/dev/null || rm -f "$1"
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

# tunnel_token_ok <token>: a Cloudflare tunnel token as the dashboard shows it
# (base64 text, at least 100 characters); nothing else may reach .env.
tunnel_token_ok() {
  if [ "${#1}" -lt 100 ]; then
    echo "That does not look like a tunnel token (too short)." >&2
    return 1
  fi
  if ! [[ "$1" =~ ^[A-Za-z0-9+/=_-]+$ ]]; then
    echo "That does not look like a tunnel token (unexpected characters)." >&2
    return 1
  fi
  return 0
}

# hostname_ok <name>: a plain lower-case DNS name with at least one dot.
hostname_ok() {
  [[ "$1" =~ ^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$ ]]
}

# release_git <user> <dir> <git args...>: git in the checkout, as the user who
# owns it (root runs it directly; safe.directory lets root read it either way).
release_git() {
  local user="$1" dir="$2"
  shift 2
  if [ "$user" = "root" ] || [ "$(id -un)" = "$user" ]; then
    git -c safe.directory="$dir" -C "$dir" "$@"
  else
    sudo -u "$user" -H git -C "$dir" "$@"
  fi
}

# checkout_release <dir> <user>: put the checkout's main branch at the newest
# release tag on origin/main (D90, the same pick_release_tag as
# update-portal.sh and the agent). The one code path for the first checkout
# (install.sh, provision-vm.sh, D94): a fresh `git clone --no-checkout` is
# checked out at the tag; an existing checkout only ever moves forward to it,
# never over local edits and never with a reset (updates are update-portal.sh's
# job). Prints the tag it is at.
checkout_release() {
  local dir="$1" user="$2" tag
  release_git "$user" "$dir" fetch --quiet --tags origin main \
    || { echo "Could not fetch the portal code from GitHub (check the server's internet access)." >&2; return 1; }
  tag="$(release_git "$user" "$dir" tag --merged origin/main --list 'v*' | pick_release_tag)" \
    || { echo "GitHub has no release of the portal yet (no v<MAJOR>.<MINOR>.<PATCH> tag on main)." >&2; return 1; }
  if [ ! -f "$dir/.git/index" ]; then
    # A fresh clone with nothing checked out yet.
    release_git "$user" "$dir" checkout --quiet -B main "$tag"
    release_git "$user" "$dir" branch --quiet --set-upstream-to=origin/main main
  elif [ "$(release_git "$user" "$dir" rev-parse HEAD)" = "$(release_git "$user" "$dir" rev-parse "$tag^{commit}")" ]; then
    :
  elif [ "$(release_git "$user" "$dir" rev-parse --abbrev-ref HEAD)" = "main" ] \
       && [ -z "$(release_git "$user" "$dir" status --porcelain --untracked-files=no)" ] \
       && release_git "$user" "$dir" merge-base --is-ancestor HEAD "$tag"; then
    release_git "$user" "$dir" merge --ff-only --quiet "$tag"
  else
    echo "The checkout in $dir is not an older release on main; left as it is (update with update-portal.sh)." >&2
    release_git "$user" "$dir" describe --tags --always 2>/dev/null || true
    return 0
  fi
  printf '%s\n' "$tag"
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

# --- Restore from a backup (D98) -----------------------------------------------

# backup_stamp_ok <stamp>: backup.sh's folder name (YYYY-MM-DDTHHMMZ), nothing else.
backup_stamp_ok() {
  [[ "$1" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{4}Z$ ]]
}

# backup_release_ok <backup's APP_VERSION label> <installed VERSION>: a
# backup may be restored onto the release that made it or a newer one (the
# migrations then bring it up); one from a newer release, or one that does
# not say, is refused with a clear message. Plain comparisons, no eval.
backup_release_ok() {
  local label="$1" installed="$2" a b c x y z
  if ! [[ "$label" =~ ^([0-9]{1,6})\.([0-9]{1,6})\.([0-9]{1,6})(-[a-z0-9]{1,40})?$ ]]; then
    echo "Refusing: the backup does not say which release made it (APP_VERSION), so it cannot be checked against this one." >&2
    return 1
  fi
  a=$((10#${BASH_REMATCH[1]})); b=$((10#${BASH_REMATCH[2]})); c=$((10#${BASH_REMATCH[3]}))
  if ! [[ "$installed" =~ ^([0-9]{1,6})\.([0-9]{1,6})\.([0-9]{1,6})$ ]]; then
    echo "Refusing: the installed release (VERSION) is not readable." >&2
    return 1
  fi
  x=$((10#${BASH_REMATCH[1]})); y=$((10#${BASH_REMATCH[2]})); z=$((10#${BASH_REMATCH[3]}))
  if [ "$a" -gt "$x" ] || { [ "$a" -eq "$x" ] && [ "$b" -gt "$y" ]; } \
     || { [ "$a" -eq "$x" ] && [ "$b" -eq "$y" ] && [ "$c" -gt "$z" ]; }; then
    echo "Refusing: the backup was made by release $a.$b.$c, newer than the installed $x.$y.$z. Install release $a.$b.$c (or newer) first, then restore." >&2
    return 1
  fi
  return 0
}

# --- Compose files always together (D91) -------------------------------------
# On the VM the portal is the base file plus the tunnel overlay. Plain
# `docker compose ...` in the checkout reads COMPOSE_FILE from .env, so it
# uses both files too; without it, `docker compose up -d --force-recreate web`
# recreated web without the edge network and cloudflared lost the site.
PORTAL_COMPOSE_FILE="docker-compose.yml:docker-compose.tunnel.yml"

# ensure_compose_file <env file>: add COMPOSE_FILE=<both files> when the file
# has no COMPOSE_FILE line (appended; no other line is touched). A line with
# another value is replaced with set_env, which rewrites only that key.
ensure_compose_file() {
  local file="$1" line current="" found=0
  while IFS= read -r line || [ -n "$line" ]; do
    if [[ "$line" == COMPOSE_FILE=* ]]; then found=1; current="${line#COMPOSE_FILE=}"; fi
  done <"$file"
  if [ "$found" -eq 0 ]; then
    echo "Adding COMPOSE_FILE=$PORTAL_COMPOSE_FILE to .env (plain docker compose uses both files)"
    # Keep the last line intact when the file does not end with a newline.
    if [ -s "$file" ] && [ -n "$(tail -c 1 "$file")" ]; then printf '\n' >>"$file"; fi
    printf 'COMPOSE_FILE=%s\n' "$PORTAL_COMPOSE_FILE" >>"$file"
  elif [ "$current" != "$PORTAL_COMPOSE_FILE" ]; then
    echo "Setting COMPOSE_FILE=$PORTAL_COMPOSE_FILE in .env (was ${current:-empty})"
    set_env "$file" COMPOSE_FILE "$PORTAL_COMPOSE_FILE"
  fi
  return 0
}

# network_names  (stdin: names, one per line): prints only plain names
# (letters, digits, _ . -), so nothing else reaches a command line or SQL.
network_names() {
  local name
  while IFS= read -r name || [ -n "$name" ]; do
    if [[ "$name" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$ ]]; then printf '%s\n' "$name"; fi
  done
  return 0
}

# cloudflared_networks: the running cloudflared container's id on the first
# line, its project's edge network on the second, then every network it is
# attached to. Prints nothing when cloudflared is not running. Needs COMPOSE.
cloudflared_networks() {
  local id project
  id="$("${COMPOSE[@]}" ps -q cloudflared 2>/dev/null | head -1 | tr -cd '0-9a-f')" || true
  [ -n "$id" ] || return 0
  project="$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' "$id" \
    2>/dev/null | head -1 | network_names)" || true
  printf '%s\n%s\n' "$id" "${project:-oak-creek-portal}_edge"
  docker inspect -f '{{range $name, $_ := .NetworkSettings.Networks}}{{println $name}}{{end}}' \
    "$id" 2>/dev/null | network_names || true
}

# cloudflared_extra_networks: the networks cloudflared is on besides edge,
# one per line. Security review E3/E9: cloudflared belongs on edge only and
# must never reach Postgres or the file store. Needs COMPOSE.
cloudflared_extra_networks() {
  cloudflared_networks | extra_networks
}

# extra_networks  (stdin: cloudflared_networks' output): the attached
# networks other than the edge network (line 2), one per line.
extra_networks() {
  local lines edge
  lines="$(cat)"
  [ -n "$lines" ] || return 0
  edge="$(sed -n 2p <<<"$lines")"
  sed -n '3,$p' <<<"$lines" | grep -Fxv -- "$edge" || true
}

# cloudflared_edge_only: disconnect cloudflared from every network but edge
# (for example after `docker network connect <project>_default` by hand).
# Idempotent: does nothing when it is on edge only or not running. Needs COMPOSE.
cloudflared_edge_only() {
  local lines id edge net
  lines="$(cloudflared_networks)"
  [ -n "$lines" ] || return 0
  id="$(sed -n 1p <<<"$lines")"
  edge="$(sed -n 2p <<<"$lines")"
  if ! sed -n '3,$p' <<<"$lines" | grep -Fxq -- "$edge"; then
    echo "WARNING: cloudflared is not on the $edge network; recreating it from both compose files."
    "${COMPOSE[@]}" up -d --no-deps --force-recreate cloudflared || true
    return 0
  fi
  while IFS= read -r net; do
    if [ -z "$net" ] || [ "$net" = "$edge" ]; then continue; fi
    echo "Disconnecting cloudflared from the $net network: it belongs on $edge only (security review E3)."
    docker network disconnect "$net" "$id" \
      || echo "WARNING: could not disconnect cloudflared from $net (sudo docker network disconnect $net $id)." >&2
  done < <(sed -n '3,$p' <<<"$lines")
  return 0
}

# --- The outbound firewall for the containers (D101) -------------------------
# harden-host.sh installs /usr/local/sbin/hoa-egress-firewall (from
# egress-firewall.sh); its timer keeps the rules on the portal's networks.
# After compose creates or recreates networks, the scripts re-apply at once
# rather than within the minute. Nothing happens where it is not installed
# (local development never has it).
EGRESS_FIREWALL_BIN="${HOA_EGRESS_BIN:-/usr/local/sbin/hoa-egress-firewall}"

egress_firewall_refresh() {
  [ -x "$EGRESS_FIREWALL_BIN" ] || return 0
  "$EGRESS_FIREWALL_BIN" apply --quiet \
    || echo "WARNING: the outbound firewall could not be re-applied (sudo hoa-egress-firewall status); its timer tries again each minute." >&2
  return 0
}

# egress_firewall_before_start <project> <compose command...>: where the
# firewall is installed and the project has no networks yet (first start, or
# after `compose down`), create them and the containers without starting
# them, then apply, so no container runs a moment without the rules (review
# EXP-001, 0.46.0). Networks that already exist keep their rules.
egress_firewall_before_start() {
  local project="$1"
  shift
  [ -x "$EGRESS_FIREWALL_BIN" ] || return 0
  if [ -n "$(docker network ls -q --filter "label=com.docker.compose.project=$project" 2>/dev/null)" ]; then
    return 0
  fi
  echo "==> Creating the networks and applying the outbound firewall before anything starts (D101)"
  "$@" up --no-start --remove-orphans \
    || { echo "WARNING: could not create the containers first; the firewall follows within a minute." >&2; return 0; }
  egress_firewall_refresh
}

# egress_firewall_state: "<state> <networks>": active, stale, missing or
# no-networks from the firewall; absent when it is not installed; unknown
# when it cannot tell. Only that shape is printed (it goes to SQL variables).
egress_firewall_state() {
  local out
  if [ ! -x "$EGRESS_FIREWALL_BIN" ]; then echo "absent 0"; return 0; fi
  out="$("$EGRESS_FIREWALL_BIN" status 2>/dev/null | head -1)" || true
  if [[ "$out" =~ ^(active|stale|missing|no-networks)\ ([0-9]{1,3})$ ]]; then
    echo "$out"
  else
    echo "unknown 0"
  fi
}
