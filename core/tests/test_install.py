"""The one-command install (infra/poc/install.sh, D94). The VM is out of reach
from tests: install.sh runs here against a copy of the checkout with the real
lib.sh and make-env.sh, fake step scripts (provision, harden, start, health
timer) and a fake docker, systemctl, ss, nproc, df and timeout on PATH."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import subprocess  # nosec B404 - runs the repository's own scripts
from pathlib import Path

import pytest

POC = Path("infra/poc")
BASH = shutil.which("bash")
GIT = shutil.which("git")
SHELLCHECK = shutil.which("shellcheck")

pytestmark = pytest.mark.skipif(BASH is None, reason="bash not installed")

# A made-up token shaped like Cloudflare's (base64 JSON, starts eyJ).
# The base64 says {"a":"fake-account","t":"fake-tunnel","s":"not-a-real-secret"}.
_FAKE = "eyJhIjoiZmFrZS1hY2NvdW50IiwidCI6ImZha2UtdHVubmVsIiwicyI6Im5vdC1hLXJlYWwtc2VjcmV0In0"
TOKEN = _FAKE + "x" * 60  # not-a-secret  # nosec B105
LINK = "https://portal.example.org/invite/NOT-A-REAL-LINK-TOKEN-0123456789/"
STEP_SCRIPTS = ("provision-vm.sh", "harden-host.sh", "start-portal.sh", "health-timer.sh")


def _fake(directory: Path, name: str, body: str) -> None:
    path = directory / name
    path.write_text("#!/usr/bin/env bash\n" + body)
    path.chmod(0o755)


class Box:
    """A checkout copy, fake programs and the paths install.sh writes to."""

    def __init__(self, root: Path):
        self.root = root
        self.app = root / "app"
        self.poc = self.app / "infra" / "poc"
        self.poc.mkdir(parents=True)
        (self.app / ".git").mkdir()
        for name in ("docker-compose.yml", "docker-compose.tunnel.yml"):
            (self.app / name).write_text("services: {}\n")
        (self.app / "VERSION").write_text("0.44.0\n")
        for name in ("install.sh", "lib.sh", "make-env.sh"):
            shutil.copy(POC / name, self.poc / name)
        self.calls = root / "calls.txt"
        self.argv = root / "argv.txt"
        self.officer = root / "officer-state"
        self.officer.write_text("none\n")
        for name in STEP_SCRIPTS:
            self.step(name)
        self.bin = root / "bin"
        self.bin.mkdir()
        self.os_release = root / "os-release"
        self.os_release.write_text(
            'ID=ubuntu\nVERSION_ID="24.04"\nPRETTY_NAME="Ubuntu 24.04.1 LTS"\n'
        )
        self.meminfo = root / "meminfo"
        self.meminfo.write_text("MemTotal:        4012345 kB\n")
        self.log = root / "log" / "hoa-portal-install.log"
        self.state = root / "state"
        self.ssh_peers = ""
        self.cloudflared_log = "INF Registered tunnel connection connIndex=0 location=iad01"
        self.fakes()

    def step(self, name: str, body: str = "") -> None:
        # Records that it ran, with its argv; start-portal also prints .env's
        # token to prove the log masks it.
        extra = ""
        if name == "start-portal.sh":
            extra = 'grep "^TUNNEL_TOKEN=" "$(dirname "$0")/../../.env"\n'
        _fake(
            self.poc,
            name,
            f'echo "{name}" >> "{self.calls}"\necho "{name} $*" >> "{self.argv}"\n{extra}{body}',
        )

    def fakes(self) -> None:
        record = f'echo "$(basename "$0") $*" >> "{self.argv}"\n'
        _fake(
            self.bin,
            "id",
            record + 'case "$1" in -u) echo 0 ;; -un|-gn) echo root ;; '
            '*) exec /usr/bin/id "$@" ;; esac\n',
        )
        # Tests may run as an ordinary user (GitHub's runners): only root may
        # set an owner, so the fake drops -o/-g there and runs the real install.
        _fake(
            self.bin,
            "install",
            'args=(); while [ $# -gt 0 ]; do case "$1" in -o|-g) '
            'if [ "$(/usr/bin/id -u)" -eq 0 ]; then args+=("$1" "$2"); fi; shift 2 ;; '
            '*) args+=("$1"); shift ;; esac; done; exec /usr/bin/install "${args[@]}"\n',
        )
        _fake(self.bin, "nproc", "echo 4\n")
        _fake(
            self.bin,
            "df",
            'echo "Filesystem 1024-blocks Used Available Capacity Mounted"\n'
            'echo "/dev/sda1 20000000 2000000 18000000 10% /"\n',
        )
        _fake(self.bin, "timeout", record + "exit 0\n")
        _fake(self.bin, "ss", f'cat "{self.root}/ss.txt" 2>/dev/null || true\n')
        _fake(self.bin, "systemctl", record + "exit 1\n")
        _fake(self.bin, "curl", record + "echo -n 302\n")
        # The outbound firewall harden-host.sh installs (D101).
        self.egress_state = "active 2"
        _fake(
            self.bin,
            "hoa-egress-firewall",
            record + f'[ "$1" = status ] && cat "{self.root}/egress.txt"\nexit 0\n',
        )
        _fake(
            self.bin,
            "docker",
            record
            + f"""args="$*"
