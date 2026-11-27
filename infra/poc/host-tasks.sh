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

HOST_TASK_LOG="${HOST_TASK_LOG:-/var/log/hoa-portal-tasks.log}"

# host_task_command <kind>: set TASK_CMD (array) and TASK_MINUTES (time
# limit) for a known kind; fail for anything else.
host_task_command() {
  TASK_CMD=()
  TASK_MINUTES=0
  TASK_NEEDS_DATASET=0
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
      *:0)
        grep -v '^[[:space:]]*$' "$out" | tail -1 ;;
      *)
        echo "Failed (exit $rc): $(grep -v '^[[:space:]]*$' "$out" | tail -1)" ;;
    esac
  } | host_task_clean | head -1 | cut -c1-480 || true
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
  host_task_finish "$id" "$state" "$summary" "$tail"
  record_host_tasks
}

# The audit log gets each finished task (kind and state) from the app itself.
record_host_tasks() {
  "${COMPOSE[@]}" exec -T web python manage.py record_host_tasks >/dev/null 2>&1 || true
}
