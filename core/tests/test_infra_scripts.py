"""Deployment scripts and compose files (review R2-002 to R2-005, R2-011,
R2-012, BUG-003). The VM is out of reach from tests: these read the files,
run the pure helpers, and check the backup checksum tool end to end."""

from __future__ import annotations

import re
import shutil
import subprocess  # nosec B404 - runs the repository's own scripts
import sys
from pathlib import Path

import pytest

POC = Path("infra/poc")
BASH = shutil.which("bash")


# --- R2-012: every setting in .env.example reaches the containers --------------------------

# Keys in .env.example that compose deliberately does not pass to web/worker,
# with the reason. Anything else must appear in a compose file.
NOT_PASSED = {
    # docker compose itself reads it to pick services.
    "COMPOSE_PROFILES": "read by docker compose on the host",
    "ALLOW_BRANCH": "read by infra/poc/portal-agent.sh on the VM",
    # Built by compose from the HOA_* values; listed for use outside compose.
    "DATABASE_URL": "compose builds it from HOA_APP_DB_*",
    "DB_APP_ROLE": "compose sets it from HOA_APP_DB_USER",
    # Its default differs per settings module (production: on); a blank
    # pass-through would switch it off.
    "SECURE_SSL_REDIRECT": "production default applies behind the tunnel",
}


def _env_example_keys() -> set[str]:
    keys = set()
    for line in Path(".env.example").read_text().splitlines():
        match = re.match(r"^#?\s?([A-Z][A-Z0-9_]+)=", line)
        if match:
            keys.add(match.group(1))
    return keys


def test_every_env_example_key_is_passed_through_compose_or_documented():
    compose = Path("docker-compose.yml").read_text() + Path("docker-compose.tunnel.yml").read_text()
    missing = sorted(
        key
        for key in _env_example_keys()
        if key not in NOT_PASSED and "${" + key not in compose and f"  {key}:" not in compose
    )
    assert missing == [], f"add to x-app-env in docker-compose.yml or NOT_PASSED: {missing}"


@pytest.mark.parametrize(
    "key",
    [
        "VAPID_PUBLIC_KEY",
        "VAPID_PRIVATE_KEY",
        "VAPID_SUBJECT",
        "PUSH_ALLOWED_HOSTS",
        "SMS_PROVIDER",
        "SMS_ACCOUNT_SID",
        "SMS_AUTH_TOKEN",
        "SMS_FROM",
        "SMS_API_BASE",
    ],
)
def test_push_and_sms_settings_reach_the_app_and_the_vm_env(key):
    compose = Path("docker-compose.yml").read_text()
    app_env = compose.split("x-app-env: &app-env", 1)[1].split("\n\n", 1)[0]
    assert f"  {key}: ${{{key}:-" in app_env
    assert key in Path(POC, "make-env.sh").read_text()
    assert key in _env_example_keys()


def test_push_allowed_hosts_default_matches_settings():
    from django.conf import settings

    compose = Path("docker-compose.yml").read_text()
    default = re.search(r"PUSH_ALLOWED_HOSTS:-([^}]*)}", compose).group(1)
    assert default.split(",") == list(settings.PUSH_ALLOWED_HOSTS)


# --- Shell syntax ----------------------------------------------------------------------------


@pytest.mark.skipif(BASH is None, reason="bash not installed")
@pytest.mark.parametrize("script", sorted(p.name for p in POC.glob("*.sh")))
def test_scripts_parse(script):
    subprocess.run([BASH, "-n", str(POC / script)], check=True)  # noqa: S603  # nosec B603


def _lib(snippet: str, *args: str, stdin: str = "") -> subprocess.CompletedProcess:
    """Run ``snippet`` in bash with infra/poc/lib.sh sourced; args are $1..."""
    return subprocess.run(  # noqa: S603  # nosec B603
        [BASH, "-c", f"set -euo pipefail; source infra/poc/lib.sh; {snippet}", "lib", *args],
        input=stdin,
        capture_output=True,
        text=True,
    )


# --- BUG-003 / R2-002: the restore drill never touches the live file store ---------------


pytestmark_bash = pytest.mark.skipif(BASH is None, reason="bash not installed")
SWFS, R2, DRILL = "http://seaweedfs:8333", "https://a.r2.cloudflarestorage.com", "hoa-restore-drill"


