"""Production and POC settings. Refuses to start with an unsafe configuration."""

import ipaddress

from django.core.exceptions import ImproperlyConfigured

from .base import *  # noqa: F403
from .base import env

DEBUG = False
SECURE_SSL_REDIRECT = env.bool("SECURE_SSL_REDIRECT", default=True)
# Start short on the POC; raise to 31536000 for production (runbook).
SECURE_HSTS_SECONDS = env.int("SECURE_HSTS_SECONDS", default=3600)

# Emailed links must point at the public site. If PORTAL_BASE_URL was not
# set (e.g. an older .env), use the public hostname rather than localhost.
if not env("PORTAL_BASE_URL", default="") and ALLOWED_HOSTS:  # noqa: F405
    PORTAL_BASE_URL = f"https://{ALLOWED_HOSTS[0]}"  # noqa: F405

_problems = []
if not ALLOWED_HOSTS:  # noqa: F405
    _problems.append("DJANGO_ALLOWED_HOSTS must list the public hostname(s)")
if "*" in ALLOWED_HOSTS:  # noqa: F405
    _problems.append("DJANGO_ALLOWED_HOSTS must not contain '*'")
if len(SECRET_KEY) < 50 or SECRET_KEY.startswith("dev-insecure"):  # noqa: F405
    _problems.append("DJANGO_SECRET_KEY must be a unique random value of 50+ characters")
if not SESSION_COOKIE_SECURE or not CSRF_COOKIE_SECURE:  # noqa: F405
    _problems.append("Session and CSRF cookies must be Secure in production")
if not DB_APP_ROLE:  # noqa: F405
    _problems.append("DB_APP_ROLE must name the runtime database role (decision Q8)")
if len(AUDIT_HASH_KEY) < 32:  # noqa: F405
    _problems.append("AUDIT_HASH_KEY must be a random value of 32+ characters")
for _cidr in TRUSTED_PROXY_CIDRS:  # noqa: F405
    try:
        _net = ipaddress.ip_network(_cidr.strip(), strict=False)
    except ValueError:
        _problems.append(f"TRUSTED_PROXY_CIDRS entry {_cidr!r} is not a network")
        continue
    if _net.prefixlen == 0:
        _problems.append("TRUSTED_PROXY_CIDRS must not trust every address")
if not TRUSTED_PROXY_CIDRS and not env.bool("NO_TRUSTED_PROXY", default=False):  # noqa: F405
    _problems.append(
        "TRUSTED_PROXY_CIDRS must list the reverse proxy (for example the cloudflared "
        "address); set NO_TRUSTED_PROXY=true only if clients connect directly"
    )
if not PORTAL_BASE_URL.startswith("https://"):
    _problems.append("PORTAL_BASE_URL must be the public https:// address")
if ADMIN_ENABLED:  # noqa: F405
    _problems.append("ADMIN_ENABLED must be false in production (decision Q9)")
if env.bool("DJANGO_DEBUG", default=False):
    _problems.append("DJANGO_DEBUG must be false in production")
if _problems:
    raise ImproperlyConfigured("Unsafe production configuration: " + "; ".join(_problems))

# Demo data may only be loaded on a deployment explicitly marked as the POC
# (security review 2026-11). Anywhere else the flag is ignored.
DEPLOYMENT_STAGE = env("DEPLOYMENT_STAGE", default="production")
if DEPLOYMENT_STAGE != "poc":
    SEED_DEMO_ALLOWED = False
