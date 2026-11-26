"""Production and POC settings. Refuses to start with an unsafe configuration."""

import ipaddress

from django.core.exceptions import ImproperlyConfigured

from .base import *  # noqa: F403
from .base import env

DEBUG = False
SECURE_SSL_REDIRECT = env.bool("SECURE_SSL_REDIRECT", default=True)

# Where this server runs: "production" (real owners) or "poc" (the proof of
# concept VM, fictional data). Production settings default to "production",
# so a forgotten value gets the strict rules, never the relaxed ones.
DEPLOYMENT_STAGE = env("DEPLOYMENT_STAGE", default="production")

# HSTS: a year in production (the minimum below); the POC starts short so a
# certificate mistake there is not remembered by browsers for long (runbook).
_hsts = env("SECURE_HSTS_SECONDS", default="").strip()
SECURE_HSTS_SECONDS = (
    int(_hsts) if _hsts else (31536000 if DEPLOYMENT_STAGE == "production" else 3600)
)
HSTS_PRODUCTION_MINIMUM = 31536000

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

# Stage rules (external review 2026-11-26, D57). Both the POC and production
# hold uploaded files, so both scan every upload with ClamAV and serve only
# files the scanner passed. Production also sends real mail to real people
# (no redirect) and keeps HSTS for at least a year.
if DEPLOYMENT_STAGE not in ("production", "poc"):
    _problems.append('DEPLOYMENT_STAGE must be "production" or "poc"')
if DOCUMENT_SCANNER != "clamd":  # noqa: F405
    _problems.append(
        "DOCUMENT_SCANNER must be clamd (the ClamAV container) on the POC and in production"
    )
elif not DOCUMENT_CLAMD_ADDRESS:  # noqa: F405
    _problems.append("DOCUMENT_CLAMD_ADDRESS must name the ClamAV daemon (for example clamav:3310)")
if not DOCUMENT_REQUIRE_CLEAN_SCAN:  # noqa: F405
    _problems.append("DOCUMENT_REQUIRE_CLEAN_SCAN must be true on the POC and in production")
if DEPLOYMENT_STAGE == "production":
    if EMAIL_REDIRECT_ALL_TO:  # noqa: F405
        _problems.append("EMAIL_REDIRECT_ALL_TO must be empty in production")
    if SECURE_HSTS_SECONDS < HSTS_PRODUCTION_MINIMUM:
        _problems.append(
            f"SECURE_HSTS_SECONDS must be at least {HSTS_PRODUCTION_MINIMUM} in production"
        )
if _problems:
    raise ImproperlyConfigured("Unsafe production configuration: " + "; ".join(_problems))

# Demo data may only be loaded on a deployment explicitly marked as the POC
# (security review 2026-11). Anywhere else the flag is ignored.
if DEPLOYMENT_STAGE != "poc":
    SEED_DEMO_ALLOWED = False
    # Outside the POC a board seat always needs a second board member (D21);
    # the environment cannot switch that off.
    BOARD_SEAT_SECOND_APPROVAL = True
else:
    # POC/test environment: one board member may record seats (D21).
    BOARD_SEAT_SECOND_APPROVAL = env.bool("BOARD_SEAT_SECOND_APPROVAL", default=False)