@pytestmark_bash
@pytest.mark.parametrize(
    ("endpoint", "bucket", "live_endpoint", "live_bucket", "ok"),
    [
        # Live on R2: the drill's own SeaweedFS is fine.
        (
            SWFS,
            DRILL,
            R2,
            "files",
            True,
        ),
        # Live on the local store: same host name, different project and bucket.
        (
            SWFS,
            DRILL,
            SWFS,
            "hoa-private",
            True,
        ),
        # .env leaked through: the drill would write to R2.
        (
            R2,
            "files",
            R2,
            "files",
            False,
        ),
        (R2, DRILL, "x", "y", False),
        (SWFS, "hoa-private", SWFS, "hoa-private", False),
        # Someone named the live bucket like the drill's: still refused.
        (
            SWFS,
            DRILL,
            SWFS,
            DRILL,
            False,
        ),
    ],
)
def test_drill_refuses_any_store_but_its_own(endpoint, bucket, live_endpoint, live_bucket, ok):
    result = _lib(
        'scratch_store_ok "$1" "$2" "$3" "$4"', endpoint, bucket, live_endpoint, live_bucket
    )
    assert (result.returncode == 0) is ok


@pytestmark_bash
def test_file_counts_are_read_from_command_output_and_compared():
    out = '{"level": "INFO", "event": "x 7"}\nImported 12 files.\n'
    assert _lib("file_count Imported", stdin=out).stdout.strip() == "12"
    assert _lib("file_count Exported", stdin="Exported 0 files.\n").stdout.strip() == "0"
    assert _lib("file_count Imported", stdin="Traceback ... 3\n").returncode != 0
    assert _lib('counts_match "$1" "$2" files', "12", "12").returncode == 0
    for expected, actual in (("12", "0"), ("12", ""), ("", "0"), ("3", "x")):
        assert _lib('counts_match "$1" "$2" files', expected, actual).returncode != 0


def test_restore_drill_overrides_the_store_and_checks_counts():
    drill = Path(POC, "restore-drill.sh").read_text()
    store = drill.index("export S3_ENDPOINT_URL=")
    # Every override is in place before the first compose command runs.
    assert store < drill.index('"${SCRATCH[@]}" config')
    assert store < drill.index('"${SCRATCH[@]}" up -d')
    assert drill.index("scratch_store_ok") < drill.index('"${SCRATCH[@]}" up -d')
    assert 'counts_match "$IN_ARCHIVE" "$IMPORTED"' in drill


@pytestmark_bash
@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl not installed")
def test_restore_drill_store_settings_win_over_a_live_r2_env():
    """Run the drill's override block with live R2 values already in the
    environment (as docker compose would read them from .env): the exported
    values that compose actually uses are the scratch store's."""
    drill = Path(POC, "restore-drill.sh").read_text().splitlines()
    start = next(i for i, line in enumerate(drill) if line.startswith("export COMPOSE_PROFILES"))
    end = next(i for i, line in enumerate(drill) if line.startswith("export BACKUP_S3_BUCKET"))
    block = "\n".join(drill[start : end + 1])
    live = {
        "S3_ENDPOINT_URL": R2,
        "S3_BUCKET": "live-files",
        "S3_ACCESS_KEY_ID": "live-id",
        "S3_SECRET_ACCESS_KEY": "live-secret",  # nosec B105 - test value
        "BACKUP_S3_BUCKET": "offsite",
        "BACKUP_S3_SECRET_ACCESS_KEY": "offsite-secret",  # nosec B105 - test value
    }
    result = subprocess.run(  # noqa: S603  # nosec B603
        [BASH, "-c", f"set -euo pipefail; source infra/poc/lib.sh\n{block}\nenv"],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", **live},
    )
    assert result.returncode == 0, result.stderr
    # Exported, so the docker compose process sees them (env prints only exports).
    got = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    assert got["COMPOSE_PROFILES"] == "local-store"
    assert got["S3_ENDPOINT_URL"] == SWFS
    assert got["S3_BUCKET"] == DRILL
    assert got["S3_ACCESS_KEY_ID"].startswith("drill") and got["S3_ACCESS_KEY_ID"] != "live-id"
    assert len(got["S3_SECRET_ACCESS_KEY"]) == 48
    assert got["BACKUP_S3_BUCKET"] == "" and got["BACKUP_S3_SECRET_ACCESS_KEY"] == ""


# --- R2-003 / R2-004: stamped keyed checksums, key never on a command line ----------------


def _mac(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603  # nosec B603
        [sys.executable, "-I", str(POC / "backup_mac.py"), *args],
        capture_output=True,
        text=True,
    )


