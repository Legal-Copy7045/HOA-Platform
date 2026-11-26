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
    subprocess.run([BASH, "-n", str(POC / script)], check=True)  # nosec B603
