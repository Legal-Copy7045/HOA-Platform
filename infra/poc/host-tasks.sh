# Server tasks (Admin -> Server tasks in the portal, decision D36). Sourced
# by portal-agent.sh, never run on its own. Needs APP_DIR, the COMPOSE array
# and psql_run (SQL on stdin, values as psql variables).
#
# The portal only inserts a core_hosttask row naming a kind. Here the kind is
# only ever compared in host_task_command's case statement, which is the one
# mapping from a request to a fixed script with fixed arguments; nothing else
# read from the database reaches a command line. Results go back through psql
# variables. core/tests/test_host_tasks_agent.py runs these functions with a
# fake docker, psql and scripts.
#
# One kind takes a value: reseed_demo (D53) needs the dataset name. It is read
# back through a psql variable, must be lower-case letters and _ only, and
# must be one the portal's own seed_demo lists; then it is passed as one
# quoted argument to the fixed script, never inside a shell string.
#
# restore_backup (D98) needs the backup point: read back the same way, it
# must be local:<stamp> or offsite:<stamp> (backup.sh's YYYY-MM-DDTHHMMZ)
# naming a backup that really exists here (a folder under the backup root,
# not a link, or a complete set in the off-site bucket); then the fixed
# script gets "--local <stamp>" or "--offsite <stamp>", never a path. The
# restored database holds the backup's task history, so this task's own row
# is written back into it afterwards (host_task_finish_restore).

HOST_TASK_LOG="${HOST_TASK_LOG:-/var/log/hoa-portal-tasks.log}"
HOST_BACKUP_ROOT="${HOA_BACKUP_DIR:-/var/backups/hoa-portal}"

# host_task_command <kind>: set TASK_CMD (array) and TASK_MINUTES (time
# limit) for a known kind; fail for anything else.
host_task_command() {
  TASK_CMD=()
  TASK_MINUTES=0
  TASK_NEEDS_DATASET=0
  TASK_NEEDS_BACKUP=0
  case "$1" in
    backup)
      TASK_MINUTES=20; TASK_CMD=(bash "$APP_DIR/infra/poc/backup.sh") ;;
    restore_drill)
      TASK_MINUTES=25; TASK_CMD=(bash "$APP_DIR/infra/poc/restore-drill.sh") ;;
    restore_drill_offsite)
      TASK_MINUTES=25; TASK_CMD=(bash "$APP_DIR/infra/poc/restore-drill.sh" --from-offsite) ;;
    offsite_lock_check)
      TASK_MINUTES=5; TASK_CMD=("${COMPOSE[@]}" exec -T web python manage.py check_offsite_lock) ;;
    harden_host)
      TASK_MINUTES=25; TASK_CMD=(bash "$APP_DIR/infra/poc/harden-host.sh") ;;
    health_check)
      TASK_MINUTES=5; TASK_CMD=("${COMPOSE[@]}" exec -T web python manage.py health_check --alert) ;;
    reseed_demo)
      TASK_MINUTES=25; TASK_NEEDS_DATASET=1; TASK_CMD=(bash "$APP_DIR/infra/poc/reseed-demo.sh") ;;
    restart_app)
      TASK_MINUTES=5; TASK_CMD=(bash "$APP_DIR/infra/poc/restart-portal.sh") ;;
    restore_backup)
      TASK_MINUTES=28; TASK_NEEDS_BACKUP=1; TASK_CMD=(bash "$APP_DIR/infra/poc/restore-portal.sh") ;;
    *)
      return 1 ;;
  esac
}

# host_task_dataset <id>: set TASK_DATASET to the task's dataset if it is a
# plain name ([a-z_], 1-40 characters) that the portal's seed_demo lists;
# fail otherwise. TASK_ATTACH is 1 only when the column is exactly true.
# The id is a number (checked by the caller).
host_task_dataset() {
  local row value attach names
  TASK_DATASET=""
  TASK_ATTACH=0
  row="$(psql_run -v id="$1" <<'SQL'
SELECT dataset, attach_requester FROM core_hosttask WHERE id = :'id'::bigint;
SQL
)" || return 1
  IFS='|' read -r value attach <<<"$row"
  [[ "$value" =~ ^[a-z_]{1,40}$ ]] || return 1
  names="$("${COMPOSE[@]}" exec -T web python manage.py seed_demo --list-datasets 2>/dev/null)" \
    || return 1
  grep -Fxq -- "$value" <<<"$names" || return 1
  TASK_DATASET="$value"
  [ "$attach" = "t" ] && TASK_ATTACH=1
  return 0
}

# host_backup_stamp_ok <stamp>: backup.sh's folder name, nothing else.
host_backup_stamp_ok() {
  [[ "$1" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{4}Z$ ]]
}