def _backup(root: Path, stamp: str, key: Path, *, db=b"db", files=b"files") -> Path:
    folder = root / stamp
    folder.mkdir()
    (folder / "db.dump.enc").write_bytes(db)
    (folder / "files.tar.enc").write_bytes(files)
    assert _mac("sign", str(folder), str(key), stamp).returncode == 0
    return folder


@pytest.fixture
def key(tmp_path):
    path = tmp_path / "hoa-backup.key"
    path.write_text("c2VjcmV0LWJhY2t1cC1rZXktZm9yLXRlc3RzLW9ubHk=\n")
    return path


def test_a_signed_backup_verifies_and_shows_its_stamp(tmp_path, key):
    folder = _backup(tmp_path, "2026-11-26T0230Z", key)
    result = _mac("verify", str(folder), str(key))
    assert result.returncode == 0 and result.stdout.strip() == "2026-11-26T0230Z"
    text = (folder / "HMAC-SHA256").read_text()
    assert "stamp 2026-11-26T0230Z" in text and key.read_text().strip() not in text


def test_the_checksum_uses_a_key_derived_from_the_backup_key(tmp_path, key):
    import hashlib
    import hmac

    folder = _backup(tmp_path, "2026-11-26T0230Z", key)
    raw = hmac.new(key.read_bytes().rstrip(b"\n"), b"db", hashlib.sha256).hexdigest()
    assert raw not in (folder / "HMAC-SHA256").read_text()


def test_a_missing_or_emptied_checksum_file_is_refused(tmp_path, key):
    folder = _backup(tmp_path, "2026-11-26T0230Z", key)
    (folder / "HMAC-SHA256").write_text("")
    assert _mac("verify", str(folder), str(key)).returncode == 1
    (folder / "HMAC-SHA256").unlink()
    result = _mac("verify", str(folder), str(key))
    assert result.returncode == 1 and "--allow-unsigned" in result.stderr


def test_a_removed_line_or_a_listed_decoy_is_refused(tmp_path, key):
    folder = _backup(tmp_path, "2026-11-26T0230Z", key)
    lines = (folder / "HMAC-SHA256").read_text().splitlines()
    (folder / "HMAC-SHA256").write_text(
        "\n".join(line for line in lines if not line.endswith("db.dump.enc")) + "\n"
    )
    assert _mac("verify", str(folder), str(key)).returncode == 1
    # A checksum for some other file name does not stand in for db.dump.enc.
    (folder / "decoy").write_bytes(b"x")
    (folder / "HMAC-SHA256").write_text("\n".join(lines).replace("db.dump.enc", "decoy") + "\n")
    assert _mac("verify", str(folder), str(key)).returncode == 1


def test_an_older_dump_with_its_own_checksum_line_is_refused(tmp_path, key):
    old = _backup(tmp_path, "2026-11-12T0230Z", key, db=b"two weeks ago")
    new = _backup(tmp_path, "2026-11-26T0230Z", key, db=b"last night")
    old_line = next(
        line for line in (old / "HMAC-SHA256").read_text().splitlines() if "db.dump.enc" in line
    )
    (new / "db.dump.enc").write_bytes((old / "db.dump.enc").read_bytes())
    text = (new / "HMAC-SHA256").read_text().splitlines()
    swapped = [old_line if line.endswith("db.dump.enc") else line for line in text]
    (new / "HMAC-SHA256").write_text("\n".join(swapped) + "\n")
    assert _mac("verify", str(new), str(key)).returncode == 1


def test_a_whole_older_backup_under_a_newer_name_is_refused(tmp_path, key):
    old = _backup(tmp_path, "2026-11-12T0230Z", key)
    newer_name = tmp_path / "2026-11-26T0230Z"
    old.rename(newer_name)
    result = _mac("verify", str(newer_name), str(key))
    assert result.returncode == 1 and "2026-11-12T0230Z" in result.stderr
    # Under its own name (or a neutral folder) it verifies, and shows its real date.
    neutral = tmp_path / "downloaded"
    newer_name.rename(neutral)
    assert _mac("verify", str(neutral), str(key)).stdout.strip() == "2026-11-12T0230Z"