case "$args" in
  *" ps -a --format"*)
    for s in postgres web worker cloudflared seaweedfs; do echo "$s|running|healthy|0"; done
    echo "clamav|running|healthy|0"; echo "migrate|exited||0"; echo "create-bucket|exited||0" ;;
  *" ps -q cloudflared"*) echo abc123 ;;
  "inspect -f {{{{index .Config.Labels"*) echo oak-creek-portal ;;
  "inspect -f {{{{range"*) echo oak-creek-portal_edge ;;
  *" logs "*cloudflared*) cat "{self.root}/cloudflared.log" ;;
  *"migrate --check"*) exit 0 ;;
  *"bootstrap_first_officer --status"*) cat "{self.officer}" ;;
  *"bootstrap_first_officer --name"*)
    echo invited > "{self.officer}"
    echo "First officer created."
    echo "Open this link to create the login (works once). Do not share it:"
    echo ""
    echo "  {LINK}" ;;
  *"print_invitation_link"*) echo "Open this link:"; echo "{LINK}" ;;
  *) exit 0 ;;
esac
""",
        )

    def run(self, *args: str, stdin: str = "") -> subprocess.CompletedProcess:
        (self.root / "ss.txt").write_text(self.ssh_peers)
        (self.root / "cloudflared.log").write_text(self.cloudflared_log + "\n")
        (self.root / "egress.txt").write_text(self.egress_state + "\n")
        env = {
            "PATH": f"{self.bin}:/usr/sbin:/usr/bin:/sbin:/bin",
            "HOA_INSTALL_LOG": str(self.log),
            "HOA_INSTALL_STATE_DIR": str(self.state),
            "HOA_OS_RELEASE": str(self.os_release),
            "HOA_MEMINFO": str(self.meminfo),
            "HOA_CHECK_WAIT": "0",
            "HOA_EGRESS_BIN": str(self.bin / "hoa-egress-firewall"),
            "HOME": str(self.root),
        }
        return subprocess.run(  # noqa: S603  # nosec B603
            [BASH, str(self.poc / "install.sh"), *args],
            input=stdin,
            capture_output=True,
            text=True,
            env=env,
            start_new_session=True,  # no terminal: nothing can be asked
            timeout=120,
        )

    @property
    def env(self) -> Path:
        return self.app / ".env"

    def steps_run(self) -> list[str]:
        return self.calls.read_text().split() if self.calls.exists() else []


FIRST = (
    "--token-file",
    "-",
    "--hostname",
    "portal.example.org",
    "--officer-name",
    "Pat Example",
    "--officer-email",
    "pat@example.org",
)


@pytest.fixture
def box(tmp_path):
    return Box(tmp_path)


def _secrets(env: Path) -> list[str]:
    values = []
    for line in env.read_text().splitlines():
        key, _, value = line.partition("=")
        if re.search(r"(KEY|PASSWORD|SECRET|TOKEN)", key) and len(value) >= 16:
            values.append(value)
    return values


def test_a_fresh_install_runs_every_step_in_order_and_keeps_secrets_off_screen_and_log(box):
    result = box.run(*FIRST, stdin=TOKEN + "\n")
    assert result.returncode == 0, result.stdout + result.stderr
    assert box.steps_run() == [
        "provision-vm.sh",
        "harden-host.sh",
        "start-portal.sh",
        "health-timer.sh",
    ]
    env = box.env.read_text()
    assert stat.S_IMODE(box.env.stat().st_mode) == 0o600
    assert f"TUNNEL_TOKEN={TOKEN}\n" in env
    assert "COMPOSE_FILE=docker-compose.yml:docker-compose.tunnel.yml\n" in env
    assert "PORTAL_BASE_URL=https://portal.example.org\n" in env
    for key in (
        "DJANGO_SECRET_KEY",
        "HOA_OWNER_DB_PASSWORD",
        "HOA_APP_DB_PASSWORD",
        "POSTGRES_SUPERUSER_PASSWORD",
        "S3_SECRET_ACCESS_KEY",
        "AUDIT_HASH_KEY",
        "BOOKS_PACKAGE_KEY",
    ):
        assert re.search(rf"^{key}=[0-9a-f]{{20,}}$", env, re.M), key
    log = box.log.read_text()
    assert stat.S_IMODE(box.log.stat().st_mode) == 0o600
    shown = result.stdout + result.stderr
    # The token (the fake start-portal.sh printed it) and every generated
    # secret: never on the screen, never in the log.
    assert TOKEN not in log and "[hidden]" in log
    for value in _secrets(box.env):
        assert value not in log and value not in shown
    # The one-time sign-in link: on the screen, never in the log.
    assert LINK in result.stdout
    assert "/invite/" not in log and "shown on screen only" in log
    assert "Pat Example" not in log and "pat@example.org" not in log
    # The token never reached a program's command line.
    assert TOKEN not in box.argv.read_text()
    assert "bootstrap_first_officer --name Pat Example --email pat@example.org --also-sysadmin" in (
        box.argv.read_text()
    )
    assert "setup-offsite-backups.sh" in result.stdout
    assert "Community setup" in result.stdout
    assert "complete" in (box.state / "install-state").read_text().split()


def test_a_second_run_regenerates_nothing_and_asks_nothing(box):
    assert box.run(*FIRST, stdin=TOKEN + "\n").returncode == 0
    before = hashlib.sha256(box.env.read_bytes()).hexdigest()
    box.calls.unlink()
    again = box.run("--yes")
    assert again.returncode == 0, again.stdout + again.stderr
    assert hashlib.sha256(box.env.read_bytes()).hexdigest() == before
    assert box.steps_run() == []  # every step already done: only the checks
    assert "already installed" in again.stdout
    assert "Already done: provision" in again.stdout
    # Nobody has signed in yet: says how to get a new link, prints none itself.
    assert "print_invitation_link" in again.stdout and LINK not in again.stdout
    # --repair runs the steps again, still without touching a secret.
    repair = box.run("--yes", "--repair")
    assert repair.returncode == 0, repair.stderr
    assert box.steps_run() == list(STEP_SCRIPTS)
    assert hashlib.sha256(box.env.read_bytes()).hexdigest() == before


def test_an_interrupted_install_resumes_after_the_last_finished_step(box):
    box.step("harden-host.sh", 'echo "Falco signing key mismatch"; exit 1\n')
    first = box.run(*FIRST, stdin=TOKEN + "\n")
    assert first.returncode == 1
    assert "STOPPED: Host security set-up failed" in first.stderr
    assert "Run this again" in first.stderr
    assert box.steps_run() == ["provision-vm.sh", "harden-host.sh"]
    before = box.env.read_bytes()
    box.step("harden-host.sh")
    box.calls.unlink()
    second = box.run("--yes")
    assert second.returncode == 0, second.stdout + second.stderr
    assert "did not finish" in second.stdout
    assert box.steps_run() == ["harden-host.sh", "start-portal.sh", "health-timer.sh"]
    assert box.env.read_bytes() == before


def test_an_older_env_gets_only_the_generated_secrets_it_lacks(box):
    box.run(*FIRST, stdin=TOKEN + "\n")
    lines = [
        ln
        for ln in box.env.read_text().splitlines()
        if not ln.startswith(("BOOKS_PACKAGE_KEY=", "SEED_DEMO_ALLOWED=", "EMAIL_HOST_USER="))
    ]
    box.env.write_text("\n".join(lines) + "\n")
    box.env.chmod(0o600)
    kept = box.env.read_text()
    result = box.run("--yes")
    assert result.returncode == 0, result.stderr
    text = box.env.read_text()
    assert text.startswith(kept)  # nothing already there changed
    added = text[len(kept) :]
    assert re.fullmatch(r"BOOKS_PACKAGE_KEY=[0-9a-f]{64}\n", added)  # a setting is never added
    assert "BOOKS_PACKAGE_KEY" in result.stdout and added.split("=")[1].strip() not in result.stdout


def test_without_a_terminal_or_token_file_it_stops_before_changing_anything(box):
    result = box.run()
    assert result.returncode == 1
    assert "No tunnel token" in result.stderr and "--token-file" in result.stderr
    assert box.steps_run() == [] and not box.env.exists()


@pytest.mark.parametrize(
    ("token", "why"),
    [("short", "too short"), ("A" * 120 + "$(reboot)", "unexpected characters")],
)
def test_a_bad_token_is_refused_up_front(box, token, why):
    result = box.run("--token-file", "-", stdin=token)
    assert result.returncode == 1 and why in result.stderr
    assert box.steps_run() == [] and not box.env.exists()


def test_the_whole_cloudflare_command_can_be_pasted(box, tmp_path):
    token_file = tmp_path / "tunnel-token"
    token_file.write_text(f"sudo cloudflared service install {TOKEN}\n")
    result = box.run("--token-file", str(token_file), "--skip-first-officer")
    assert result.returncode == 0, result.stderr
    assert f"TUNNEL_TOKEN={TOKEN}\n" in box.env.read_text()
    assert "bootstrap_first_officer --name" not in box.argv.read_text()
    assert "bootstrap_first_officer --name" in result.stdout  # the command for later


@pytest.mark.parametrize(
    ("os_release", "meminfo", "message"),
    [
        ('ID=debian\nVERSION_ID="12"\nPRETTY_NAME="Debian 12"\n', None, "Ubuntu 22.04 or newer"),
        ('ID=ubuntu\nVERSION_ID="20.04"\n', None, "Ubuntu 22.04 or newer"),
        (None, "MemTotal: 2000000 kB\n", "needs a 4 GB server"),
    ],
)
def test_preflight_refuses_an_unsuitable_server_before_any_change(
    box, os_release, meminfo, message
):
    if os_release:
        box.os_release.write_text(os_release)
    if meminfo:
        box.meminfo.write_text(meminfo)
    result = box.run(*FIRST, stdin=TOKEN + "\n")
    assert result.returncode == 1 and message in result.stderr
    assert box.steps_run() == [] and not box.env.exists()


def test_an_ssh_session_is_not_cut_off_without_a_choice(box):
    box.ssh_peers = "0 0 192.0.2.5:22 198.51.100.7:51514\n"
    refused = box.run(*FIRST, stdin=TOKEN + "\n")
    assert refused.returncode == 1
    assert "--admin-cidr 198.51.100.7/32" in refused.stderr and "--close-ssh" in refused.stderr
    assert box.steps_run() == []
    kept = box.run(*FIRST, "--admin-cidr", "198.51.100.7/32", stdin=TOKEN + "\n")
    assert kept.returncode == 0, kept.stderr
    assert "provision-vm.sh" in box.argv.read_text()
    assert box.steps_run()[0] == "provision-vm.sh"


def test_a_refused_token_names_the_fix_and_new_token_replaces_only_it(box):
    box.cloudflared_log = "ERR Register tunnel error: Unauthorized: Invalid tunnel secret"
    result = box.run(*FIRST, stdin=TOKEN + "\n")
    assert result.returncode == 1
    assert "Cloudflare refused the tunnel token" in result.stderr
    assert "install.sh --new-token" in result.stderr
    before = box.env.read_text()
    box.cloudflared_log = "INF Registered tunnel connection connIndex=0"
    new = TOKEN[:-3] + "yyy"
    box.calls.unlink()
    again = box.run("--yes", "--new-token", "--token-file", "-", stdin=new + "\n")
    assert again.returncode == 0, again.stderr
    after = box.env.read_text()
    assert after == before.replace(f"TUNNEL_TOKEN={TOKEN}", f"TUNNEL_TOKEN={new}")
    assert box.steps_run() == ["start-portal.sh"]  # recreates cloudflared with it
    assert new not in box.log.read_text()


def test_the_final_check_includes_the_outbound_firewall(box):
    """D101: the install's checks re-apply the firewall and need it active."""
    result = box.run(*FIRST, stdin=TOKEN + "\n")
    assert result.returncode == 0, result.stderr
    assert "Outbound firewall: on for 2 portal networks" in result.stdout
    assert "hoa-egress-firewall apply --quiet" in box.argv.read_text()
    box.egress_state = "missing 2"
    again = box.run("--yes")
    assert again.returncode == 1
    assert "outbound firewall for the containers is not on (missing)" in again.stderr
    assert "harden-host.sh" in again.stderr


