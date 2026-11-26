"""Production settings must refuse unsafe configuration (CLAUDE.md security rules)."""

import importlib
import sys

import pytest
from django.core.exceptions import ImproperlyConfigured

SAFE_ENV = {
    "DJANGO_SECRET_KEY": "x" * 64,
    "DJANGO_ALLOWED_HOSTS": "portal.example.org",
    "DATABASE_URL": "postgres://app:app@db:5432/hoa",
    "DB_APP_ROLE": "hoa_app",
    "DJANGO_DEBUG": "false",
    "ADMIN_ENABLED": "false",
    "AUDIT_HASH_KEY": "k" * 40,
    "TRUSTED_PROXY_CIDRS": "172.30.50.10/32",
    "DEPLOYMENT_STAGE": "production",
    "DOCUMENT_SCANNER": "clamd",
    "DOCUMENT_CLAMD_ADDRESS": "clamav:3310",
    "DOCUMENT_REQUIRE_CLEAN_SCAN": "true",
    "EMAIL_REDIRECT_ALL_TO": "",
}
UNSET = object()


def _load(monkeypatch, **overrides):
    # Nothing from the developer's own environment may decide the outcome.
    monkeypatch.delenv("SECURE_HSTS_SECONDS", raising=False)
    for key, value in {**SAFE_ENV, **overrides}.items():
        if value is UNSET:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    for name in ("core.settings.production", "core.settings.base"):
        sys.modules.pop(name, None)
    try:
        return importlib.import_module("core.settings.production")
    finally:
        for name in ("core.settings.production", "core.settings.base"):
            sys.modules.pop(name, None)


def test_portal_url_defaults_to_the_public_host(monkeypatch):
    monkeypatch.delenv("PORTAL_BASE_URL", raising=False)
    module = _load(monkeypatch)
    assert module.PORTAL_BASE_URL == "https://portal.example.org"


def test_safe_configuration_loads(monkeypatch):
    module = _load(monkeypatch, TRUSTED_PROXY_CIDRS="172.30.50.10/32")
    assert module.TRUSTED_PROXY_CIDRS == ["172.30.50.10/32"]
    assert module.DEBUG is False
    assert module.SESSION_COOKIE_SECURE and module.CSRF_COOKIE_SECURE
    assert module.SECURE_SSL_REDIRECT is True
    assert module.SECURE_HSTS_SECONDS > 0
    assert module.ADMIN_ENABLED is False


@pytest.mark.parametrize(
    "overrides",
    [
        {"DJANGO_ALLOWED_HOSTS": ""},
        {"DJANGO_ALLOWED_HOSTS": "*"},
        {"DJANGO_SECRET_KEY": "short"},  # nosec B105
        {"DJANGO_SECRET_KEY": "dev-insecure-" + "x" * 60},
        {"SESSION_COOKIE_SECURE": "false"},
        {"CSRF_COOKIE_SECURE": "false"},
        {"DB_APP_ROLE": ""},
        {"DJANGO_DEBUG": "true"},
        {"ADMIN_ENABLED": "true"},
        {"AUDIT_HASH_KEY": ""},
        {"AUDIT_HASH_KEY": "too-short"},
        {"TRUSTED_PROXY_CIDRS": "0.0.0.0/0"},
        {"TRUSTED_PROXY_CIDRS": "::/0"},
        {"TRUSTED_PROXY_CIDRS": "not-a-network"},
        {"PORTAL_BASE_URL": "http://localhost:8000"},
        {"TRUSTED_PROXY_CIDRS": ""},  # review B5: rate limits would key on the proxy
    ],
)
def test_unsafe_configuration_is_refused(monkeypatch, overrides):
    with pytest.raises(ImproperlyConfigured):
        _load(monkeypatch, **overrides)


def test_direct_connections_can_opt_out_of_a_proxy(monkeypatch):
    module = _load(monkeypatch, TRUSTED_PROXY_CIDRS="", NO_TRUSTED_PROXY="true")
    assert module.TRUSTED_PROXY_CIDRS == []


@pytest.mark.parametrize("stage,allowed", [("production", False), ("poc", True)])
def test_demo_data_only_on_a_deployment_marked_poc(monkeypatch, stage, allowed):
    module = _load(monkeypatch, SEED_DEMO_ALLOWED="true", DEPLOYMENT_STAGE=stage)
    assert module.SEED_DEMO_ALLOWED is allowed


# --- Stage rules (external review 2026-11-26, D57) ---------------------------------------

