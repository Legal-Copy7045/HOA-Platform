"""Compose files always together, cloudflared on the edge network only (D91,
security review E3/E9). The VM is out of reach: these run infra/poc/lib.sh's
helpers with a fake docker, read the scripts, and check the health check."""

from __future__ import annotations

import datetime
import shutil
import subprocess  # nosec B404 - runs the repository's own scripts
from pathlib import Path

import pytest
from django.utils import timezone

POC = Path("infra/poc")
BASH = shutil.which("bash")
needs_bash = pytest.mark.skipif(BASH is None, reason="bash not installed")
BOTH = "docker-compose.yml:docker-compose.tunnel.yml"


def _lib(snippet: str, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603  # nosec B603
        [BASH, "-c", f"set -euo pipefail; source infra/poc/lib.sh; {snippet}", "lib", *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


# --- COMPOSE_FILE in .env ------------------------------------------------------------------


@needs_bash
def test_missing_compose_file_is_appended_and_no_other_line_changes(tmp_path):
    env = tmp_path / ".env"
    before = "# comment = x\nA=1\nS3_SECRET_ACCESS_KEY=a\\b$c'd\n\nTUNNEL_TOKEN=xyz\n"
    env.write_bytes(before.encode())
    result = _lib('ensure_compose_file "$1"', str(env))
    assert result.returncode == 0, result.stderr
    assert env.read_text() == before + f"COMPOSE_FILE={BOTH}\n"
    assert "Adding COMPOSE_FILE" in result.stdout
    # Idempotent: a second run changes nothing and says nothing.
    again = _lib('ensure_compose_file "$1"', str(env))
    assert again.returncode == 0 and again.stdout == ""
    assert env.read_text() == before + f"COMPOSE_FILE={BOTH}\n"


@needs_bash
def test_a_last_line_without_newline_is_kept_whole(tmp_path):
    env = tmp_path / ".env"
    env.write_text("A=1\nTUNNEL_TOKEN=xyz")
    assert _lib('ensure_compose_file "$1"', str(env)).returncode == 0
    assert env.read_text() == f"A=1\nTUNNEL_TOKEN=xyz\nCOMPOSE_FILE={BOTH}\n"


@needs_bash
def test_a_one_file_compose_file_is_replaced_in_place(tmp_path):
    env = tmp_path / ".env"
    env.write_text("A=1\nCOMPOSE_FILE=docker-compose.yml\nB=2\n")
    env.chmod(0o600)
    result = _lib('ensure_compose_file "$1"', str(env))
    assert result.returncode == 0, result.stderr
    assert env.read_text() == f"A=1\nCOMPOSE_FILE={BOTH}\nB=2\n"
    assert oct(env.stat().st_mode & 0o777) == "0o600"


def test_new_and_existing_installs_get_both_files():
    make_env = (POC / "make-env.sh").read_text()
    assert f"\nCOMPOSE_FILE={BOTH}\n" in make_env
    # The .env heredoc is unquoted: a backtick in it would run a command.
    body = make_env.split('cat > "$OUTPUT" <<ENV\n', 1)[1].split("\nENV\n", 1)[0]
    assert "`" not in body
    start = (POC / "start-portal.sh").read_text()
    assert 'source "$APP_DIR/infra/poc/lib.sh"' in start
    assert start.index("ensure_compose_file .env") < start.index('"${COMPOSE[@]}" up -d')
    update = (POC / "update-portal.sh").read_text()
    # Before any early exit ("nothing to do"), so it always runs.
    first_exit = update.index('echo "Nothing to do.')
    assert update.index("ensure_compose_file .env") < first_exit
    assert update.index("cloudflared_edge_only") < first_exit
    assert update.index("cloudflared_edge_only") < update.index("g fetch --quiet --tags")
    # Every script that runs compose names both files.
    for name in ("start-portal.sh", "update-portal.sh", "portal-agent.sh", "restart-portal.sh"):
        text = (POC / name).read_text()
        assert "-f docker-compose.yml -f docker-compose.tunnel.yml" in text, name
    # Local development keeps the base file only.
    assert "COMPOSE_FILE" not in Path(".env.example").read_text()


def test_cloudflared_is_on_edge_only_in_the_compose_file():
    from core.tests.test_image_pins import load_compose

    tunnel = load_compose("docker-compose.tunnel.yml")
    assert list(tunnel["services"]["cloudflared"]["networks"]) == ["edge"]
    assert tunnel["services"]["web"]["networks"] == ["default", "edge"]


# --- cloudflared_edge_only with a fake docker ---------------------------------------------


@pytest.fixture
def fake_docker(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "docker"
    script.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "docker $*" >> "{tmp_path}/docker.log"\n'
        'case "$*" in\n'
        '  *"ps -q cloudflared"*) [ -n "${CF_ID:-}" ] && echo "$CF_ID" ;;\n'
        '  *"com.docker.compose.project"*) echo oak-creek-portal ;;\n'
        '  *"inspect -f {{range"*) printf "%b" "${CF_NETS:-}" ;;\n'
        "esac\n"
        "exit 0\n"
    )
    script.chmod(0o755)
    return tmp_path


def _edge_only(vm: Path, nets: str, cf_id: str = "abc123") -> tuple[str, str]:
    env = {"PATH": f"{vm}/bin:/usr/bin:/bin", "CF_ID": cf_id, "CF_NETS": nets}
    result = _lib("COMPOSE=(docker compose -f a.yml -f b.yml); cloudflared_edge_only", env=env)
    assert result.returncode == 0, result.stderr
    log = (vm / "docker.log").read_text() if (vm / "docker.log").exists() else ""
    (vm / "docker.log").unlink(missing_ok=True)
    return result.stdout, log


@needs_bash
def test_the_default_network_is_disconnected_and_edge_is_kept(fake_docker):
    out, log = _edge_only(fake_docker, "oak-creek-portal_default\\noak-creek-portal_edge\\n")
    assert "docker network disconnect oak-creek-portal_default abc123\n" in log
    assert "disconnect oak-creek-portal_edge" not in log
    assert "Disconnecting cloudflared from the oak-creek-portal_default network" in out


@needs_bash
def test_edge_only_is_left_alone_and_running_again_does_nothing(fake_docker):
    out, log = _edge_only(fake_docker, "oak-creek-portal_edge\\n")
    assert "disconnect" not in log and "up -d" not in log and out == ""


@needs_bash
def test_no_cloudflared_running_does_nothing(fake_docker):
    _out, log = _edge_only(fake_docker, "", cf_id="")
    assert log.splitlines() == ["docker compose -f a.yml -f b.yml ps -q cloudflared"]


@needs_bash
def test_cloudflared_without_edge_is_recreated_from_both_files(fake_docker):
    _out, log = _edge_only(fake_docker, "oak-creek-portal_default\\n")
    assert "docker compose -f a.yml -f b.yml up -d --no-deps --force-recreate cloudflared" in log
    assert "network disconnect" not in log


@needs_bash
def test_strange_network_names_never_reach_a_command(fake_docker):
    _out, log = _edge_only(
        fake_docker, "oak-creek-portal_edge\\n$(touch pwned)\\n-x; rm\\nbridge\\n"
    )
    assert "docker network disconnect bridge abc123\n" in log
    assert "pwned" not in log and "-x;" not in log
    assert log.count("network disconnect") == 1
    assert not (fake_docker / "pwned").exists() and not Path("pwned").exists()


@needs_bash
def test_extra_networks_lists_only_the_others(fake_docker):
    env = {
        "PATH": f"{fake_docker}/bin:/usr/bin:/bin",
        "CF_ID": "abc123",
        "CF_NETS": "oak-creek-portal_default\\noak-creek-portal_edge\\n",
    }
    result = _lib("COMPOSE=(docker compose); cloudflared_extra_networks", env=env)
    assert result.stdout == "oak-creek-portal_default\n"


def test_agent_reports_the_tunnel_networks_through_psql_variables():
    agent = (POC / "portal-agent.sh").read_text()
    assert 'TUNNEL_LINES="$(cloudflared_networks)"' in agent
    assert 'psql_run -v ts="$TUNNEL_STATE" -v te=' in agent
    assert "jsonb_build_object('tunnel', :'ts', 'tunnel_extra', :'te')" in agent
    # After the host security object is written (which replaces it).
    assert agent.index("SET host_security = :'hs'::jsonb") < agent.index("'tunnel_extra'")


# --- The "Tunnel network" health check -------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("states", "seen_minutes_ago", "expected"),
    [
        ({"tunnel": "running", "tunnel_extra": ""}, 1, True),
        ({"tunnel": "running", "tunnel_extra": "oak-creek-portal_default"}, 1, False),
        ({"tunnel": "absent", "tunnel_extra": ""}, 1, None),
        ({"falco": "active"}, 1, None),  # an agent from before D91
        ({"tunnel": "running", "tunnel_extra": "oak-creek-portal_default"}, 30, None),
    ],
)
def test_tunnel_network_check(states, seen_minutes_ago, expected):
    from core import health
    from core.models import UpdateStatus

    UpdateStatus.objects.create(
        pk=1,
        host_security=states,
        agent_seen_at=timezone.now() - datetime.timedelta(minutes=seen_minutes_ago),
    )
    result = health._tunnel_network()
    if expected is None:
        assert result is None
    else:
        assert result[0] is expected
        if not expected:
            assert "oak-creek-portal_default" in result[1] and "update-portal.sh" in result[1]


@pytest.mark.django_db
def test_tunnel_check_is_registered_and_shows_names_only():
    from core import health
    from core.models import UpdateStatus

    assert "tunnel_network" in [k for k, _label, _fn in health.CHECKS]
    UpdateStatus.objects.create(
        pk=1,
        host_security={"tunnel": "running", "tunnel_extra": "<b>x</b> ok_net"},
        agent_seen_at=timezone.now(),
    )
    ok, detail = health._tunnel_network()
    assert not ok and "ok_net" in detail and "<b>" not in detail