def test_the_checks_report_unhealthy_containers_and_an_extra_network(box):
    docker = (box.bin / "docker").read_text()
    docker = docker.replace('echo "clamav|running|healthy|0"', 'echo "clamav|running|unhealthy|0"')
    docker = docker.replace(
        "echo oak-creek-portal_edge ;;",
        "echo oak-creek-portal_edge; echo oak-creek-portal_default ;;",
    )
    (box.bin / "docker").write_text(docker)
    result = box.run(*FIRST, stdin=TOKEN + "\n")
    assert result.returncode == 1
    assert "not running or not healthy: clamav" in result.stderr
    assert "oak-creek-portal_default" in result.stderr and "edge only" in result.stderr
    assert "complete" not in (box.state / "install-state").read_text()


# --- make-env.sh stays a working step on its own ------------------------------------------


def _make_env_box(tmp_path: Path) -> Path:
    poc = tmp_path / "infra" / "poc"
    poc.mkdir(parents=True)
    for name in ("make-env.sh", "lib.sh"):
        shutil.copy(POC / name, poc / name)
    (tmp_path / "docker-compose.tunnel.yml").write_text("services: {}\n")
    return poc / "make-env.sh"


def test_make_env_still_asks_when_run_by_hand(tmp_path):
    script = _make_env_box(tmp_path)
    result = subprocess.run(  # noqa: S603  # nosec B603
        [BASH, str(script)],
        input=f"{TOKEN}\nredirect@example.org\n",
        capture_output=True,
        text=True,
        start_new_session=True,
    )
    assert result.returncode == 0, result.stderr
    env = (tmp_path / ".env").read_text()
    assert f"TUNNEL_TOKEN={TOKEN}\n" in env
    assert "EMAIL_REDIRECT_ALL_TO=redirect@example.org\n" in env
    assert "start-portal.sh" in result.stdout and TOKEN not in result.stdout
    again = subprocess.run(  # noqa: S603  # nosec B603
        [BASH, str(script)], input=f"{TOKEN}\n\n", capture_output=True, text=True
    )
    assert again.returncode == 1 and "already exists" in again.stderr
    assert (tmp_path / ".env").read_text() == env