def test_an_edited_stamp_or_a_wrong_key_is_refused(tmp_path, key):
    folder = _backup(tmp_path, "2026-11-12T0230Z", key)
    neutral = tmp_path / "copy"
    folder.rename(neutral)
    text = (neutral / "HMAC-SHA256").read_text()
    (neutral / "HMAC-SHA256").write_text(text.replace("2026-11-12T0230Z", "2026-11-26T0230Z"))
    assert _mac("verify", str(neutral), str(key)).returncode == 1
    (neutral / "HMAC-SHA256").write_text(text)
    other = tmp_path / "other.key"
    other.write_text("another-key\n")
    assert _mac("verify", str(neutral), str(other)).returncode == 1


def test_old_backups_need_allow_unsigned_and_still_check_the_old_checksum(tmp_path, key):
    import hashlib
    import hmac

    folder = tmp_path / "2026-11-01T0230Z"
    folder.mkdir()
    raw = key.read_bytes().rstrip(b"\n")
    lines = []
    for name, data in (("db.dump.enc", b"db"), ("files.tar.enc", b"files")):
        (folder / name).write_bytes(data)
        lines.append(f"{hmac.new(raw, data, hashlib.sha256).hexdigest()}  {name}")
    (folder / "HMAC-SHA256").write_text("\n".join(lines) + "\n")
    assert _mac("verify", str(folder), str(key)).returncode == 1
    allowed = _mac("verify", str(folder), str(key), "--allow-unsigned")
    assert allowed.returncode == 0 and "WARNING" in allowed.stderr
    (folder / "db.dump.enc").write_bytes(b"tampered")
    assert _mac("verify", str(folder), str(key), "--allow-unsigned").returncode == 1


def test_no_script_puts_the_backup_key_on_a_command_line():
    for script in POC.glob("*.sh"):
        text = script.read_text()
        assert "-hmac" not in text, script
        assert 'cat "$KEY_FILE"' not in text, script
    backup = Path(POC, "backup.sh").read_text()
    drill = Path(POC, "restore-drill.sh").read_text()
    assert 'backup_mac.py" sign "$DEST" "$KEY_FILE" "$STAMP"' in backup
    assert 'backup_mac.py" verify "$SRC" "$KEY_FILE"' in drill
    assert "sha256sum -c" not in drill  # names inside the backup are never trusted
    assert "python3" in Path(POC, "provision-vm.sh").read_text()


# --- R2-005: moving files to R2 keeps the secret off command lines and checks the count -----


@pytestmark_bash
def test_set_env_writes_values_exactly_and_replaces_repeats(tmp_path):
    env = tmp_path / ".env"
    env.write_text("A=1\nS3_SECRET_ACCESS_KEY=old\nB=2\nS3_SECRET_ACCESS_KEY=older\n")
    secret = r"a\nb\\c=d$e'f"  # nosec B105 - test value
    result = _lib('set_env "$1" S3_SECRET_ACCESS_KEY "$2"; set_env "$1" NEW x', str(env), secret)
    assert result.returncode == 0, result.stderr
    assert env.read_text() == f"A=1\nS3_SECRET_ACCESS_KEY={secret}\nB=2\nNEW=x\n"
    assert oct(env.stat().st_mode & 0o777) == "0o600"


def test_move_to_r2_uses_set_env_and_compares_counts():
    script = Path(POC, "move-files-to-r2.sh").read_text()
    assert "awk -v" not in script
    assert 'set_env .env S3_SECRET_ACCESS_KEY "$SECRET"' in script
    assert 'counts_match "$BEFORE" "$IN_R2"' in script
    assert script.index("counts_match") < script.index("Done. Files now live in R2")


# --- R2-011: updates install only from main ----------------------------------------------


@pytestmark_bash
@pytest.mark.parametrize(
    ("branch", "allow", "ok"),
    [
        ("main", "", True),
        ("claude/ecstatic-bell-cnrpzi", "", False),
        ("claude/ecstatic-bell-cnrpzi", "claude/ecstatic-bell-cnrpzi", True),
        ("claude/other", "claude/ecstatic-bell-cnrpzi", False),
        ("mainline", "", False),
    ],
)
def test_updates_install_only_from_main_unless_allowed(branch, allow, ok):
    assert (_lib('branch_allowed "$1" "$2"', branch, allow).returncode == 0) is ok


def test_agent_checks_the_branch_before_installing_and_vm_defaults_to_main():
    agent = Path(POC, "portal-agent.sh").read_text()
    refuse = agent.index('branch_allowed "$BRANCH" "$(env_value ALLOW_BRANCH)"')
    assert refuse < agent.index('update-portal.sh" >>')
    assert 'BRANCH="${BRANCH:-main}"' in Path(POC, "provision-vm.sh").read_text()