# host_local_backup_ok <stamp>: a complete backup folder under the backup
# root (a real folder, not a link).
host_local_backup_ok() {
  local dir="$HOST_BACKUP_ROOT/$1"
  host_backup_stamp_ok "$1" || return 1
  [ -d "$dir" ] && [ ! -L "$dir" ] && [ -f "$dir/db.dump.enc" ] && [ -f "$dir/files.tar.enc" ] \
    && [ -f "$dir/HMAC-SHA256" ]
}

# host_task_backup <id>: set TASK_BACKUP_WHERE (local|offsite),
# TASK_BACKUP_STAMP and the row's requester, requested and started times
# (for writing the row back after a restore) if the task's backup point is
# well formed and names a backup that exists; fail otherwise.
host_task_backup() {
  local row value by at started where stamp
  TASK_BACKUP=""; TASK_BACKUP_WHERE=""; TASK_BACKUP_STAMP=""; TASK_BY=""; TASK_AT=""; TASK_STARTED=""
  row="$(psql_run -v id="$1" <<'SQL'
SELECT backup, coalesce(requested_by_id::text, requested_by_user_id::text, ''), requested_at, coalesce(started_at::text, '')
  FROM core_hosttask WHERE id = :'id'::bigint;
SQL
)" || return 1
  IFS='|' read -r value by at started <<<"$row"
  [[ "$value" =~ ^(local|offsite):([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{4}Z)$ ]] || return 1
  where="${BASH_REMATCH[1]}"; stamp="${BASH_REMATCH[2]}"
  [[ "$by" =~ ^[0-9]{0,18}$ ]] || return 1
  [[ "$at" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}[\ T][0-9:.+-]{5,30}$ ]] || return 1
  [[ "$started" =~ ^([0-9]{4}-[0-9]{2}-[0-9]{2}[\ T][0-9:.+-]{5,30})?$ ]] || return 1
  if [ "$where" = local ]; then
    host_local_backup_ok "$stamp" || return 1
  else
    "${COMPOSE[@]}" exec -T web python manage.py pull_backup --list 2>/dev/null \
      | LC_ALL=C tr -cd '0-9TZ\n-' | grep -Fxq -- "$stamp" || return 1
  fi
  TASK_BACKUP="$value"; TASK_BACKUP_WHERE="$where"; TASK_BACKUP_STAMP="$stamp"
  TASK_BY="$by"; TASK_AT="$at"; TASK_STARTED="$started"
  return 0
}

# host_backup_points_json: the local backup points as JSON for the restore
# page (D98): stamp, size in KB, the release label, whether it is a kept
# safety backup. Every value is checked or reduced to safe characters here,
# so the JSON needs no escaping.
host_backup_points_json() {
  local dir stamp kb version kept out="" sep=""
  if [ -d "$HOST_BACKUP_ROOT" ]; then
    for dir in "$HOST_BACKUP_ROOT"/*/; do
      dir="${dir%/}"
      stamp="${dir##*/}"
      host_local_backup_ok "$stamp" || continue
      kb="$(du -sk "$dir" 2>/dev/null | awk '{ print $1 }')" || kb=0
      kb="${kb//[^0-9]/}"
      version="$(head -c 64 "$dir/APP_VERSION" 2>/dev/null | LC_ALL=C tr -cd 'a-z0-9.-' | cut -c1-48)" || version=""
      kept=false
      [ -f "$dir/KEEP" ] && kept=true
      out+="$sep{\"stamp\":\"$stamp\",\"size_kb\":${kb:-0},\"version\":\"$version\",\"kept\":$kept}"
      sep=","
    done
  fi
  printf '[%s]\n' "$out"
}

# publish_backup_points: the local list every minute (cheap: a folder
# listing); tolerant of a database without the column yet.
publish_backup_points() {
  psql_run -v points="$(host_backup_points_json)" >/dev/null 2>&1 <<'SQL' || true
UPDATE core_updatestatus
   SET backup_points = coalesce(backup_points, '{}'::jsonb)
                       || jsonb_build_object('local', :'points'::jsonb, 'at', now())
 WHERE id = 1;
SQL
}

# publish_offsite_points: the off-site list (a bucket listing, so every 15
# minutes and after each task, not every minute).
publish_offsite_points() {
  "${COMPOSE[@]}" exec -T web python manage.py backup_inventory --offsite >/dev/null 2>&1 || true
}

# host_task_clean: printable ASCII only (no control or escape characters),
# lines cut to 200 characters, anything shaped like NAME_PASSWORD=value hidden.
# The scripts keep keys off their output already; this is a second guard.
host_task_clean() {
  LC_ALL=C tr -cd '\11\12\40-\176' \
    | LC_ALL=C sed -E 's/((PASSWORD|SECRET|TOKEN|_KEY)[A-Za-z0-9_]*[ ]*[=:][ ]*)[^ ]+/\1[hidden]/Ig' \
    | cut -c1-200
}