def test_make_env_takes_the_token_from_a_file_and_refuses_a_bad_hostname(tmp_path):
    script = _make_env_box(tmp_path)
    token_file = tmp_path / "t"
    token_file.write_text(TOKEN + "\n")
    bad = subprocess.run(  # noqa: S603  # nosec B603
        [BASH, str(script), "--token-file", str(token_file), "--hostname", "a b;c"],
        capture_output=True,
        text=True,
    )
    assert bad.returncode == 1 and "Not a host name" in bad.stderr
    out = tmp_path / "rendered"
    ok = subprocess.run(  # noqa: S603  # nosec B603
        [BASH, str(script), "--token-file", str(token_file), "--no-questions", "--output", out],
        capture_output=True,
        text=True,
    )
    assert ok.returncode == 0, ok.stderr
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    assert not (tmp_path / ".env").exists()


# --- The code comes from the newest release tag, never main -------------------------------


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(  # noqa: S603  # nosec B603
        [GIT, *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.org",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.org",
        },
    ).stdout.strip()


def _origin(tmp_path: Path) -> Path:
    """A repository with releases v0.9.0 and v0.10.0 and newer, unreleased main."""
    origin = tmp_path / "origin"
    poc = origin / "infra" / "poc"
    poc.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=origin)
    shutil.copy(POC / "lib.sh", poc / "lib.sh")
    (origin / "VERSION").write_text("0.9.0\n")
    _git("add", ".", cwd=origin)
    _git("commit", "-q", "-m", "0.9.0", cwd=origin)
    _git("tag", "v0.9.0", cwd=origin)
    (origin / "VERSION").write_text("0.10.0\n")
    for name in ("install.sh", "make-env.sh"):
        shutil.copy(POC / name, poc / name)
    (origin / "docker-compose.yml").write_text("services: {}\n")
    (origin / "docker-compose.tunnel.yml").write_text("services: {}\n")
    _git("add", ".", cwd=origin)
    _git("commit", "-q", "-m", "0.10.0", cwd=origin)
    _git("tag", "v0.10.0", cwd=origin)
    _git("tag", "v0.11.0-rc.1", cwd=origin)
    (origin / "UNRELEASED").write_text("main only\n")
    _git("add", ".", cwd=origin)
    _git("commit", "-q", "-m", "unreleased", cwd=origin)
    return origin


