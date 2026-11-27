"""The VM agent's server-task side (infra/poc/host-tasks.sh, decision D36),
run with a fake docker, psql and scripts: success, failure, time limit,
unknown kinds, and that nothing read from the database reaches a command
line. Plus static checks of the scripts."""

from __future__ import annotations

import re
import shutil
import subprocess  # nosec B404 - runs the repository's own scripts
from pathlib import Path

import pytest

POC = Path("infra/poc")
TASKS = (POC / "host-tasks.sh").read_text()
AGENT = (POC / "portal-agent.sh").read_text()
BASH = shutil.which("bash")
needs_bash = pytest.mark.skipif(BASH is None, reason="bash not installed")

HARNESS = r"""
set -euo pipefail
APP_DIR="$1"
COMPOSE=(docker compose -f docker-compose.yml)
HOST_TASK_LOG="$APP_DIR/tasks.log"
source "$2"
psql_run() {
  local sql
  sql="$(cat)"
  { printf 'ARGS:'; printf ' [%s]' "$@"; printf '\nSQL: %s\n' "$sql"; } >>"$APP_DIR/psql.log"
  case "$sql" in
    *"RETURNING id, kind"*) printf '%s\n' "$CLAIM" ;;
    *"SELECT dataset, attach_requester FROM core_hosttask"*)
      printf '%s|%s\n' "${DATASET:-}" "${ATTACH:-f}" ;;
  esac
  return 0
}
process_host_task
"""


def _script(path: Path, body: str) -> None:
    path.write_text("#!/usr/bin/env bash\n" + body)
    path.chmod(0o755)