# host_task_summary <kind> <exit code> <output file>: one short line.
host_task_summary() {
  local kind="$1" rc="$2" out="$3" stamp size where
  if [ "$rc" -eq 124 ] || [ "$rc" -eq 137 ]; then
    echo "Stopped: it took longer than $TASK_MINUTES minutes."
    return 0
  fi
  {
    case "$kind:$rc" in
      backup:0)
        stamp="$(sed -n 's|^Backup done: .*/||p' "$out" | tail -1)"
        size="$(sed -n 's|^ *size: ||p' "$out" | tail -1)"
        if grep -q '^==> Copying off-site' "$out"; then where="copied off-site"; else where="local copy only"; fi
        echo "Backup ${stamp:-made}, ${size:-size unknown}, $where" ;;
      health_check:*)
        echo "$(grep -c '^OK ' "$out") checks OK, $(grep -c '^FAIL' "$out") failing" ;;
      restore_backup:[1-9]*)
        # The real error, then whether the safety backup went back (D98).
        echo "Failed (exit $rc): $(host_task_error_line "$out" | cut -c1-250) $(grep -E '^(NOT )?[Pp]ut back:' "$out" | tail -1 || true)" ;;
      *:0)
        grep -v '^[[:space:]]*$' "$out" | tail -1 ;;
      *)
        echo "Failed (exit $rc): $(host_task_error_line "$out")" ;;
    esac
  } | host_task_clean | head -1 | cut -c1-480 || true
}

# host_task_error_line <output file>: the line that says what went wrong.
# Only what came before a script's "==> Restarting after a failure" marker
# counts (after it, the EXIT trap's restart lines such as "Container ...
# Started"). The first line with ERROR, psql:, or Refusing wins; else after a
# Python traceback its exception line; else the last line (D98).
host_task_error_line() {
  local before line
  before="$(sed '/^==> Restarting after a failure/,$d' "$1")"
  line="$(grep -E -m1 'ERROR|psql:|Refusing' <<<"$before" || true)"
  if [ -z "$line" ] && grep -q '^Traceback' <<<"$before"; then
    line="$(sed -n '/^Traceback/,$p' <<<"$before" | grep -E '^[A-Za-z_.]+(Error|Exception|Exit)[A-Za-z]*:' | tail -1 || true)"
  fi
  [ -n "$line" ] || line="$(grep -v '^[[:space:]]*$' <<<"$before" | tail -1 || true)"
  printf '%s\n' "$line"
}

# host_task_finish <id> <state> <summary> <tail>: record the outcome. A
# reseed that failed half-way may have left no table to write to; the full
# output is in $HOST_TASK_LOG either way.
host_task_finish() {
  psql_run -v id="$1" -v state="$2" -v summary="$3" -v tail="$4" >/dev/null <<'SQL' \
    || echo "Could not record the result of server task #$1 (see $HOST_TASK_LOG)." >&2
UPDATE core_hosttask
   SET state = :'state', finished_at = now(), summary = :'summary', output_tail = :'tail'
 WHERE id = :'id'::bigint AND state = 'running';
SQL
}

# host_task_finish_restore <id> <state> <summary> <tail>: after a restore
# the database is the backup's (or, after a rollback, the safety backup's),
# which may not have this task's row or has an older copy of it: write it
# (insert or update by id; the requester's login only if it exists there,
# its id always, as requested_by_user_id, so the trail says who asked:
# review BUG-003, 0.45.0), and move the id counter past it. Values only as
# psql variables.
host_task_finish_restore() {
  psql_run -v id="$1" -v state="$2" -v summary="$3" -v tail="$4" -v backup="$TASK_BACKUP" \
    -v by="$TASK_BY" -v at="$TASK_AT" -v started="$TASK_STARTED" >/dev/null <<'SQL' \
    || echo "Could not record the result of server task #$1 (see $HOST_TASK_LOG)." >&2
INSERT INTO core_hosttask (id, kind, dataset, attach_requester, backup, requested_by_id,
                           requested_by_user_id, requested_at, state, started_at, finished_at,
                           summary, output_tail)
VALUES (:'id'::bigint, 'restore_backup', '', false, :'backup',
        (SELECT id FROM accounts_user WHERE id = NULLIF(:'by', '')::bigint),
        NULLIF(:'by', '')::bigint,
        :'at'::timestamptz, :'state', NULLIF(:'started', '')::timestamptz, now(),
        :'summary', :'tail')
ON CONFLICT (id) DO UPDATE
   SET kind = EXCLUDED.kind, dataset = '', attach_requester = false, backup = EXCLUDED.backup,
       requested_by_id = EXCLUDED.requested_by_id,
       requested_by_user_id = EXCLUDED.requested_by_user_id, requested_at = EXCLUDED.requested_at,
       state = EXCLUDED.state, started_at = EXCLUDED.started_at, finished_at = now(),
       summary = EXCLUDED.summary, output_tail = EXCLUDED.output_tail,
       audited_at = NULL, notified_at = NULL;
SELECT setval(pg_get_serial_sequence('core_hosttask', 'id'), (SELECT max(id) FROM core_hosttask));
SQL
}