@pytest.mark.skipif(GIT is None, reason="git not installed")
def test_checkout_release_puts_a_fresh_clone_at_the_newest_release_and_never_resets(tmp_path):
    origin = _origin(tmp_path)
    clone = tmp_path / "clone"
    _git("clone", "-q", "--no-checkout", str(origin), str(clone), cwd=tmp_path)

    def checkout():
        return subprocess.run(  # noqa: S603  # nosec B603
            [
                BASH,
                "-c",
                'set -euo pipefail; source "$1"; checkout_release "$2" root',
                "x",
                str(POC / "lib.sh"),
                str(clone),
            ],
            capture_output=True,
            text=True,
        )

    first = checkout()
    assert first.returncode == 0, first.stderr
    assert first.stdout.strip().splitlines()[-1] == "v0.10.0"
    assert (clone / "VERSION").read_text() == "0.10.0\n" and not (clone / "UNRELEASED").exists()
    assert _git("rev-parse", "--abbrev-ref", "HEAD", cwd=clone) == "main"
    assert _git("rev-parse", "--abbrev-ref", "main@{upstream}", cwd=clone) == "origin/main"
    head = _git("rev-parse", "HEAD", cwd=clone)
    assert checkout().returncode == 0 and _git("rev-parse", "HEAD", cwd=clone) == head
    # Local edits are never overwritten.
    (clone / "VERSION").write_text("edited\n")
    (origin / "VERSION").write_text("0.12.0\n")
    _git("commit", "-q", "-am", "0.12.0", cwd=origin)
    _git("tag", "v0.12.0", cwd=origin)
    kept = checkout()
    assert kept.returncode == 0 and "left as it is" in kept.stderr
    assert (clone / "VERSION").read_text() == "edited\n"