POC = {"DEPLOYMENT_STAGE": "poc"}


def test_production_defaults_to_a_year_of_hsts(monkeypatch):
    module = _load(monkeypatch)
    assert module.DEPLOYMENT_STAGE == "production"
    assert module.SECURE_HSTS_SECONDS == 31536000


def test_a_missing_stage_gets_the_production_rules(monkeypatch):
    with pytest.raises(ImproperlyConfigured, match="EMAIL_REDIRECT_ALL_TO"):
        _load(monkeypatch, DEPLOYMENT_STAGE=UNSET, EMAIL_REDIRECT_ALL_TO="inbox@example.org")


@pytest.mark.parametrize("stage", ["production", "poc"])
@pytest.mark.parametrize(
    "overrides,message",
    [
        ({"DOCUMENT_SCANNER": "none"}, "DOCUMENT_SCANNER must be clamd"),
        ({"DOCUMENT_SCANNER": "other"}, "DOCUMENT_SCANNER must be clamd"),
        ({"DOCUMENT_SCANNER": UNSET}, "DOCUMENT_SCANNER must be clamd"),
        ({"DOCUMENT_CLAMD_ADDRESS": ""}, "DOCUMENT_CLAMD_ADDRESS"),
        ({"DOCUMENT_REQUIRE_CLEAN_SCAN": "false"}, "DOCUMENT_REQUIRE_CLEAN_SCAN"),
        ({"DOCUMENT_REQUIRE_CLEAN_SCAN": UNSET}, "DOCUMENT_REQUIRE_CLEAN_SCAN"),
    ],
)
def test_poc_and_production_need_clamav_and_clean_scans(monkeypatch, stage, overrides, message):
    with pytest.raises(ImproperlyConfigured, match=message):
        _load(monkeypatch, DEPLOYMENT_STAGE=stage, **overrides)


@pytest.mark.parametrize(
    "overrides,message",
    [
        ({"EMAIL_REDIRECT_ALL_TO": "inbox@example.org"}, "EMAIL_REDIRECT_ALL_TO"),
        ({"SECURE_HSTS_SECONDS": "3600"}, "SECURE_HSTS_SECONDS"),
        ({"SECURE_HSTS_SECONDS": "31535999"}, "SECURE_HSTS_SECONDS"),
        ({"SECURE_HSTS_SECONDS": "0"}, "SECURE_HSTS_SECONDS"),
    ],
)
def test_production_refuses_redirected_mail_and_short_hsts(monkeypatch, overrides, message):
    with pytest.raises(ImproperlyConfigured, match=message):
        _load(monkeypatch, **overrides)


def test_production_accepts_a_longer_hsts(monkeypatch):
    assert _load(monkeypatch, SECURE_HSTS_SECONDS="63072000").SECURE_HSTS_SECONDS == 63072000


def test_the_poc_may_redirect_mail_and_keep_hsts_short(monkeypatch):
    module = _load(
        monkeypatch, **POC, EMAIL_REDIRECT_ALL_TO="inbox@example.org", SECURE_HSTS_SECONDS="3600"
    )
    assert module.EMAIL_REDIRECT_ALL_TO == "inbox@example.org"
    assert module.SECURE_HSTS_SECONDS == 3600
    assert _load(monkeypatch, **POC).SECURE_HSTS_SECONDS == 3600  # the POC default


def test_an_unknown_stage_is_refused(monkeypatch):
    with pytest.raises(ImproperlyConfigured, match="DEPLOYMENT_STAGE"):
        _load(monkeypatch, DEPLOYMENT_STAGE="staging")


def test_the_tunnel_overlay_sets_the_scanner_itself():
    """The POC/production overlay names the scanner and the clean-scan rule
    directly, so an old .env cannot switch scanning off (D57)."""
    from core.tests.test_image_pins import load_compose

    overlay = load_compose("docker-compose.tunnel.yml")
    env = overlay["x-production-env"]
    assert env["DOCUMENT_SCANNER"] == "clamd"
    assert env["DOCUMENT_CLAMD_ADDRESS"] == "clamav:3310"
    assert env["DOCUMENT_REQUIRE_CLEAN_SCAN"] == "true"
    # HSTS is left to the stage's default unless .env sets it.
    assert env["SECURE_HSTS_SECONDS"] == "${SECURE_HSTS_SECONDS:-}"
    for service in ("web", "worker", "migrate"):
        assert overlay["services"][service]["environment"] == env