# process_host_task: claim the oldest requested task, run it, record it.
process_host_task() {
  # Before the migration that adds the table, there is nothing to do.
  psql_run <<'SQL' >/dev/null 2>&1 || return 0
SELECT 1 FROM core_hosttask LIMIT 1;
SQL
  # The agent holds its lock, so a task still marked running was cut off
  # (the agent was stopped or the VM restarted).
  psql_run >/dev/null <<'SQL'
UPDATE core_hosttask
   SET state = 'failed', finished_at = now(),
       summary = 'Interrupted: the server agent stopped before the task finished.'
 WHERE state = 'running';
SQL
  local claimed id kind out rc=0 state summary tail
  claimed="$(psql_run <<'SQL'
UPDATE core_hosttask SET state = 'running', started_at = now()
 WHERE id = (SELECT id FROM core_hosttask WHERE state = 'requested'
              ORDER BY requested_at, id LIMIT 1 FOR UPDATE SKIP LOCKED)
RETURNING id, kind;
SQL
)"
  [ -n "$claimed" ] || return 0
  IFS='|' read -r id kind <<<"$claimed"
  [[ "$id" =~ ^[0-9]+$ ]] || { echo "Unexpected task id from the database." >&2; return 1; }
  if ! host_task_command "$kind"; then
    echo "=== $(date -u +%FT%TZ) server task #$id refused: not on the list" >>"$HOST_TASK_LOG"
    host_task_finish "$id" refused "Refused: not a task this server runs." ""
    record_host_tasks
    return 0
  fi
  if [ "$TASK_NEEDS_DATASET" -eq 1 ]; then
    if ! host_task_dataset "$id"; then
      echo "=== $(date -u +%FT%TZ) server task #$id refused: dataset not on the list" >>"$HOST_TASK_LOG"
      host_task_finish "$id" refused "Refused: not a demo dataset this portal has." ""
      record_host_tasks
      return 0
    fi
    TASK_CMD+=("$TASK_DATASET")
    # A fixed flag and the (numeric) task id: never text from the database.
    if [ "$TASK_ATTACH" -eq 1 ]; then TASK_CMD+=(--attach-requester "$id"); fi
  fi
  if [ "$TASK_NEEDS_BACKUP" -eq 1 ]; then
    if ! host_task_backup "$id"; then
      echo "=== $(date -u +%FT%TZ) server task #$id refused: backup point not on the list" >>"$HOST_TASK_LOG"
      host_task_finish "$id" refused "Refused: not a backup point this server has." ""
      record_host_tasks
      return 0
    fi
    # A fixed flag and a checked stamp: never a path, never other text.
    TASK_CMD+=("--$TASK_BACKUP_WHERE" "$TASK_BACKUP_STAMP")
  fi
  out="$(mktemp)"
  chmod 600 "$out"
  echo "=== $(date -u +%FT%TZ) server task #$id $kind" >>"$HOST_TASK_LOG"
  timeout --kill-after=30s "${HOST_TASK_LIMIT:-${TASK_MINUTES}m}" "${TASK_CMD[@]}" \
    </dev/null >"$out" 2>&1 || rc=$?
  cat "$out" >>"$HOST_TASK_LOG"
  if [ "$rc" -eq 0 ]; then state=succeeded; else state=failed; fi
  summary="$(host_task_summary "$kind" "$rc" "$out")"
  tail="$(host_task_clean <"$out" | grep -v '^[[:space:]]*$' | tail -n 40 || true)"
  rm -f "$out"
  # A task may have restarted the database; wait for it before writing back.
  for _ in 1 2 3 4 5 6; do "${COMPOSE[@]}" exec -T postgres pg_isready -q 2>/dev/null && break; sleep 5; done
  if [ "$TASK_NEEDS_BACKUP" -eq 1 ]; then
    host_task_finish_restore "$id" "$state" "$summary" "$tail"
  else
    host_task_finish "$id" "$state" "$summary" "$tail"
  fi
  record_host_tasks
  publish_backup_points
  publish_offsite_points
}

# The audit log gets each finished task (kind and state) from the app itself,
# and the requester and sysadmins an email with the result (D98).
record_host_tasks() {
  "${COMPOSE[@]}" exec -T web python manage.py record_host_tasks >/dev/null 2>&1 || true
}