@pytest.mark.skipif(GIT is None, reason="git not installed")
def test_curl_bootstrap_clones_the_newest_release_and_hands_over_to_its_install_script(tmp_path):
    origin = _origin(tmp_path)
    # A newer v-tag on a side branch is never run (review EXP-005, 0.44.0).
    _git("checkout", "-q", "-b", "side", cwd=origin)
    (origin / "infra" / "poc" / "lib.sh").write_text('echo "SIDE BRANCH CODE RAN"; exit 7\n')
    _git("commit", "-q", "-am", "side", cwd=origin)
    _git("tag", "v0.99.0", cwd=origin)
    _git("checkout", "-q", "main", cwd=origin)
    box = Box(tmp_path / "box")
    target = tmp_path / "opt" / "hoa-portal"
    script = (POC / "install.sh").read_text()
    env = {
        "PATH": f"{box.bin}:/usr/sbin:/usr/bin:/sbin:/bin",
        "HOA_INSTALL_LOG": str(box.log),
        "HOA_INSTALL_STATE_DIR": str(box.state),
        "HOA_OS_RELEASE": str(box.os_release),
        "HOA_MEMINFO": str(box.meminfo),
        "HOA_CHECK_WAIT": "0",
        "HOA_EGRESS_BIN": str(box.bin / "hoa-egress-firewall"),
        "HOME": str(tmp_path),
        "APP_DIR": str(target),
        "APP_USER": "root",
        "REPO_URL": str(origin),
    }
    # curl ... | sudo bash: the script arrives on stdin, so a token cannot.
    result = subprocess.run(  # noqa: S603  # nosec B603
        [BASH, "-s", "--", "--skip-first-officer"],
        input=script,
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
        start_new_session=True,
        timeout=120,
    )
    assert (target / "VERSION").read_text() == "0.10.0\n"
    assert "SIDE BRANCH CODE RAN" not in result.stdout + result.stderr
    assert not (target / "UNRELEASED").exists()  # never main's unreleased tip
    assert "release's own install.sh" in result.stdout or "own install.sh" in result.stdout
    # The release's install.sh took over and stopped at the token question.
    assert result.returncode == 1 and "No tunnel token" in result.stderr