@pytest.fixture
def vm(tmp_path):
    """A fake VM: scripts that log their arguments, and a fake docker."""
    poc = tmp_path / "infra" / "poc"
    poc.mkdir(parents=True)
    log = tmp_path / "ran.log"
    _script(
        poc / "backup.sh",
        f'echo "backup.sh $*" >> "{log}"\n'
        'echo "==> Database"\n'
        "printf 'colour \\033[31mred\\033[0m bell \\a\\n'\n"
        'echo "HOA_OWNER_DB_PASSWORD=supersecret"\n'
        'echo "    size: 5.3M"\n'
        'echo "==> Copying off-site"\n'
        'echo "Backup done: /var/backups/hoa-portal/2026-11-26T1830Z"\n',
    )
    _script(
        poc / "restore-drill.sh",
        f'echo "restore-drill.sh $*" >> "{log}"\n'
        'if [ "${1:-}" = "--from-offsite" ]; then\n'
        '  echo "Restore drill passed for /root/x (made at 2026-11-26T0230Z, version abc1234)."\n'
        "  exit 0\n"
        "fi\n"
        'echo "==> Restoring the database"\n'
        'echo "ERROR: restore-drill.sh stopped at line 9 (exit 3)." >&2\n'
        "exit 3\n",
    )
    _script(poc / "harden-host.sh", f'echo "harden-host.sh $*" >> "{log}"\nsleep 20\n')
    _script(
        poc / "reseed-demo.sh",
        f'echo "reseed-demo.sh $*" >> "{log}"\n'
        'echo "==> Taking a backup first"\n'
        'echo "Reseeded with the $1 dataset. Kept 1 real login(s) and 2 server task record(s)."\n',
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _script(
        bin_dir / "docker",
        f'echo "docker $*" >> "{tmp_path}/docker.log"\n'
        'case "$*" in\n'
        '  *"health_check --alert"*)\n'
        '    printf "OK    Database: fine\\nFAIL  Backups: old\\n"; exit 1 ;;\n'
        '  *check_offsite_lock*) echo "Locked: the bucket refused to delete a backup file." ;;\n'
        "  *--list-datasets*) printf 'oak_creek\\nfresh_start\\n' ;;\n"
        "esac\n"
        "exit 0\n",
    )
    return tmp_path


def _run(
    vm: Path, claim: str, limit: str = "", dataset: str = "", attach: str = "f"
) -> tuple[subprocess.CompletedProcess, str]:
    env = {
        "PATH": f"{vm}/bin:/usr/bin:/bin",
        "CLAIM": claim,
        "DATASET": dataset,
        "ATTACH": attach,
    }
    if limit:
        env["HOST_TASK_LIMIT"] = limit
    result = subprocess.run(  # noqa: S603  # nosec B603
        [BASH, "-c", HARNESS, "agent", str(vm), str(POC / "host-tasks.sh")],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    psql = (vm / "psql.log").read_text() if (vm / "psql.log").exists() else ""
    return result, psql


def _finish(psql: str) -> str:
    """The ARGS line of the write-back (the one with -v state=...)."""
    lines = [line for line in psql.splitlines() if "[state=" in line]
    assert len(lines) == 1, psql
    return lines[0]


def _read(vm: Path, name: str) -> str:
    path = vm / name
    return path.read_text() if path.exists() else ""


@needs_bash
def test_backup_succeeds_with_a_summary_and_a_clean_tail(vm):
    result, psql = _run(vm, "12|backup")
    assert result.returncode == 0, result.stderr
    assert _read(vm, "ran.log") == "backup.sh \n"
    finish = _finish(psql)
    assert "[id=12]" in finish and "[state=succeeded]" in finish
    assert "[summary=Backup 2026-11-26T1830Z, 5.3M, copied off-site]" in finish
    tail = psql.split("[tail=", 1)[1]
    assert "colour [31mred[0m bell" in tail  # escape and bell characters dropped
    assert "\x1b" not in psql and "\x07" not in psql
    assert "supersecret" not in psql and "HOA_OWNER_DB_PASSWORD=[hidden]" in psql
    # The full output goes to the VM's own log only.
    assert "supersecret" in _read(vm, "tasks.log")
    assert "manage.py record_host_tasks" in _read(vm, "docker.log")


@needs_bash
def test_a_failing_task_is_recorded_failed_with_its_last_line(vm):
    _, psql = _run(vm, "3|restore_drill")
    finish = _finish(psql)
    assert "[state=failed]" in finish
    assert (
        "[summary=Failed (exit 3): ERROR: restore-drill.sh stopped at line 9 (exit 3).]" in finish
    )


@needs_bash
def test_offsite_drill_gets_its_fixed_argument(vm):
    _, psql = _run(vm, "4|restore_drill_offsite")
    assert _read(vm, "ran.log") == "restore-drill.sh --from-offsite\n"
    assert "[state=succeeded]" in _finish(psql)
    assert "Restore drill passed for /root/x" in _finish(psql)


@needs_bash
@pytest.mark.parametrize(
    ("kind", "command", "state", "summary"),
    [
        (
            "health_check",
            "exec -T web python manage.py health_check --alert",
            "failed",
            "1 checks OK, 1 failing",
        ),
        (
            "offsite_lock_check",
            "exec -T web python manage.py check_offsite_lock",
            "succeeded",
            "Locked: the bucket refused to delete a backup file.",
        ),
    ],
)
def test_manage_py_tasks_run_in_the_web_container(vm, kind, command, state, summary):
    _, psql = _run(vm, f"5|{kind}")
    assert f"docker compose -f docker-compose.yml {command}\n" in _read(vm, "docker.log")
    finish = _finish(psql)
    assert f"[state={state}]" in finish and f"[summary={summary}]" in finish


@needs_bash
def test_a_task_over_its_time_limit_is_stopped(vm):
    result, psql = _run(vm, "6|harden_host", limit="1s")
    assert result.returncode == 0, result.stderr
    finish = _finish(psql)
    assert "[state=failed]" in finish and "[summary=Stopped: it took longer than" in finish


@needs_bash
@pytest.mark.parametrize(
    "kind",
    ["send_test_email", "rm -rf /", "backup; touch pwned", "$(touch pwned)", "BACKUP", ""],
)
def test_unknown_kinds_are_refused_and_nothing_runs(vm, kind):
    result, psql = _run(vm, f"7|{kind}")
    assert result.returncode == 0, result.stderr
    assert _read(vm, "ran.log") == ""
    assert not (vm / "pwned").exists() and not Path("pwned").exists()
    finish = _finish(psql)
    assert (
        "[state=refused]" in finish and "[summary=Refused: not a task this server runs.]" in finish
    )
    # The only docker call is the audit command, which takes no arguments.
    assert _read(vm, "docker.log") == (
        "docker compose -f docker-compose.yml exec -T web python manage.py record_host_tasks\n"
    )


@needs_bash
def test_no_database_value_reaches_a_command_line(vm):
    _run(vm, "8|backup")
    argv = _read(vm, "docker.log") + _read(vm, "ran.log")
    psql_args = [line for line in _read(vm, "psql.log").splitlines() if line.startswith("ARGS:")]
    assert "backup" not in argv.replace("backup.sh", "")
    # psql gets values only as -v variables (state, summary, tail, id), never the kind.
    assert all("backup]" not in line for line in psql_args)


@needs_bash
def test_nothing_waiting_does_nothing(vm):
    result, psql = _run(vm, "")
    assert result.returncode == 0
    assert "[state=" not in psql and _read(vm, "ran.log") == "" and _read(vm, "docker.log") == ""


@needs_bash
def test_a_cut_off_task_is_marked_failed_before_claiming(vm):
    _, psql = _run(vm, "")
    interrupted = psql.index("summary = 'Interrupted")
    claim = psql.index("RETURNING id, kind")
    assert interrupted < claim


@needs_bash
def test_a_strange_id_stops_the_agent(vm):
    result, psql = _run(vm, "1; DROP TABLE x|backup")
    assert result.returncode != 0 and "[state=" not in psql and _read(vm, "ran.log") == ""


# --- Static checks ------------------------------------------------------------------------


def test_the_case_statement_is_the_only_mapping():
    assert TASKS.count("TASK_CMD=(") == 9  # the reset plus one per kind
    body = TASKS.split("host_task_command() {", 1)[1].split("\n}\n", 1)[0]
    assert body.count("TASK_CMD=(") == 9 and "*)\n      return 1 ;;" in body
    from core.models import HostTask

    mapped = set(re.findall(r"^    ([a-z_]+)\)$", body, re.M))
    assert mapped == set(HostTask.Kind.values)


def test_scripts_never_build_sql_or_commands_from_strings():
    for text in (TASKS, AGENT):
        assert '-c "$' not in text and "eval " not in text and "bash -c" not in text
    # The kind is only compared, logged once known, and used to pick a summary.
    uses = [line.strip() for line in TASKS.splitlines() if re.search(r"\$\{?kind\b", line)]
    assert uses == [
        'case "$kind:$rc" in',
        'if ! host_task_command "$kind"; then',
        'echo "=== $(date -u +%FT%TZ) server task #$id $kind" >>"$HOST_TASK_LOG"',
        'summary="$(host_task_summary "$kind" "$rc" "$out")"',
    ]
    # Values written back only through psql variables.
    assert '-v id="$1" -v state="$2" -v summary="$3" -v tail="$4"' in TASKS
    assert "WHERE id = :'id'::bigint" in TASKS


def test_agent_runs_tasks_after_the_update_logic_and_survives_a_missing_table():
    assert 'source "$APP_DIR/infra/poc/host-tasks.sh"' in AGENT
    install = AGENT.index('update-portal.sh" "${UPDATE_ARGS[@]}" >>')
    assert AGENT.rindex("process_host_task") > install
    assert "flock -n 9" in AGENT
    guard = TASKS.split("process_host_task() {", 1)[1]
    assert guard.index("SELECT 1 FROM core_hosttask LIMIT 1;") < guard.index("UPDATE core_hosttask")
    assert "|| return 0" in guard.split("SELECT 1 FROM core_hosttask", 1)[0]
    # Each task's time limit stays inside the agent service's 30 minutes.
    limits = [int(n) for n in re.findall(r"TASK_MINUTES=(\d+);", TASKS)]
    assert limits and max(limits) < 30
    assert "TimeoutStartSec=30min" in (POC / "start-portal.sh").read_text()


# --- Demo reseed (D53) --------------------------------------------------------------------

RESEED = (POC / "reseed-demo.sh").read_text()


@needs_bash
@pytest.mark.parametrize("dataset", ["fresh_start", "oak_creek"])
def test_reseed_runs_the_fixed_script_with_the_dataset_as_one_argument(vm, dataset):
    result, psql = _run(vm, "21|reseed_demo", dataset=dataset)
    assert result.returncode == 0, result.stderr
    assert _read(vm, "ran.log") == f"reseed-demo.sh {dataset}\n"
    finish = _finish(psql)
    assert "[state=succeeded]" in finish
    assert f"[summary=Reseeded with the {dataset} dataset." in finish
    # The dataset was read back through a psql variable, by id only.
    assert "ARGS: [-v] [id=21]\nSQL: SELECT dataset, attach_requester FROM core_hosttask" in psql
    # And checked against the portal's own list.
    assert "manage.py seed_demo --list-datasets" in _read(vm, "docker.log")


@needs_bash
@pytest.mark.parametrize(
    "dataset",
    [
        "",
        "Fresh_start",
        "fresh-start",
        "fresh_start; touch pwned",
        "$(touch pwned)",
        "../etc",
        "a" * 41,
        "unknown_set",  # well formed, but not a dataset this portal has
    ],
)
def test_reseed_with_a_bad_or_unknown_dataset_is_refused_and_nothing_runs(vm, dataset):
    result, psql = _run(vm, "22|reseed_demo", dataset=dataset)
    assert result.returncode == 0, result.stderr
    assert _read(vm, "ran.log") == ""
    assert not (vm / "pwned").exists() and not Path("pwned").exists()
    finish = _finish(psql)
    assert "[state=refused]" in finish
    assert "[summary=Refused: not a demo dataset this portal has.]" in finish


def test_reseed_case_entry_is_a_fixed_command():
    assert (
        "    reseed_demo)\n      TASK_MINUTES=25; TASK_NEEDS_DATASET=1; "
        'TASK_CMD=(bash "$APP_DIR/infra/poc/reseed-demo.sh") ;;'
    ) in TASKS
    # The dataset is appended as one quoted array element, after the checks.
    assert 'TASK_CMD+=("$TASK_DATASET")' in TASKS
    process = TASKS.split("process_host_task() {", 1)[1]
    assert process.index("host_task_dataset") < process.index('TASK_CMD+=("$TASK_DATASET")')
    assert process.index('TASK_CMD+=("$TASK_DATASET")') < process.index('"${TASK_CMD[@]}"')
    check = TASKS.split("host_task_dataset() {", 1)[1].split("\n}\n", 1)[0]
    assert '[[ "$value" =~ ^[a-z_]{1,40}$ ]] || return 1' in check
    assert 'grep -Fxq -- "$value"' in check and "--list-datasets" in check


def test_reseed_script_guards_and_order():
    """Stage and flag guard first, backup before anything changes, logins
    saved before the database is emptied, put back before seeding."""
    order = [
        'demo_dataset_ok "$DATASET"',
        'reseed_allowed "$(env_value DEPLOYMENT_STAGE)" "$(env_value SEED_DEMO_ALLOWED)" || exit 1',
        "seed_demo --list-datasets",
        'bash "$APP_DIR/infra/poc/backup.sh" \\\n'
        '  || { echo "The backup failed: nothing was changed."',
        "manage_owner keep_logins export --out -",
        '"${COMPOSE[@]}" stop web worker',
        "DROP SCHEMA public CASCADE;",
        '"${COMPOSE[@]}" run --rm -T --no-deps migrate >/dev/null',
        "manage_owner apply_db_grants",
        "manage_owner keep_logins import --in -",
        'manage_owner seed_demo --dataset "$DATASET"',
        'manage_owner demo_attach_requester --task "$ATTACH_TASK"',
        "manage_owner clean_demo_files",
        '"${COMPOSE[@]}" start web worker >/dev/null\necho "Reseeded',
    ]
    positions = [RESEED.index(step) for step in order]
    assert positions == sorted(positions), order
    # Owner credentials only through the one-shot migrate service and psql.
    assert 'manage_owner() { "${COMPOSE[@]}" run --rm -T --no-deps migrate manage "$@"; }' in RESEED
    assert "exec -T web python manage.py keep_logins" not in RESEED
    # The saved logins: a root-only temporary directory, shredded on every exit.
    assert 'KEEP_DIR="$(mktemp -d /root/hoa-keep-logins.XXXXXX)"' in RESEED
    assert 'chmod 700 "$KEEP_DIR"' in RESEED and "umask 077" in RESEED
    assert "trap finish EXIT" in RESEED and 'shred -u "$KEEP"' in RESEED
    # The site comes back even when a step fails or the time limit stops it.
    assert "trap 'exit 143' TERM INT" in RESEED
    finish = RESEED.split("finish() {", 1)[1].split("\n}\n", 1)[0]
    assert '"${COMPOSE[@]}" start web worker' in finish
    # The app role gets its schema access back; values only as psql variables.
    assert (
        'psql_owner -v app="$APP_USER"' in RESEED
        and 'GRANT USAGE ON SCHEMA public TO :"app";' in RESEED
    )
    assert '-c "$' not in RESEED and "eval " not in RESEED and "bash -c" not in RESEED


@needs_bash
@pytest.mark.parametrize(
    ("stage", "flag", "ok"),
    [
        ("poc", "true", True),
        ("poc", "TRUE", True),
        ("poc", "false", False),
        ("poc", "", False),
        ("production", "true", False),
        ("", "true", False),
        ("local", "true", False),
    ],
)
def test_reseed_runs_only_on_the_poc_with_demo_data_on(stage, flag, ok):
    from core.tests.test_infra_scripts import _lib

    result = _lib('reseed_allowed "$1" "$2"', stage, flag)
    assert (result.returncode == 0) is ok
    if not ok:
        assert "DEPLOYMENT_STAGE=poc and SEED_DEMO_ALLOWED=true" in result.stderr


@needs_bash
@pytest.mark.parametrize(
    ("name", "ok"),
    [("fresh_start", True), ("oak_creek", True), ("x;y", False), ("A", False), ("", False)],
)
def test_reseed_script_accepts_plain_dataset_names_only(name, ok):
    from core.tests.test_infra_scripts import _lib

    assert (_lib('demo_dataset_ok "$1"', name).returncode == 0) is ok


@needs_bash
@pytest.mark.parametrize(
    ("attach", "args"),
    [
        ("t", "fresh_start --attach-requester 23"),
        ("f", "fresh_start"),
        ("", "fresh_start"),
        ("true; touch pwned", "fresh_start"),  # only exactly "t" counts
    ],
)
def test_attach_requester_is_a_fixed_flag_with_the_task_id(vm, attach, args):
    result, psql = _run(vm, "23|reseed_demo", dataset="fresh_start", attach=attach)
    assert result.returncode == 0, result.stderr
    assert _read(vm, "ran.log") == f"reseed-demo.sh {args}\n"
    assert not (vm / "pwned").exists()
    assert "[state=succeeded]" in _finish(psql)


# --- Restart the portal (D91) ---------------------------------------------------------------

RESTART = (POC / "restart-portal.sh").read_text()


@needs_bash
def test_restart_runs_the_fixed_script_and_reports_its_last_line(vm):
    _script(
        vm / "infra" / "poc" / "restart-portal.sh",
        f'echo "restart-portal.sh $*" >> "{vm}/ran.log"\n'
        'echo "==> Restarting the web server and the background worker"\n'
        'echo "Restarted the web server and the background worker; the site answers."\n',
    )
    result, psql = _run(vm, "31|restart_app")
    assert result.returncode == 0, result.stderr
    assert _read(vm, "ran.log") == "restart-portal.sh \n"  # no arguments at all
    finish = _finish(psql)
    assert "[state=succeeded]" in finish
    assert "[summary=Restarted the web server and the background worker; the site answers.]" in (
        finish
    )


@needs_bash
@pytest.mark.parametrize("kind", ["restart", "restart_app ", "restart_web", "RESTART_APP"])
def test_only_the_exact_restart_kind_is_accepted(vm, kind):
    _, psql = _run(vm, f"32|{kind}")
    assert _read(vm, "ran.log") == ""
    assert "[state=refused]" in _finish(psql)


def test_restart_case_entry_and_script_use_both_compose_files():
    assert (
        "    restart_app)\n      TASK_MINUTES=5; "
        'TASK_CMD=(bash "$APP_DIR/infra/poc/restart-portal.sh") ;;'
    ) in TASKS
    assert (
        'COMPOSE=(docker compose --project-directory "$APP_DIR" -f docker-compose.yml '
        "-f docker-compose.tunnel.yml)"
    ) in RESTART
    order = [
        "ensure_compose_file .env",
        '"${COMPOSE[@]}" restart web worker',
        "grep -q '_edge$'",
        '"${COMPOSE[@]}" up -d --no-deps --force-recreate web',
        "{{.State.Health.Status}}",
        "\ncloudflared_edge_only\n",
        'echo "Restarted the web server',
    ]
    positions = [RESTART.index(step) for step in order]
    assert positions == sorted(positions)
    # Nothing rebuilt, pulled or migrated; nothing else restarted.
    for word in ("build", "pull", "migrate", "down", "postgres", "eval ", "bash -c"):
        assert word not in RESTART.split("set -euo pipefail", 1)[1], word
    assert "set -euo pipefail" in RESTART and '"$(id -u)" -eq 0' in RESTART


def test_reseed_script_accepts_only_the_flag_and_a_numeric_task_id():
    usage = RESEED.split('ATTACH_TASK=""', 1)[1].split("\nfi\n", 1)[0]
    assert (
        '[ "$#" -eq 3 ] && [ "$2" = "--attach-requester" ] && [[ "$3" =~ ^[0-9]{1,18}$ ]]' in usage
    )
    assert 'if [ -n "$ATTACH_TASK" ]; then' in RESEED
