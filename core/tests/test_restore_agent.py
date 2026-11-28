"""The VM side of Restore from a backup and of the failure summaries (D98):
infra/poc/host-tasks.sh with a fake docker, psql and scripts (only a backup
point that really exists reaches the fixed script, as --local|--offsite
<stamp>; never a path), the local backup list, lib.sh's release check, the
summary that shows the real error, and static checks of restore-portal.sh
and the reseed's EXIT trap."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess  # nosec B404 - runs the repository's own scripts
from pathlib import Path

import pytest

POC = Path("infra/poc")
TASKS = (POC / "host-tasks.sh").read_text()
RESTORE = (POC / "restore-portal.sh").read_text()
RESEED = (POC / "reseed-demo.sh").read_text()
BACKUP = (POC / "backup.sh").read_text()
BASH = shutil.which("bash")
needs_bash = pytest.mark.skipif(BASH is None, reason="bash not installed")
STAMP = "2026-11-20T0230Z"

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
    *"SELECT backup, coalesce(requested_by_id"*)
      printf '%s|%s|%s|%s\n' "$POINT" "${BY:-7}" "$AT" "2026-11-28 02:41:00+00" ;;
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
    poc = tmp_path / "infra" / "poc"
    poc.mkdir(parents=True)
    log = tmp_path / "ran.log"
    _script(
        poc / "restore-portal.sh",
        f'echo "restore-portal.sh $*" >> "{log}"\n'
        'if [ "${FAIL:-}" = 1 ]; then\n'
        '  echo "==> Emptying the database (table by table)"\n'
        '  echo "psql:<stdin>:1: ERROR:  out of shared memory" >&2\n'
        '  echo "ERROR: restore-portal.sh stopped at line 99 (exit 3)." >&2\n'
        '  echo "==> Restarting after a failure (exit 3); the error is above"\n'
        '  echo "Put back: the safety backup 2026-11-28T0241Z (the portal as it was)."\n'
        '  echo " Container oak-creek-portal-web-1 Started" >&2\n'
        "  exit 3\n"
        "fi\n"
        f'echo "Restored the backup of {STAMP} (release 0.44.0-abc). Safety backup of the '
        'state before: 2026-11-28T0241Z (kept). Everyone signs in again."\n',
    )
    backups = tmp_path / "backups"
    good = backups / STAMP
    good.mkdir(parents=True)
    for f in ("db.dump.enc", "files.tar.enc", "HMAC-SHA256", "SHA256SUMS"):
        (good / f).write_bytes(b"x" * 2048)
    (good / "APP_VERSION").write_text("0.44.0-abc1234\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _script(
        bin_dir / "docker",
        f'echo "docker $*" >> "{tmp_path}/docker.log"\n'
        'case "$*" in\n'
        "  *'pull_backup --list'*) printf '2026-11-19T0230Z\\n2026-11-21T0230Z\\n' ;;\n"
        "esac\n"
        "exit 0\n",
    )
    return tmp_path


def _run(vm: Path, point: str, fail: bool = False, by: str = "7"):
    env = {
        "PATH": f"{vm}/bin:/usr/bin:/bin",
        "CLAIM": "31|restore_backup",
        "POINT": point,
        "BY": by,
        "HOA_BACKUP_DIR": str(vm / "backups"),
        "FAIL": "1" if fail else "",
        "AT": "2026-11-28 02:40:11.744+00",
    }
    result = subprocess.run(  # noqa: S603  # nosec B603
        [BASH, "-c", HARNESS, "agent", str(vm), str(POC / "host-tasks.sh")],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    psql = (vm / "psql.log").read_text() if (vm / "psql.log").exists() else ""
    ran = (vm / "ran.log").read_text() if (vm / "ran.log").exists() else ""
    return result, psql, ran


def _finish(psql: str) -> str:
    lines = [line for line in psql.splitlines() if "[state=" in line]
    assert len(lines) == 1, psql
    return lines[0]


@needs_bash
def test_a_listed_local_backup_runs_the_fixed_script_with_the_stamp(vm):
    result, psql, ran = _run(vm, f"local:{STAMP}")
    assert result.returncode == 0, result.stderr
    assert ran == f"restore-portal.sh --local {STAMP}\n"
    finish = _finish(psql)
    assert "[state=succeeded]" in finish and f"[backup=local:{STAMP}]" in finish
    assert "[by=7]" in finish and "[at=2026-11-28 02:40:11.744+00]" in finish
    # The row is written back into the restored database (insert or update).
    tail = psql.split("[state=succeeded]", 1)[1]
    assert "INSERT INTO core_hosttask" in tail and "ON CONFLICT (id) DO UPDATE" in tail
    assert "setval(pg_get_serial_sequence('core_hosttask', 'id')" in tail
    assert "[summary=Restored the backup of 2026-11-20T0230Z" in finish
    # The requester's id is kept even where that login is not in the restored
    # data (review BUG-003, 0.45.0).
    assert "requested_by_user_id = EXCLUDED.requested_by_user_id" in tail
    assert "NULLIF(:'by', '')::bigint," in tail  # the id itself, login or not


@needs_bash
def test_an_offsite_point_must_be_in_the_bucket_list(vm):
    _, psql, ran = _run(vm, "offsite:2026-11-21T0230Z")
    assert ran == "restore-portal.sh --offsite 2026-11-21T0230Z\n"
    assert "[state=succeeded]" in _finish(psql)


@needs_bash
@pytest.mark.parametrize(
    "point",
    [
        "",
        STAMP,
        "local:2026-11-21T0230Z",  # well formed, not on this server
        "offsite:2026-11-22T0230Z",  # not in the bucket
        "local:../../etc",
        "local:2026-11-20T0230Z/../../x",
        f"local:{STAMP}; touch pwned",
        "local:$(touch pwned)",
        "local:/var/backups/hoa-portal/2026-11-20T0230Z",
        "remote:2026-11-20T0230Z",
        "local:linked",
    ],
)
def test_anything_else_is_refused_and_nothing_runs(vm, point):
    (vm / "backups" / "linked").symlink_to(vm / "backups" / STAMP)
    result, psql, ran = _run(vm, point)
    assert result.returncode == 0, result.stderr
    assert ran == "" and not (vm / "pwned").exists() and not Path("pwned").exists()
    finish = _finish(psql)
    assert "[state=refused]" in finish
    assert "[summary=Refused: not a backup point this server has.]" in finish


@needs_bash
def test_a_symlinked_backup_folder_is_refused(vm):
    other = vm / "elsewhere"
    shutil.copytree(vm / "backups" / STAMP, other)
    shutil.rmtree(vm / "backups" / STAMP)
    (vm / "backups" / STAMP).symlink_to(other)
    _, psql, ran = _run(vm, f"local:{STAMP}")
    assert ran == "" and "[state=refused]" in _finish(psql)


@needs_bash
def test_a_requester_id_that_is_not_a_number_is_refused(vm):
    _, psql, ran = _run(vm, f"local:{STAMP}", by="7; DROP TABLE x")
    assert ran == "" and "[state=refused]" in _finish(psql)


@needs_bash
def test_a_failure_shows_the_real_error_and_whether_the_safety_backup_went_back(vm):
    _, psql, _ = _run(vm, f"local:{STAMP}", fail=True)
    finish = _finish(psql)
    assert "[state=failed]" in finish
    assert (
        "[summary=Failed (exit 3): psql:<stdin>:1: ERROR:  out of shared memory "
        "Put back: the safety backup 2026-11-28T0241Z" in finish
    )
    assert "Container" not in finish.split("[tail=")[0]


# --- The summary of any failed task -----------------------------------------------------

SUMMARY = r"""
set -euo pipefail
source "$1"
TASK_MINUTES=25
host_task_summary "$2" "$3" "$4"
"""


def _summary(tmp_path, kind, rc, output):
    out = tmp_path / "out.txt"
    out.write_text(output)
    result = subprocess.run(  # noqa: S603  # nosec B603
        [BASH, "-c", SUMMARY, "s", str(POC / "host-tasks.sh"), kind, str(rc), str(out)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@needs_bash
def test_the_reseed_failure_shows_the_psql_error_not_the_restart(tmp_path):
    output = (
        "==> Taking a backup first\nBackup done: /var/backups/hoa-portal/2026-11-27T2210Z\n"
        "==> Stopping the portal\n==> Emptying the database\n"
        "psql:<stdin>:1: ERROR:  out of shared memory\n"
        "HINT:  You might need to increase max_locks_per_transaction.\n"
        "ERROR: reseed-demo.sh stopped at line 107 (exit 3).\n"
        "==> Restarting after a failure (exit 3); the error is above\n"
        " Container oak-creek-portal-worker-1 Started\n"
        " Container oak-creek-portal-web-1 Started\n"
    )
    assert _summary(tmp_path, "reseed_demo", 3, output) == (
        "Failed (exit 3): psql:<stdin>:1: ERROR:  out of shared memory"
    )


@needs_bash
@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("step\nRefusing: the dataset is unknown.\nlast\n", "Refusing: the dataset is unknown."),
        (
            "Traceback (most recent call last):\n  File x\nValueError: bad value\nlast line\n",
            "ValueError: bad value",
        ),
        ("one\ntwo\n\n", "two"),
        ("one\n==> Restarting after a failure (exit 1)\n Container x Started\n", "one"),
    ],
)
def test_the_summary_prefers_the_error_line(tmp_path, output, expected):
    assert _summary(tmp_path, "harden_host", 1, output) == f"Failed (exit 1): {expected}"


# --- The local backup list ----------------------------------------------------------------

POINTS = r"""
set -euo pipefail
source "$1"
host_backup_points_json
"""


@needs_bash
def test_the_local_list_has_only_complete_real_backup_folders(tmp_path):
    root = tmp_path / "backups"
    for name in (STAMP, "2026-11-21T0230Z", "not-a-stamp", "2026-11-22T0230Z"):
        d = root / name
        d.mkdir(parents=True)
        for f in ("db.dump.enc", "files.tar.enc", "HMAC-SHA256"):
            if not (name == "2026-11-22T0230Z" and f == "HMAC-SHA256"):  # incomplete
                (d / f).write_bytes(b"y" * 4096)
    (root / STAMP / "APP_VERSION").write_text('0.44.0-abc1234"},{"x":1\n')
    (root / "2026-11-21T0230Z" / "KEEP").write_text("kept\n")
    (root / "2026-11-23T0230Z").symlink_to(root / STAMP)
    result = subprocess.run(  # noqa: S603  # nosec B603
        [BASH, "-c", POINTS, "p", str(POC / "host-tasks.sh")],
        capture_output=True,
        text=True,
        env={**os.environ, "HOA_BACKUP_DIR": str(root)},
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    rows = json.loads(result.stdout)
    assert [r["stamp"] for r in rows] == [STAMP, "2026-11-21T0230Z"]
    assert rows[0]["version"] == "0.44.0-abc1234x1"  # quotes and braces dropped
    assert rows[0]["kept"] is False and rows[1]["kept"] is True
    assert all(isinstance(r["size_kb"], int) and r["size_kb"] > 0 for r in rows)


# --- lib.sh: which releases may be restored --------------------------------------------------

LIB = r"""
source "$1"
backup_release_ok "$2" "$3"
"""


@needs_bash
@pytest.mark.parametrize(
    ("label", "installed", "ok", "says"),
    [
        ("0.44.0-abc1234", "0.45.0", True, ""),
        ("0.45.0-abc1234", "0.45.0", True, ""),
        ("0.9.9", "0.45.0", True, ""),
        ("0.45.1-abc", "0.45.0", False, "newer than the installed 0.45.0"),
        ("1.0.0-abc", "0.45.0", False, "Install release 1.0.0"),
        ("", "0.45.0", False, "does not say which release"),
        ("unknown", "0.45.0", False, "does not say which release"),
        ("0.44.0; rm -rf /", "0.45.0", False, "does not say which release"),
    ],
)
def test_backup_release_ok(label, installed, ok, says):
    result = subprocess.run(  # noqa: S603  # nosec B603
        [BASH, "-c", LIB, "l", str(POC / "lib.sh"), label, installed],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert (result.returncode == 0) is ok
    assert says in result.stderr


# --- Static checks --------------------------------------------------------------------------


def test_restore_case_entry_is_a_fixed_command():
    assert (
        "    restore_backup)\n      TASK_MINUTES=28; TASK_NEEDS_BACKUP=1; "
        'TASK_CMD=(bash "$APP_DIR/infra/poc/restore-portal.sh") ;;'
    ) in TASKS
    process = TASKS.split("process_host_task() {", 1)[1]
    assert process.index("host_task_backup") < process.index('"${TASK_CMD[@]}"')
    assert 'TASK_CMD+=("--$TASK_BACKUP_WHERE" "$TASK_BACKUP_STAMP")' in process
    check = TASKS.split("host_task_backup() {", 1)[1].split("\n}\n", 1)[0]
    assert "^(local|offsite):([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{4}Z)$" in check
    assert "host_local_backup_ok" in check and "pull_backup --list" in check


def test_restore_script_steps_in_order():
    order = [
        'backup_stamp_ok "$STAMP"',
        '[ -L "$DIR" ]',
        'backup_mac.py" verify',
        'offsite_stamp_ok "$STAMP" "$SIGNED"',
        'backup_release_ok "$LABEL" "$INSTALLED" || exit 1',
        "pg_restore -l",
        'bash "$APP_DIR/infra/poc/backup.sh" --keep',
        'STOPPED=1\n"${COMPOSE[@]}" stop web worker',
        'load_backup "$SRC"',
        "STOPPED=0\nstart_portal",
    ]
    positions = [RESTORE.index(step) for step in order]
    assert positions == sorted(positions), order
    load = RESTORE.split("load_backup() {", 1)[1].split("\n}\n", 1)[0]
    steps = [
        "empty-database.sql",
        "pg_restore -h 127.0.0.1",
        "--exit-on-error",
        "run --rm -T --no-deps migrate",
        "apply_db_grants",
        "DELETE FROM django_session;",
        "WHERE state IN ('requested', 'running');",
        "import_files --in -",
        "check_storage",
    ]
    assert [load.index(s) for s in steps] == sorted(load.index(s) for s in steps)
    assert "--overwrite" not in RESTORE  # never replaces stored files (SEC-009)
    # Every step in load_backup is checked, so it also works from the EXIT trap.
    for line in load.splitlines():
        if re.match(r"\s+(psql_owner|decrypt|manage_owner|\"\$\{COMPOSE)", line):
            assert line.rstrip().endswith(("|| return 1", "\\", "<<'SQL' || return 1")), line
    # On a failure after the stop the safety backup goes back, after the marker.
    finish = RESTORE.split("finish() {", 1)[1].split("\n}\n", 1)[0]
    assert finish.index("==> Restarting after a failure") < finish.index(
        'load_backup "$BACKUP_ROOT/$SAFETY"'
    )
    assert "Put back:" in finish and "NOT put back:" in finish and "start_portal" in finish
    assert "trap finish EXIT" in RESTORE and "trap 'exit 143' TERM INT" in RESTORE
    # Never a path from outside, no rm -rf of a variable, no strings run as code.
    assert 'rm -rf "$' not in RESTORE and "eval " not in RESTORE and "bash -c" not in RESTORE
    assert '-c "$' not in RESTORE
    assert (
        'COMPOSE=(docker compose --project-directory "$APP_DIR" -f docker-compose.yml '
        "-f docker-compose.tunnel.yml)" in RESTORE
    )


def test_safety_backups_are_kept_by_the_clean_up():
    assert "--keep) KEEP_MARK=1 ;;" in BACKUP
    assert "! -exec test -e '{}/KEEP' ';'" in BACKUP


def test_reseed_marks_its_restart_after_a_failure():
    finish = RESEED.split("finish() {", 1)[1].split("\n}\n", 1)[0]
    assert finish.index("==> Restarting after a failure") < finish.index("start web worker")