def test_the_bootstrap_address_is_pinned_to_a_release_tag_never_main():
    readme = (POC / "README.md").read_text()
    script = (POC / "install.sh").read_text()
    pinned = r"https://raw\.githubusercontent\.com/Legal-Copy7045/HOA-Portal/v[0-9<]"
    assert re.search(pinned + r"[^ ]*/infra/poc/install\.sh \| sudo bash", readme)
    assert re.search(pinned + r"[^ ]*/infra/poc/install\.sh \| sudo bash", script)
    for text in (readme, script, (POC / "provision-vm.sh").read_text()):
        assert "HOA-Portal/main/" not in text
    # The checkout follows the same release picker as update-portal.sh.
    lib = (POC / "lib.sh").read_text()
    assert "tag --merged origin/main --list 'v*' | pick_release_tag" in lib
    assert 'checkout_release "$APP_DIR"' in script
    assert 'checkout_release "$APP_DIR" "$APP_USER"' in (POC / "provision-vm.sh").read_text()


def test_install_calls_the_step_scripts_instead_of_repeating_them():
    script = (POC / "install.sh").read_text()
    for name in (*STEP_SCRIPTS, "make-env.sh"):
        assert f'"$POC/{name}"' in script, name
    # No second copy of what make-env.sh generates.
    assert "openssl rand" not in script and "DJANGO_SECRET_KEY" not in script
    # The token: read with builtins, handed over on stdin, never an argument.
    assert 'printf \'%s\\n\' "$TOKEN" | run_logged bash "$POC/make-env.sh" --token-file -' in script
    assert re.search(r"--token-file\s+\"?\$TOKEN\b", script) is None
    assert "read -r -s -p" in script


@pytest.mark.skipif(SHELLCHECK is None, reason="shellcheck not installed")
@pytest.mark.parametrize("name", ["install.sh", "make-env.sh", "provision-vm.sh"])
def test_new_and_changed_scripts_pass_shellcheck(name):
    result = subprocess.run(  # noqa: S603  # nosec B603
        [SHELLCHECK, "-x", str(POC / name)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stdout
    lib = subprocess.run(  # noqa: S603  # nosec B603
        [SHELLCHECK, "-s", "bash", str(POC / "lib.sh")], capture_output=True, text=True
    )
    assert lib.returncode == 0, lib.stdout
