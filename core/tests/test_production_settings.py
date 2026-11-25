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
}


def _load(monkeypatch, **overrides):
    for key, value in {**SAFE_ENV, **overrides}.items():
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
